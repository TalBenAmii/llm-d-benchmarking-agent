#!/usr/bin/env bash
# install.sh — one-shot bootstrap for the llm-d Benchmarking Assistant Agent.
#
# Sets up EVERYTHING needed to run the project end-to-end, in order:
#   1. (clone if missing) the three upstream sibling repos: llm-d/, llm-d-benchmark/, llm-d-skills/
#   2. llm-d client toolchain     → llm-d/helpers/client-setup/install-deps.sh
#                                    (git, curl, tar, yq, kubectl, helm, helm-diff,
#                                     helmfile, kustomize)
#   3. benchmark framework + CLI  → llm-d-benchmark/install.sh --uv
#                                    (builds llm-d-benchmark/.venv + the `llmdbenchmark` CLI)
#   4. this project's venv + app  → .venv + `pip install -e .`, and a .env
#
# Optionally also installs the host cluster prereqs (Docker + the kind binary) via the
# vetted scripts/install_prereqs.sh (with --prereqs).
#
# When it finishes:   ./scripts/run.sh   →   http://127.0.0.1:8000
#
# Usage:
#   ./scripts/install.sh                 # full bootstrap (repos + client deps + bench + app)
#   ./scripts/install.sh --dev           # + dev extras (chart-testing; this project's .[dev])
#   ./scripts/install.sh --prereqs       # + Docker & kind (host cluster prereqs; needs passwordless sudo)
#   ./scripts/install.sh --app-only      # ONLY this project's venv + .env (skip the upstream installers)
#   ./scripts/install.sh --no-client     # skip the llm-d client toolchain step
#   ./scripts/install.sh --no-bench      # skip the llm-d-benchmark framework step
#   ./scripts/install.sh --no-clone      # don't clone missing repos (fail if a needed repo is absent)
#   ./scripts/install.sh --no-llm-setup  # skip the interactive Claude-plan (LLM provider) step at the end
#   ./scripts/install.sh --uv | --no-uv  # force the venv backend (default: uv if present, else python3 -m venv)
#   ./scripts/install.sh -h | --help
#
# The three repos are expected as siblings of this project. Override their location with
# REPOS_DIR=/path (matches the agent's own REPOS_DIR setting).
#
# Notes:
#   • Step 2 installs SYSTEM packages and uses sudo internally; step 4's Docker/kind install
#     (--prereqs) needs root or passwordless sudo (the agent runs non-interactively).
#   • For a real GPU cluster (beyond the CPU/kind quickstart) see docs/GPU_CLUSTER_RUNBOOK.md.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # this script lives in scripts/
REPOS_DIR="${REPOS_DIR:-$(dirname "$PROJECT_DIR")}"   # repos are siblings of the project
GUIDE_REPO="$REPOS_DIR/llm-d"
BENCH_REPO="$REPOS_DIR/llm-d-benchmark"
SKILLS_REPO="$REPOS_DIR/llm-d-skills"
VENV="$PROJECT_DIR/.venv"

DEV=0; PREREQS=0; APP_ONLY=0; NO_CLIENT=0; NO_BENCH=0; NO_CLONE=0; NO_LLM_SETUP=0; USE_UV="auto"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dev)          DEV=1 ;;
    --prereqs)      PREREQS=1 ;;
    --app-only)     APP_ONLY=1 ;;
    --no-client)    NO_CLIENT=1 ;;
    --no-bench)     NO_BENCH=1 ;;
    --no-clone)     NO_CLONE=1 ;;
    --no-llm-setup) NO_LLM_SETUP=1 ;;
    --uv)           USE_UV=1 ;;
    --no-uv)        USE_UV=0 ;;
    -h|--help)      sed -n '2,36p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "install.sh: unknown option '$1' (try --help)" >&2; exit 2 ;;
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

# ── Resolve the venv backend (uv vs python3 -m venv), robust on a fresh box ─
# The trap a fresh Debian/Ubuntu falls into: python3 is present but python3-venv
# is NOT (so `python3 -m venv` fails with "ensurepip is not available") and uv
# isn't installed either. Resolve LAZILY (after step 2 has provided curl) and,
# in auto mode, GUARANTEE a working backend: an existing uv, else a python3 that
# can really build a venv, else apt-install python3-venv, else bootstrap uv.
_BACKEND_DONE=0
resolve_backend() {
  [[ "$_BACKEND_DONE" == 1 ]] && return 0
  if [[ "$USE_UV" == 1 || "$USE_UV" == 0 ]]; then
    :                                          # explicit --uv / --no-uv: honour it
  elif command -v uv >/dev/null 2>&1; then
    USE_UV=1
  elif command -v python3 >/dev/null 2>&1 && python3 -c 'import ensurepip' >/dev/null 2>&1; then
    USE_UV=0                                   # system python3 can build a venv
  else
    warn "python3 cannot create virtual environments here (python3-venv/ensurepip missing)."
    if command -v apt-get >/dev/null 2>&1; then
      log "Installing python3-venv + python3-pip…"
      $SUDO apt-get update -y  >/dev/null 2>&1 || true
      $SUDO apt-get install -y python3-venv python3-pip >/dev/null 2>&1 || true
    fi
    if command -v python3 >/dev/null 2>&1 && python3 -c 'import ensurepip' >/dev/null 2>&1; then
      USE_UV=0
    else
      log "Bootstrapping uv instead (self-contained — needs no python3-venv)…"
      command -v curl >/dev/null 2>&1 || die "need either a working python3-venv or curl (to bootstrap uv). Install python3-venv and re-run."
      curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 || die "uv bootstrap failed (no network?). Install python3-venv and re-run."
      export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
      command -v uv >/dev/null 2>&1 || die "uv was bootstrapped but is not on PATH — open a new shell or add ~/.local/bin to PATH, then re-run."
      USE_UV=1
    fi
  fi
  [[ "$USE_UV" == 1 ]] && BENCH_UV="--uv" || BENCH_UV="--no-uv"
  _BACKEND_DONE=1
  log "venv backend: $([[ "$USE_UV" == 1 ]] && echo uv || echo 'python3 -m venv')"
}

if [[ "$PREREQS" == 1 && "$APP_ONLY" != 1 ]]; then
  step "Host prereqs: Docker + kind (scripts/install_prereqs.sh)"
  bash "$PROJECT_DIR/scripts/install_prereqs.sh" --docker --kind --kubectl
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
  resolve_backend
  step "Benchmark framework + CLI (llm-d-benchmark/install.sh $BENCH_UV)"
  BENCH_SH="$BENCH_REPO/install.sh"
  [[ -f "$BENCH_SH" ]] || die "expected $BENCH_SH — is the llm-d-benchmark repo populated? (re-run without --no-clone)"
  ( cd "$BENCH_REPO" && bash "install.sh" "$BENCH_UV" )
fi

step "This project (.env + venv + app)"
cd "$PROJECT_DIR"
resolve_backend   # no-op if step 3 already resolved it; matters for --app-only/--no-bench

ensure_env

if [[ ! -x "$VENV/bin/python" ]]; then
  if [[ "$USE_UV" == 1 ]]; then log "Creating venv with uv…"; uv venv "$VENV" >/dev/null
  else log "Creating venv with python3 -m venv…"; python3 -m venv "$VENV"; fi
fi
PY="$VENV/bin/python"

TARGET="."; [[ "$DEV" == 1 ]] && TARGET=".[dev]"
log "Installing the agent (editable: $TARGET)…"
if [[ "$USE_UV" == 1 ]]; then
  uv pip install --python "$PY" -e "$TARGET" >/dev/null
else
  "$PY" -m pip install --upgrade pip >/dev/null
  "$PY" -m pip install -e "$TARGET" >/dev/null
fi
"$PY" -c "import app.main" >/dev/null 2>&1 && log "Agent imports OK." || die "the agent failed to import after install."

# Offer to wire the user's Claude subscription as the LLM provider (consent-first; skips itself
# without a TTY). Best-effort: a declined/failed setup must never fail the install.
if [[ "$NO_LLM_SETUP" != 1 ]]; then
  step "LLM provider — wire your Claude plan (optional)"
  bash "$PROJECT_DIR/scripts/setup-claude-plan.sh" \
    || warn "Claude-plan setup didn't complete — run ./scripts/setup-claude-plan.sh anytime."
fi

have() { command -v "$1" >/dev/null 2>&1 && printf 'present' || printf 'MISSING'; }
step "Summary"
if [[ "$APP_ONLY" != 1 ]]; then
  printf '  client toolchain : kubectl=%s helm=%s helmfile=%s kustomize=%s yq=%s\n' \
    "$(have kubectl)" "$(have helm)" "$(have helmfile)" "$(have kustomize)" "$(have yq)"
  printf '  benchmark CLI    : %s  (%s/.venv)\n' \
    "$([[ -x "$BENCH_REPO/.venv/bin/llmdbenchmark" ]] && echo present || echo 'in repo .venv — activate to use')" "$BENCH_REPO"
  [[ "$PREREQS" == 1 ]] && printf '  host prereqs     : docker=%s kind=%s\n' "$(have docker)" "$(have kind)"
fi
printf '  agent venv       : %s\n' "$VENV"
printf '\n'
log "Done. Start the agent with:  ./scripts/run.sh   (then open http://127.0.0.1:8000)"
[[ "$PREREQS" != 1 && "$APP_ONLY" != 1 ]] && \
  log "To benchmark on a local kind cluster you'll also need Docker + kind — re-run with --prereqs (needs passwordless sudo), or let the agent install them on demand."
trap - EXIT
