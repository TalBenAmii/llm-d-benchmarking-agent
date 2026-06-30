#!/usr/bin/env bash
# Per-flow ISOLATED runner for the live / simulate flow eval — the bulletproof timeout.
#
# Why this exists (and why the in-process caps are not enough)
# -----------------------------------------------------------
# The live/simulate eval drives each flow with a real LLM through the claude-agent-sdk, which spawns
# a bundled CLI subprocess. Two failure modes defeat the in-process asyncio caps in
# scripts/validate_flows.py (_bounded) and tests/flows/harness.py (_TimeoutTurn):
#   1. A blocking/synchronous call freezes the asyncio event loop, so the per-call and per-flow
#      `asyncio.wait(timeout=…)` timers never fire — a frozen loop cannot run its own timers.
#   2. AgentSdkProvider reuses ONE long-lived SDK subprocess across flows; accumulated state
#      DEADLOCKS between flows (idle event loop ↔ idle subprocess), past every in-process cap.
# Neither is fixable from inside the process. This runner fixes both structurally:
#   • each flow runs as its OWN python process, so it gets a FRESH SDK subprocess  → kills mode 2;
#   • each process is wrapped in coreutils `timeout -s TERM -k`, an EXTERNAL kernel-level kill no
#     in-process freeze can defeat (Python's default SIGTERM disposition terminates even a frozen
#     loop; -k escalates to SIGKILL after a grace window)                          → kills mode 1.
# One stuck flow can never stall the run: timeout kills it, we reap its orphaned SDK subprocess, and
# we move to the next flow. The in-process caps stay as a faster first-line defense; THIS is the
# guarantee. (Background: docs/VALIDATION.md §"Isolated eval runner".)
#
# Usage:
#   scripts/run_eval_isolated.sh [live|simulate] [flow-name …]
#     (no flow names) → every flow scored in that mode
#     flow-name …      → just those flows (e.g. re-running a subset of failures)
#
# Env overrides (sane defaults; set these in a git WORKTREE or a non-standard layout):
#   PYTHON                interpreter (default: <proj>/.venv/bin/python, else python3)
#   REPOS_DIR             monorepo root holding llm-d/ + llm-d-benchmark/ (default: parent of project;
#                         in a WORKTREE the siblings are empty → point this at the PRIMARY checkout)
#   EVAL_LOG_DIR          per-flow logs + summary land here (default: <proj>/workspace/eval-logs, gitignored)
#   RUN_TAG               suffix for the summary/combined-log filenames (default: empty)
#   LLM_PROVIDER          default: claude-agent-sdk
#   AGENT_SDK_MODEL       default: claude-sonnet-4-6
#   LLM_EVAL_CALL_TIMEOUT in-process per-call cap, s (default: 120) — first-line defense
#   LLM_EVAL_FLOW_TIMEOUT in-process per-flow cap, s (default: 360) — first-line defense
#   LLM_EVAL_HARD_TIMEOUT EXTERNAL per-flow wall-clock kill, s (default: 420) — the guarantee
#   LLM_EVAL_KILL_GRACE   SIGTERM → SIGKILL escalation window, s (default: 15)
set -u

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJ="$(cd "$here/.." && pwd)"
cd "$PROJ" || exit 99

MODE="${1:-simulate}"
case "$MODE" in
  live|simulate) shift || true ;;
  *) echo "usage: $(basename "$0") [live|simulate] [flow …]" >&2; exit 2 ;;
esac
SUBSET=("$@")                               # optional explicit flow names; empty = all flows for the mode

# Interpreter: explicit PYTHON wins, else the project venv, else system python3.
if [ -n "${PYTHON:-}" ]; then PY="$PYTHON"
elif [ -x "$PROJ/.venv/bin/python" ]; then PY="$PROJ/.venv/bin/python"
else PY="$(command -v python3 || command -v python)"; fi

REPOS_DIR="${REPOS_DIR:-$(cd "$PROJ/.." && pwd)}"
LOG="${EVAL_LOG_DIR:-$PROJ/workspace/eval-logs}"
mkdir -p "$LOG"

HARD="${LLM_EVAL_HARD_TIMEOUT:-420}"        # per-flow HARD wall-clock kill (s) — the external guarantee
GRACE="${LLM_EVAL_KILL_GRACE:-15}"          # SIGTERM → SIGKILL escalation window (s)

export LLM_EVAL_LIVE=1
export LLM_PROVIDER="${LLM_PROVIDER:-claude-agent-sdk}"
export AGENT_SDK_MODEL="${AGENT_SDK_MODEL:-claude-sonnet-4-6}"
export SIMULATE=0                           # app-wide SIMULATE off (the eval "simulate" MODE is the --simulate flag)
export PYTHONPATH="$PROJ"
export REPOS_DIR
export LLM_EVAL_CALL_TIMEOUT="${LLM_EVAL_CALL_TIMEOUT:-120}"
export LLM_EVAL_FLOW_TIMEOUT="${LLM_EVAL_FLOW_TIMEOUT:-360}"
export PYTHONUNBUFFERED=1                   # stream each flow's log live (else block-buffered = invisible)

SUMMARY="$LOG/iso_${MODE}${RUN_TAG:-}_summary.txt"
COMBINED="$LOG/iso_${MODE}${RUN_TAG:-}.log"
: > "$SUMMARY"; : > "$COMBINED"

# Reap ONLY orphaned (ppid==1) SDK subprocesses — never a live app's (those stay parented to the app,
# ppid≠1) and never an editor's. Safe between-flow cleanup; a no-op for providers that spawn no such
# subprocess. (The kill is scoped by both the orphan check AND the bundled-claude marker.)
reap_orphans() {
  ps -eo pid,ppid,args 2>/dev/null \
    | awk '$2==1 && /_bundled\/claude/ {print $1}' \
    | xargs -r kill -9 2>/dev/null || true
}

# Flow names for the active mode — exactly what validate_flows would select (live_eval && mode in live_modes).
flow_names() {
  if [ "${#SUBSET[@]}" -gt 0 ]; then printf '%s\n' "${SUBSET[@]}"; return; fi
  "$PY" - "$MODE" <<'PYEOF'
import sys
from tests.flows.flows import ALL_FLOWS
mode = sys.argv[1]
for f in ALL_FLOWS:
    if f.live_eval and mode in f.live_modes:
        print(f.name)
PYEOF
}

run_one() {
  local flow="$1" flog="$2"
  timeout -k "$GRACE" -s TERM "$HARD" \
    "$PY" -u scripts/validate_flows.py --flow "$flow" --"$MODE" >"$flog" 2>&1
}

mapfile -t FLOWS < <(flow_names)
n=${#FLOWS[@]}
if [ "$n" -eq 0 ]; then echo "no flows selected for mode=$MODE" >&2; exit 3; fi
echo "### ISOLATED $MODE run — $n flows, HARD=${HARD}s (-k ${GRACE}s), call=${LLM_EVAL_CALL_TIMEOUT}s flow=${LLM_EVAL_FLOW_TIMEOUT}s" | tee -a "$SUMMARY"

pass=0; fail=0; tmo=0; i=0
for flow in "${FLOWS[@]}"; do
  i=$((i+1))
  flog="$LOG/iso_${MODE}_${flow}.log"
  t0=$(date +%s)
  run_one "$flow" "$flog"; rc=$?
  dt=$(( $(date +%s) - t0 ))
  reap_orphans
  if [ "$rc" -eq 0 ]; then
    tag="PASS"; pass=$((pass+1))
  elif [ "$rc" -eq 124 ] || [ "$rc" -ge 128 ]; then   # 124 = timeout fired; ≥128 = killed by signal
    tag="TIMEOUT(rc=$rc)"; tmo=$((tmo+1))
  else
    tag="FAIL(rc=$rc)"; fail=$((fail+1))
  fi
  line="[$tag] ($i/$n, ${dt}s) $flow"
  echo "$line" | tee -a "$SUMMARY"
  { echo "===== $line ====="; cat "$flog"; echo; } >> "$COMBINED"
done

echo "### DONE $MODE: pass=$pass fail=$fail timeout=$tmo of $n" | tee -a "$SUMMARY"
# Exit non-zero if anything failed or timed out (live eval is informational, but a non-zero exit lets
# CI / a wrapper notice). The per-flow detail is in the summary + combined log under $LOG.
[ "$fail" -eq 0 ] && [ "$tmo" -eq 0 ]
