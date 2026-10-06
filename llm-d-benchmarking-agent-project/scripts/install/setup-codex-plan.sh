#!/usr/bin/env bash
# Connect the local assistant to Codex using a ChatGPT subscription.
# Usage: ./scripts/install/setup-codex-plan.sh [--device-auth]
set -euo pipefail
case "${1:-}" in
  -h|--help) sed -n '2,3p' "$0"; exit 0 ;;
  ""|--device-auth) ;;
  *) echo "Usage: $0 [--device-auth]" >&2; exit 2 ;;
esac
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source scripts/_env.sh
log() { printf '%s\n' "$*"; }
ensure_env
if [[ ! -x .venv/bin/python ]]; then
  echo "Run uv sync first." >&2
  exit 1
fi
CODEX_BIN="$(read_env CODEX_CLI_PATH)"
CODEX_BIN="${CODEX_BIN:-$(.venv/bin/python -c 'from codex_cli_bin import bundled_codex_path; print(bundled_codex_path())')}"
if ! "$CODEX_BIN" login status 2>&1 | grep -q 'Logged in using ChatGPT'; then
  "$CODEX_BIN" login "$@"
fi
if ! "$CODEX_BIN" login status 2>&1 | grep -q 'Logged in using ChatGPT'; then
  echo "A ChatGPT login is required. API-key login is not used by this integration." >&2
  exit 1
fi
set_env_var LLM_PROVIDER codex-sdk
set_env_var CODEX_MODEL "${CODEX_MODEL:-gpt-6.1-sol}"
set_env_var CODEX_EFFORT "${CODEX_EFFORT:-medium}"
log "Codex is connected through ChatGPT. Start the assistant with ./scripts/run.sh"
