#!/usr/bin/env bash
# Start the llm-d Benchmarking Assistant locally.
#
# Sets up a virtualenv (uv if available, else python3 -m venv), installs the app,
# ensures a .env exists, then launches the FastAPI/uvicorn server. HOST/PORT are
# read from .env (defaults 127.0.0.1:8000); PORT can be overridden with --port.
#
#   ./scripts/run.sh                  # start with autoreload on http://127.0.0.1:8000
#   ./scripts/run.sh --open           # ...and open it in a browser
#   ./scripts/run.sh --port 9000      # override the port
#   ./scripts/run.sh --no-reload      # disable autoreload
#   ./scripts/run.sh --reinstall      # force-reinstall dependencies first
#
# The LLM credential lives outside git: an API key in .env, or — with LLM_PROVIDER=
# claude-agent-sdk — your local `claude` CLI login (wired by scripts/setup-claude-plan.sh).
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # project root (this script lives in scripts/)
VENV=".venv"
PY="$VENV/bin/python"

RELOAD=1
OPEN=0
REINSTALL=0
PORT_OVERRIDE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-reload)  RELOAD=0; shift ;;
    --open)       OPEN=1; shift ;;
    --reinstall)  REINSTALL=1; shift ;;
    --port)       PORT_OVERRIDE="${2:?--port needs a value}"; shift 2 ;;
    -h|--help)    sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "run.sh: unknown option '$1' (try --help)" >&2; exit 2 ;;
  esac
done

log() { printf '\033[35m▸\033[0m %s\n' "$*"; }
# shellcheck source-path=SCRIPTDIR/..
# shellcheck source=scripts/_env.sh
source "scripts/_env.sh"   # cwd is the project root (cd above); provides ensure_env + read_env

ensure_env

if [[ ! -x "$PY" ]]; then
  if command -v uv >/dev/null 2>&1; then
    log "Creating virtualenv with uv…"
    uv venv "$VENV" >/dev/null
  else
    log "Creating virtualenv with python3 -m venv…"
    python3 -m venv "$VENV"
  fi
  REINSTALL=1
fi

# Install if forced, or if the app can't be imported yet.
if [[ "$REINSTALL" == 1 ]] || ! "$PY" -c "import uvicorn, app.main" >/dev/null 2>&1; then
  log "Installing dependencies (editable install)…"
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$PY" -e . >/dev/null
  else
    "$PY" -m pip install --upgrade pip >/dev/null
    "$PY" -m pip install -e . >/dev/null
  fi
fi

HOST="$(read_env HOST)"; HOST="${HOST:-127.0.0.1}"
PORT="${PORT_OVERRIDE:-$(read_env PORT)}"; PORT="${PORT:-8000}"

# Credential note per provider route — warn, never block: the UI must serve either way.
PROVIDER="$(read_env LLM_PROVIDER)"; PROVIDER="${PROVIDER:-anthropic}"
case "$PROVIDER" in
  claude-agent-sdk|agent-sdk|claude-max)
    # Plan route: the credential is the `claude` CLI's login, so a logged-out day-2 start
    # would otherwise surface only as an error at the first chat message.
    if ! command -v claude >/dev/null 2>&1; then
      log "Note: LLM_PROVIDER=$PROVIDER but the 'claude' CLI is not on PATH — the UI loads, chat won't. Run ./scripts/setup-claude-plan.sh"
    elif ! claude auth status --json 2>/dev/null | grep -qE '"loggedIn":[[:space:]]*true'; then
      log "Note: the 'claude' CLI is not logged in — the UI loads, chat won't. Run ./scripts/setup-claude-plan.sh (or 'claude auth login')."
    fi ;;
  *)
    if [[ "$PROVIDER" == openai* || "$PROVIDER" == vllm ]]; then KEY="$(read_env OPENAI_API_KEY)"; else KEY="$(read_env ANTHROPIC_API_KEY)"; fi
    if [[ -z "$KEY" ]]; then
      log "Note: no ${PROVIDER^^} API key in .env — the UI loads, but a live session needs one."
    fi ;;
esac

URL="http://${HOST}:${PORT}"
log "Starting on ${URL}  (provider: ${PROVIDER}, reload: $([[ $RELOAD == 1 ]] && echo on || echo off))"

if [[ "$OPEN" == 1 ]]; then
  ( for _ in $(seq 1 40); do
      if "$PY" -c "import socket,sys; s=socket.socket(); s.settimeout(.3); sys.exit(0 if s.connect_ex(('${HOST}',${PORT}))==0 else 1)" 2>/dev/null; then break; fi
      sleep 0.5
    done
    if command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL"
    elif grep -qi microsoft /proc/version 2>/dev/null; then explorer.exe "$URL" 2>/dev/null || true
    elif command -v open >/dev/null 2>&1; then open "$URL"
    fi ) &
fi

# ── 6. Launch (exec so Ctrl-C stops uvicorn cleanly) ──────────────────────
RELOAD_ARGS=()
[[ "$RELOAD" == 1 ]] && RELOAD_ARGS=(--reload --reload-dir app)   # watch python only; UI is static
exec "$PY" -m uvicorn app.main:app --host "$HOST" --port "$PORT" "${RELOAD_ARGS[@]}"
