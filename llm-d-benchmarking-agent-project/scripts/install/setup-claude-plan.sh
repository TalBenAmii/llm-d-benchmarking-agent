#!/usr/bin/env bash
# setup-claude-plan.sh — wire your Claude subscription (Pro/Max plan) into the assistant.
#
# Interactive and consent-first: it asks before changing anything, then
#   1. checks the `claude` CLI is installed (offers the official installer if not)
#   2. checks you are logged in (`claude auth status`; runs `claude auth login` if not)
#   3. lets you pick the model (sonnet recommended) and writes .env:
#        LLM_PROVIDER=claude-agent-sdk · AGENT_SDK_MODEL=<choice> · AGENT_SDK_EFFORT=high
#   4. verifies end-to-end with ONE tiny test call on your plan (a few tokens)
#
# No API key is involved — the app authenticates through the CLI's own login, exactly like
# the claude-agent-sdk provider does at runtime. Runs standalone or as install_local.sh's last
# step (skip there with --no-llm-setup). Without a usable terminal (CI / scripted installs)
# it skips cleanly and changes nothing.
#
# Usage:
#   ./scripts/install/setup-claude-plan.sh
#   ./scripts/install/setup-claude-plan.sh -h | --help
set -euo pipefail

case "${1:-}" in -h|--help) sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;; esac   # before the cd — $0 may be relative

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"   # project root (this script lives in scripts/install/)

log()  { printf '\033[35m▸\033[0m %s\n' "$*"; }            # llm-d purple bullet
warn() { printf '\033[1;33m[setup-claude-plan] %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31m[setup-claude-plan] ERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# shellcheck source-path=SCRIPTDIR/../..
# shellcheck source=scripts/_env.sh
source "scripts/_env.sh"   # provides _tty_interactive + ensure_env + read_env + set_env_var + confirm/menu_select + ensure_claude_cli

# Prompts read /dev/tty so they work even when stdin is a pipe (curl | bash, install_local.sh). Without a
# usable terminal there is nobody to ask — skip cleanly, never hang a scripted install. _tty_interactive
# (shared with the menu helpers, one definition) is the single source of truth: it requires /dev/tty to
# be openable AND our process to be its FOREGROUND process group, so a background/non-foreground job
# (`./scripts/install/install_local.sh &`, nohup, WSL/ssh non-interactive exec) — which would SIGTTIN-stop or block
# forever on the first read — takes the clean-skip path instead of prompting.
if _tty_interactive; then TTY=/dev/tty; else
  log "No interactive terminal — skipping Claude-plan setup. Run ./scripts/install/setup-claude-plan.sh later."
  exit 0
fi
ask() {  # $1 prompt, $2 default → echoes the answer (or default if blank)
  local ans=""
  printf '\033[36m?\033[0m %s ' "$1" >"$TTY"; IFS= read -r ans <"$TTY" || ans=""
  printf '%s' "${ans:-$2}"
}
ensure_env

# ── Consent first — context-aware default ──────────────────────────────────
# A fresh .env (example default + no key) defaults to Yes; a deliberately configured provider
# (a key actually set, or already on the plan) is shown so a re-run can't silently clobber it.
# Lower-cased to match the app's own dispatch (get_provider lower-cases LLM_PROVIDER too).
CUR_PROVIDER="$(read_env LLM_PROVIDER | tr '[:upper:]' '[:lower:]')"; CUR_PROVIDER="${CUR_PROVIDER:-anthropic}"
CUR_KEY=""
case "$CUR_PROVIDER" in
  openai|openai-compatible|vllm) CUR_KEY="$(read_env OPENAI_API_KEY)" ;;
  claude-agent-sdk|agent-sdk|claude-max) ;;   # already the plan route — re-running just re-verifies
  *) CUR_KEY="$(read_env ANTHROPIC_API_KEY)" ;;
esac
WIRE=1
if [[ -n "$CUR_KEY" ]]; then
  log "Current provider in .env: $CUR_PROVIDER (API key set)."
  confirm "Switch the assistant from $CUR_PROVIDER to your Claude subscription (no API key)?" N || WIRE=0
else
  confirm "Use your Claude subscription (Pro/Max plan) as the assistant's LLM — no API key?" Y || WIRE=0
fi
[[ "$WIRE" == 1 ]] || { log "Keeping the current provider — nothing changed."; exit 0; }

# ── The `claude` CLI (the plan's credential holder) ────────────────────────
CLI_RC=0; ensure_claude_cli || CLI_RC=$?
case "$CLI_RC" in
  0) ;;
  2) log "Skipping — nothing changed. Install it later with:  curl -fsSL https://claude.ai/install.sh | bash"
     log "…then re-run ./scripts/install/setup-claude-plan.sh"; exit 0 ;;
  *) die "the claude CLI could not be installed (see above) — install it manually and re-run this script." ;;
esac

# ── Login state (claude auth status --json) ────────────────────────────────
# One status call cached in AUTH_JSON; python parses it. Validated UP FRONT: a silently missing
# interpreter would make every auth_field read "" — indistinguishable from "not logged in" —
# and walk a logged-in user into a re-login that then "fails" for the wrong reason.
if command -v python3 >/dev/null 2>&1; then PYJSON="python3"
elif [[ -x .venv/bin/python ]]; then PYJSON=".venv/bin/python"
else die "python3 is required (to read 'claude auth status') — install it, or run ./scripts/install/install_local.sh first, then re-run this script."
fi
# Fields: loggedIn → "true"/"", email/subscriptionType → value/"".
refresh_auth() { AUTH_JSON="$(claude auth status --json 2>/dev/null || true)"; }
auth_field() {
  printf '%s' "$AUTH_JSON" | "$PYJSON" -c '
import json, sys
v = json.load(sys.stdin).get(sys.argv[1])
print("true" if v is True else "" if v in (None, False) else v)' "$1" 2>/dev/null || true
}

refresh_auth
if [[ "$(auth_field loggedIn)" != "true" ]]; then
  log "You're not logged in — opening the Claude sign-in flow…"
  claude auth login <"$TTY" >"$TTY" 2>&1 \
    || die "login did not complete — re-run this script to try again (nothing was changed)."
  refresh_auth
  [[ "$(auth_field loggedIn)" == "true" ]] \
    || die "still not logged in after the sign-in flow — run 'claude auth login' manually, then re-run this script."
fi
EMAIL="$(auth_field email)"; PLAN="$(auth_field subscriptionType)"
log "Logged in as ${EMAIL:-unknown}${PLAN:+ ($PLAN plan)}."

# The plan route needs a subscription — a bare API-console login carries no plan inference.
case "$PLAN" in
  pro|max|team|enterprise) ;;
  *)
    warn "No Claude subscription is visible on this login (subscriptionType: '${PLAN:-none}')."
    warn "Without a Pro/Max-style plan the first chat may fail — or fall back to metered API billing."
    confirm 'Wire it anyway?' N || { log "Stopped — nothing changed."; exit 0; } ;;
esac

# ── Model choice (AGENT_SDK_MODEL; effort is set to high behind the scenes) ─
echo
MODEL_IDX="$(menu_select 'Which Claude model should the assistant use?' 0 \
  'claude-sonnet-4-6  — recommended · balanced speed & quality' \
  'claude-haiku-4-5   — fastest · lightest on plan limits · weaker agent' \
  'claude-opus-4-8    — strongest · slowest · heaviest on plan limits' \
  'another model id   — type your own')"
case "$MODEL_IDX" in
  1) MODEL=claude-haiku-4-5 ;;
  2) MODEL=claude-opus-4-8 ;;
  3) MODEL="$(ask 'Model id:' claude-sonnet-4-6)" ;;
  *) MODEL=claude-sonnet-4-6 ;;
esac

set_env_var LLM_PROVIDER claude-agent-sdk
set_env_var AGENT_SDK_MODEL "$MODEL"
set_env_var AGENT_SDK_EFFORT high
log "Wrote .env: LLM_PROVIDER=claude-agent-sdk · AGENT_SDK_MODEL=$MODEL · AGENT_SDK_EFFORT=high"

# ── Verify end-to-end: one tiny inference on the plan ──────────────────────
# Mirrors the runtime provider: any stray API key is blanked so the call runs on the
# subscription, and it runs from an empty dir so no project context pads the test prompt.
# `timeout` guards a hung CLI but is optional — stock macOS ships without coreutils, and a
# missing guard must not turn a working plan into a reported failure.
log "Verifying with one tiny test call on your plan ($MODEL)…"
TIMEOUT=(); command -v timeout >/dev/null 2>&1 && TIMEOUT=(timeout 120)
PING_DIR="$(mktemp -d)"
PING_RC=0
# ${TIMEOUT[@]+…}: empty-array expansion trips `set -u` on bash <4.4 (stock macOS is 3.2).
PING_OUT="$(cd "$PING_DIR" && ${TIMEOUT[@]+"${TIMEOUT[@]}"} env ANTHROPIC_API_KEY= ANTHROPIC_AUTH_TOKEN= \
  claude -p --model "$MODEL" --no-session-persistence 'Reply with exactly: ok' 2>&1)" || PING_RC=$?
rm -rf "$PING_DIR"
if [[ "$PING_RC" -eq 0 ]]; then
  log "✓ Claude plan wired — the assistant runs on $MODEL via your subscription."
else
  warn "The test call FAILED — output:"
  printf '%s\n' "$PING_OUT" >&2
  die "the plan route isn't working yet. Usual causes: the login has no subscription, the model id isn't available on your plan, or the CLI needs an update ('claude update'). .env keeps the settings above — fix the cause and re-run this script."
fi
