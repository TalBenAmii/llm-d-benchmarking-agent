#!/usr/bin/env bash
# install_local.sh — one-shot bootstrap for the llm-d Benchmarking Assistant Agent (local/dev box).
#
# Sets up EVERYTHING needed to run the project end-to-end, in order:
#   1. (clone if missing) the three upstream sibling repos: llm-d/, llm-d-benchmark/, llm-d-skills/
#   2. llm-d client toolchain     → llm-d/helpers/client-setup/install-deps.sh
#                                    (git, curl, tar, yq, kubectl, helm, helm-diff,
#                                     helmfile, kustomize)
#   3. benchmark framework + CLI  → llm-d-benchmark/install.sh --uv
#                                    (builds llm-d-benchmark/.venv + the `llmdbenchmark` CLI)
#   4. this project's venv + app  → .venv synced from uv.lock (`uv sync`), and a .env
#
# Optionally also installs the host cluster prereqs (Docker + the kind binary) via the
# vetted scripts/install/install_prereqs.sh (with --prereqs).
#
# When it finishes:   ./scripts/run.sh   →   http://127.0.0.1:8000
#
# Usage:
#   bash <(curl -fsSL https://raw.githubusercontent.com/TalBenAmii/llm-d-benchmarking-agent/main/llm-d-benchmarking-agent-project/scripts/install_local.sh)
#                                        # curl-bootstrap: clone into INSTALL_DIR, then run on-disk
#   ./scripts/install_local.sh                 # full bootstrap (repos + client deps + bench + app)
#   ./scripts/install_local.sh --dev           # + dev extras (chart-testing; this project's .[dev])
#   ./scripts/install_local.sh --prereqs       # + Docker & kind (host cluster prereqs; needs passwordless sudo)
#   ./scripts/install_local.sh --app-only      # ONLY this project's venv + .env (skip the upstream installers)
#   ./scripts/install_local.sh --no-client     # skip the llm-d client toolchain step
#   ./scripts/install_local.sh --no-bench      # skip the llm-d-benchmark framework step
#   ./scripts/install_local.sh --no-clone      # don't clone missing repos (fail if a needed repo is absent)
#   ./scripts/install_local.sh --no-llm-setup  # skip the interactive Claude-plan (LLM provider) step at the end
#   ./scripts/install_local.sh --no-mcp        # skip installing + registering the llm-d-bench MCP server
#   ./scripts/install_local.sh -h | --help
#
# The three repos are expected as siblings of this project. Override their location with
# REPOS_DIR=/path (matches the agent's own REPOS_DIR setting). In curl-bootstrap mode the repo is
# cloned into INSTALL_DIR (default: ~/llm-d-benchmarking-agent).
#
# Notes:
#   • Step 2 installs SYSTEM packages and uses sudo internally; step 4's Docker/kind install
#     (--prereqs) needs root or passwordless sudo (the agent runs non-interactively).
#   • For a real GPU cluster (beyond the CPU/kind quickstart) see docs/guides/GPU_CLUSTER_RUNBOOK.md.
set -euo pipefail

usage() { sed -n '2,39p' "$0" | sed 's/^# \{0,1\}//'; }
case "${1:-}" in -h|--help) usage; exit 0 ;; esac   # before the bootstrap, so curl-mode --help doesn't clone

# ── Curl-bootstrap (symmetry with the llm-d-bench-mcp installer) ──────────────
# This script also runs via `bash <(curl … install_local.sh)`. Under the curl pipe BASH_SOURCE isn't a
# real path, so use it to locate the project only when that yields a real checkout; otherwise clone
# the repo into INSTALL_DIR and re-exec the on-disk copy so every path below resolves normally.
INSTALL_DIR="${INSTALL_DIR:-$HOME/llm-d-benchmarking-agent}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")/.." 2>/dev/null && pwd || true)"   # this script lives in scripts/
if [[ -z "$PROJECT_DIR" || ! -f "$PROJECT_DIR/pyproject.toml" ]]; then
  [[ "${_AGENT_BOOTSTRAPPED:-0}" == 1 ]] && { echo "install_local.sh: project not found after cloning (bootstrap loop)." >&2; exit 1; }
  command -v git >/dev/null 2>&1 || { echo "install_local.sh: git is required to fetch the repo — install git and re-run." >&2; exit 1; }
  if [[ ! -d "$INSTALL_DIR/.git" ]]; then
    printf '\033[1;35m━━ Fetching llm-d-benchmarking-agent → %s\033[0m\n' "$INSTALL_DIR"
    git clone "https://github.com/TalBenAmii/llm-d-benchmarking-agent" "$INSTALL_DIR"
  fi
  PROJECT_DIR="$INSTALL_DIR/llm-d-benchmarking-agent-project"
  [[ -f "$PROJECT_DIR/scripts/install_local.sh" ]] || { echo "install_local.sh: $PROJECT_DIR/scripts/install_local.sh missing after clone." >&2; exit 1; }
  export _AGENT_BOOTSTRAPPED=1
  exec bash "$PROJECT_DIR/scripts/install_local.sh" "$@"   # re-run on-disk so BASH_SOURCE paths resolve
fi
REPOS_DIR="${REPOS_DIR:-$(dirname "$PROJECT_DIR")}"   # repos are siblings of the project
GUIDE_REPO="$REPOS_DIR/llm-d"
BENCH_REPO="$REPOS_DIR/llm-d-benchmark"
SKILLS_REPO="$REPOS_DIR/llm-d-skills"
VENV="$PROJECT_DIR/.venv"

DEV=0; PREREQS=0; APP_ONLY=0; NO_CLIENT=0; NO_BENCH=0; NO_CLONE=0; NO_LLM_SETUP=0; NO_MCP=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dev)          DEV=1 ;;
    --prereqs)      PREREQS=1 ;;
    --app-only)     APP_ONLY=1 ;;
    --no-client)    NO_CLIENT=1 ;;
    --no-bench)     NO_BENCH=1 ;;
    --no-clone)     NO_CLONE=1 ;;
    --no-llm-setup) NO_LLM_SETUP=1 ;;
    --no-mcp)       NO_MCP=1 ;;
    -h|--help)      usage; exit 0 ;;
    *) echo "install_local.sh: unknown option '$1' (try --help)" >&2; exit 2 ;;
  esac
  shift
done

log()  { printf '\033[35m▸\033[0m %s\n' "$*"; }            # llm-d purple bullet
step() { printf '\n\033[1;35m━━ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[install] %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31m[install] ERROR: %s\033[0m\n' "$*" >&2; exit 1; }
trap 'rc=$?; [[ $rc -ne 0 ]] && printf "\n\033[1;31m[install] aborted (exit %s).\033[0m See the message above; fix it and re-run (the script is idempotent).\n" "$rc" >&2' EXIT
# shellcheck source-path=SCRIPTDIR/..
# shellcheck source=scripts/_env.sh
source "$PROJECT_DIR/scripts/_env.sh"
add_local_bin_to_path   # ~/.local/bin holds claude + uv; put it on PATH so every step below sees them

SUDO=""
if [[ "$(id -u)" -ne 0 ]] && command -v sudo >/dev/null 2>&1; then SUDO="sudo"; fi

# ── Base tools (git / curl / tar / sudo) — bootstrap on a bare Debian/Ubuntu ─
# A fresh box may have none of these, yet step 1 needs git+curl to clone and
# the client toolchain needs curl+tar to fetch binaries. We also ensure `sudo`:
# the upstream installers call `sudo …` UNCONDITIONALLY (even when run as root),
# so a raw root rootfs with no sudo binary dies with "sudo: command not found".
# Installing it is harmless — as root, sudo just execs the command. apt-install
# whatever is missing (no-op on a warm box; harmless if apt is absent and they exist).
ensure_base_tools() {
  local miss=() t need="git curl tar"
  # only need sudo when we'll invoke the upstream installers (they hard-require it)
  [[ "$NO_CLIENT" != 1 || "$NO_BENCH" != 1 ]] && need="$need sudo"
  for t in $need; do command -v "$t" >/dev/null 2>&1 || miss+=("$t"); done
  [[ ${#miss[@]} -eq 0 ]] && return 0
  if command -v apt-get >/dev/null 2>&1; then
    log "Installing base tools (${miss[*]})…"
    $SUDO apt-get update -y  >/dev/null 2>&1 || true
    $SUDO apt-get install -y "${miss[@]}" ca-certificates >/dev/null 2>&1 || true
  fi
  for t in "${miss[@]}"; do
    command -v "$t" >/dev/null 2>&1 || die "$t is required but could not be installed automatically — install it (e.g. 'apt install $t') and re-run."
  done
}

# ── uv is REQUIRED — ensure it's on PATH, bootstrapping it if missing ────────
# The venv is built by `uv sync` from the committed uv.lock (the single source of truth), so uv is
# now a hard requirement. It is self-contained (needs no python3-venv) and fetches a matching CPython
# itself. Resolve LAZILY (after step 2 has provided curl on a bare box): use an existing uv, else
# bootstrap it via the official installer into ~/.local/bin — and fail loudly if that can't happen.
_UV_DONE=0
ensure_uv() {
  [[ "$_UV_DONE" == 1 ]] && return 0
  if ! command -v uv >/dev/null 2>&1; then
    log "uv not found — bootstrapping it (self-contained; required for install)…"
    command -v curl >/dev/null 2>&1 || die "uv is required but missing, and curl isn't available to bootstrap it — install uv (https://astral.sh/uv) and re-run."
    curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 || die "uv bootstrap failed (no network?). Install uv (https://astral.sh/uv) and re-run."
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    command -v uv >/dev/null 2>&1 || die "uv was bootstrapped but is not on PATH — open a new shell or add ~/.local/bin to PATH, then re-run."
  fi
  _UV_DONE=1
  log "uv: $(command -v uv)"
}

if [[ "$PREREQS" == 1 && "$APP_ONLY" != 1 ]]; then
  step "Host prereqs: Docker + kind (scripts/install/install_prereqs.sh)"
  bash "$PROJECT_DIR/scripts/install/install_prereqs.sh" --docker --kind --kubectl
fi

if [[ "$APP_ONLY" != 1 ]]; then
  step "Upstream repos (siblings under $REPOS_DIR)"
  ensure_base_tools   # git/curl/tar — clone + the client toolchain need these
  # llm-d is needed for the client toolchain; llm-d-benchmark for the framework + CLI;
  # llm-d-skills (llm-d-incubation org) grounds the agent's procedures — required at runtime.
  [[ "$NO_CLIENT" == 1 ]] || clone_if_missing "llm-d" "$GUIDE_REPO"
  [[ "$NO_BENCH"  == 1 ]] || clone_if_missing "llm-d-benchmark" "$BENCH_REPO"
  clone_if_missing "llm-d-skills" "$SKILLS_REPO" "llm-d-incubation"
fi

if [[ "$APP_ONLY" != 1 && "$NO_CLIENT" != 1 ]]; then
  step "llm-d client toolchain (llm-d/helpers/client-setup/install-deps.sh)"
  CLIENT_SH="$GUIDE_REPO/helpers/client-setup/install-deps.sh"
  [[ -f "$CLIENT_SH" ]] || die "expected $CLIENT_SH — is the llm-d repo populated? (re-run without --no-clone)"
  dev_args=(); [[ "$DEV" == 1 ]] && dev_args=(--dev)
  ( cd "$GUIDE_REPO" && bash "helpers/client-setup/install-deps.sh" "${dev_args[@]}" )
fi

if [[ "$APP_ONLY" != 1 && "$NO_BENCH" != 1 ]]; then
  ensure_uv
  step "Benchmark framework + CLI (llm-d-benchmark/install.sh --uv)"
  BENCH_SH="$BENCH_REPO/install.sh"
  [[ -f "$BENCH_SH" ]] || die "expected $BENCH_SH — is the llm-d-benchmark repo populated? (re-run without --no-clone)"
  ( cd "$BENCH_REPO" && bash "install.sh" --uv )
fi

step "This project (.env + venv + app)"
cd "$PROJECT_DIR"
ensure_uv   # no-op if the benchmark step already ensured it; matters for --app-only/--no-bench

ensure_env

# `uv sync` builds .venv from uv.lock (the committed source of truth) and installs the agent
# editable, honouring requires-python / .python-version (uv fetches a matching CPython if the host
# lacks one). --extra dev adds the test/lint toolchain the git hooks + `make` expect in .venv.
# --inexact: force the agent's own packages back to their locked versions WITHOUT pruning the
# editable MCP server (installed further below) — so re-running install_local.sh over an existing venv
# restores lock-compliance yet keeps the MCP install instead of removing then re-adding it.
if [[ "$DEV" == 1 ]]; then log "Syncing the agent venv from uv.lock (with dev extras)…"; uv sync --inexact --extra dev >/dev/null
else                        log "Syncing the agent venv from uv.lock…";                  uv sync --inexact >/dev/null; fi
PY="$VENV/bin/python"
"$PY" -c "import app.main" >/dev/null 2>&1 && log "Agent imports OK." || die "the agent failed to import after install."

# Offer to wire the user's Claude subscription as the LLM provider (consent-first; skips itself
# without a TTY). Best-effort: a declined/failed setup must never fail the install.
if [[ "$NO_LLM_SETUP" != 1 ]]; then
  step "LLM provider — wire your Claude plan (optional)"
  bash "$PROJECT_DIR/scripts/install/setup-claude-plan.sh" \
    || warn "Claude-plan setup didn't complete — run ./scripts/install/setup-claude-plan.sh anytime."
fi

# ── MCP server — editable-install into the shared venv + register with Claude Code ─
# Auto-complete, no prompts. NON-FATAL: any failure (clone/pip/register) warns and continues —
# a broken MCP add must never abort a working app install. Skipped by --no-mcp / --app-only, and
# (like the upstream repos) by --no-clone when the repo isn't already on disk.
MCP_SUMMARY="skipped (--no-mcp)"
if [[ "$NO_MCP" != 1 && "$APP_ONLY" != 1 ]]; then
  step "MCP server (llm-d-bench → Claude Code)"
  MCP_DIR="$(dirname "$PROJECT_DIR")/llm-d-bench-mcp"   # sibling of the project, under the monorepo clone
  MCP_RC=0
  (
    if [[ ! -d "$MCP_DIR" || -z "$(ls -A "$MCP_DIR" 2>/dev/null)" ]]; then
      if [[ "$NO_CLONE" == 1 ]]; then
        warn "llm-d-bench-mcp absent at $MCP_DIR and --no-clone was given — skipping MCP setup."; exit 2
      fi
      log "Cloning llm-d-bench-mcp → $MCP_DIR"
      git clone --depth 1 "https://github.com/TalBenAmii/llm-d-bench-mcp" "$MCP_DIR" || exit 1
    fi
    log "Installing the MCP server (editable) into the agent venv, pinned to the agent's locked versions…"
    # set -e is disabled inside a subshell on the left of ||, so guard each fallible command
    # explicitly — a failed clone/export/pip must exit the subshell non-zero (→ MCP_RC → "setup failed").
    # >/dev/null suppresses only stdout; pip/git errors on stderr stay visible for diagnosis.
    #
    # The agent's uv.lock is authoritative: export it to a constraints file and pin the MCP install
    # to it, so every package MCP shares with the agent (mcp, anyio, pydantic, starlette, uvicorn, …)
    # resolves to the LOCKED version instead of drifting. If MCP genuinely required an incompatible
    # version the constrained resolve ERRORS here — desired (fail loudly, don't silently drift).
    #   --no-hashes: this is a version-pin only; the authoritative hash verification already ran in
    #     `uv sync`, and hashes would only bloat the throwaway file.
    #   (--no-dev omitted on purpose so dev-group pins are covered too — constraints bind only the
    #     packages actually being installed, so the extra lines are harmless.)
    CONSTRAINTS="$(mktemp)" || exit 1
    trap 'rm -f "$CONSTRAINTS"' EXIT   # subshell-local trap; cleans up the temp file, leaves the outer EXIT trap intact
    uv export --project "$PROJECT_DIR" --frozen --no-emit-project --no-hashes --format requirements.txt -o "$CONSTRAINTS" >/dev/null || exit 1
    uv pip install --python "$PY" -e "$MCP_DIR" --constraint "$CONSTRAINTS" >/dev/null || exit 1
    register_mcp_server "$VENV/bin/llm-d-bench-mcp" user 0 || exit 3   # installed but not registered (claude CLI absent)
  ) || MCP_RC=$?
  case "$MCP_RC" in
    0) MCP_SUMMARY="registered with Claude Code  (llm-d-bench)" ;;
    2) MCP_SUMMARY="skipped (repo absent, --no-clone)" ;;
    3) MCP_SUMMARY="installed; run 'claude mcp add' — see above" ;;
    *) MCP_SUMMARY="setup failed — see above"; warn "MCP setup didn't complete — the app is fine; finish it later." ;;
  esac
fi

have() { command -v "$1" >/dev/null 2>&1 && printf 'present' || printf 'MISSING'; }
step "Summary"
if [[ "$APP_ONLY" != 1 ]]; then
  printf '  client toolchain : kubectl=%s helm=%s helmfile=%s kustomize=%s yq=%s\n' \
    "$(have kubectl)" "$(have helm)" "$(have helmfile)" "$(have kustomize)" "$(have yq)"
  printf '  benchmark CLI    : %s  (%s/.venv)\n' \
    "$([[ -x "$BENCH_REPO/.venv/bin/llmdbenchmark" ]] && echo present || echo 'in repo .venv — activate to use')" "$BENCH_REPO"
  [[ "$PREREQS" == 1 ]] && printf '  host prereqs     : docker=%s kind=%s\n' "$(have docker)" "$(have kind)"
  printf '  MCP server       : %s\n' "$MCP_SUMMARY"
fi
printf '  agent venv       : %s\n' "$VENV"
printf '\n'
log "Done. Start the agent with:  cd $PROJECT_DIR && ./scripts/run.sh --open   (--open auto-opens your browser; otherwise visit http://127.0.0.1:8000)"
[[ "$PREREQS" != 1 && "$APP_ONLY" != 1 ]] && \
  log "To benchmark on a local kind cluster you'll also need Docker + kind — re-run with --prereqs (needs passwordless sudo), or let the agent install them on demand."
trap - EXIT
