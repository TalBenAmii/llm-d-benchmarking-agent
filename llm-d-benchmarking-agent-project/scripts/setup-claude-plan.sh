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
# the claude-agent-sdk provider does at runtime. Runs standalone or as install.sh's last
# step (skip there with --no-llm-setup). Without a usable terminal (CI / scripted installs)
# it skips cleanly and changes nothing.
#
# Usage:
#   ./scripts/setup-claude-plan.sh
#   ./scripts/setup-claude-plan.sh -h | --help
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # project root (this script lives in scripts/)

log()  { printf '\033[35m▸\033[0m %s\n' "$*"; }            # llm-d purple bullet
warn() { printf '\033[1;33m[setup-claude-plan] %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31m[setup-claude-plan] ERROR: %s\033[0m\n' "$*" >&2; exit 1; }

case "${1:-}" in -h|--help) sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;; esac

# Prompts read /dev/tty so they work even when stdin is a pipe (curl | bash, install.sh).
# Without a usable TTY there is nobody to ask — skip cleanly, never hang a scripted install.
# The probe must actually OPEN /dev/tty: `[[ -r ]]`/`[[ -w ]]` only check permission bits and
# stay true in a session with no controlling terminal (CI/setsid), where the open fails — and
# every later prompt would then silently take its default instead of asking.
TTY=/dev/tty; { : <"$TTY" >"$TTY"; } 2>/dev/null || TTY=""
if [[ -z "$TTY" ]]; then
  log "No interactive terminal — skipping Claude-plan setup. Run ./scripts/setup-claude-plan.sh later."
  exit 0
fi
ask() {  # $1 prompt, $2 default → echoes the answer (or default if blank)
  local ans=""
  printf '\033[36m?\033[0m %s ' "$1" >"$TTY"; IFS= read -r ans <"$TTY" || ans=""
  printf '%s' "${ans:-$2}"
}

# shellcheck source-path=SCRIPTDIR/..
# shellcheck source=scripts/_env.sh
source "scripts/_env.sh"   # provides ensure_env + read_env + set_env_var
ensure_env

# ── Consent first — context-aware default ──────────────────────────────────
# A fresh .env (example default + no key) defaults to Yes; a deliberately configured provider
# (a key actually set, or already on the plan) is shown so a re-run can't silently clobber it.
CUR_PROVIDER="$(read_env LLM_PROVIDER)"; CUR_PROVIDER="${CUR_PROVIDER:-anthropic}"
CUR_KEY=""
case "$CUR_PROVIDER" in
  openai|openai-compatible|vllm) CUR_KEY="$(read_env OPENAI_API_KEY)" ;;
  claude-agent-sdk|agent-sdk|claude-max) ;;   # already the plan route — re-running just re-verifies
  *) CUR_KEY="$(read_env ANTHROPIC_API_KEY)" ;;
esac
if [[ -n "$CUR_KEY" ]]; then
  log "Current provider in .env: $CUR_PROVIDER (API key set)."
  YN="$(ask "Switch the assistant from $CUR_PROVIDER to your Claude subscription (no API key)? [y/N]:" N)"
else
  YN="$(ask "Use your Claude subscription (Pro/Max plan) as the assistant's LLM — no API key? [Y/n]:" Y)"
fi
case "$YN" in [Yy]*) ;; *) log "Keeping the current provider — nothing changed."; exit 0 ;; esac

# ── The `claude` CLI (the plan's credential holder) ────────────────────────
if ! command -v claude >/dev/null 2>&1; then
  warn "The 'claude' CLI is not installed — the plan route authenticates through it."
  YN="$(ask 'Install it now (official installer, no sudo, → ~/.local/bin/claude)? [Y/n]:' Y)"
  case "$YN" in
    [Yy]*)
      log "Installing the claude CLI…"
      curl -fsSL https://claude.ai/install.sh | bash \
        || die "the claude CLI installer failed (see above) — install it manually and re-run this script."
      export PATH="$HOME/.local/bin:$PATH"
      command -v claude >/dev/null 2>&1 \
        || die "installed, but 'claude' is not on PATH — open a new shell (or add ~/.local/bin to PATH) and re-run this script."
      ;;
    *)
      log "Skipping — nothing changed. Install it later with:  curl -fsSL https://claude.ai/install.sh | bash"
      log "…then re-run ./scripts/setup-claude-plan.sh"
      exit 0 ;;
  esac
fi

# ── Login state (claude auth status --json) ────────────────────────────────
# One status call cached in AUTH_JSON; python3 parses it (guaranteed by install; the project
# venv is the fallback). Fields: loggedIn → "true"/"", email/subscriptionType → value/"".
PYJSON="python3"; command -v python3 >/dev/null 2>&1 || PYJSON=".venv/bin/python"
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
    YN="$(ask 'Wire it anyway? [y/N]:' N)"
    case "$YN" in [Yy]*) ;; *) log "Stopped — nothing changed."; exit 0 ;; esac ;;
esac

# ── Model choice (AGENT_SDK_MODEL; effort is set to high behind the scenes) ─
echo
log "Which Claude model should the assistant use?"
echo "  1) claude-sonnet-4-6  — recommended · balanced speed & quality   (default)"
echo "  2) claude-haiku-4-5   — fastest · lightest on plan limits · weaker agent"
echo "  3) claude-opus-4-8    — strongest · slowest · heaviest on plan limits"
echo "  4) another model id   — type your own"
case "$(ask 'Choice [1/2/3/4]:' 1 | tr -d '[:space:]')" in
  2) MODEL=claude-haiku-4-5 ;;
  3) MODEL=claude-opus-4-8 ;;
  4) MODEL="$(ask 'Model id:' claude-sonnet-4-6)" ;;
  *) MODEL=claude-sonnet-4-6 ;;
esac

set_env_var LLM_PROVIDER claude-agent-sdk
set_env_var AGENT_SDK_MODEL "$MODEL"
set_env_var AGENT_SDK_EFFORT high
log "Wrote .env: LLM_PROVIDER=claude-agent-sdk · AGENT_SDK_MODEL=$MODEL · AGENT_SDK_EFFORT=high"

# ── Verify end-to-end: one tiny inference on the plan ──────────────────────
# Mirrors the runtime provider: any stray API key is blanked so the call runs on the
# subscription, and it runs from an empty dir so no project context pads the test prompt.
log "Verifying with one tiny test call on your plan ($MODEL)…"
PING_DIR="$(mktemp -d)"
PING_RC=0
PING_OUT="$(cd "$PING_DIR" && timeout 120 env ANTHROPIC_API_KEY= ANTHROPIC_AUTH_TOKEN= \
  claude -p --model "$MODEL" --no-session-persistence 'Reply with exactly: ok' 2>&1)" || PING_RC=$?
rm -rf "$PING_DIR"
if [[ "$PING_RC" -eq 0 ]]; then
  log "✓ Claude plan wired — the assistant runs on $MODEL via your subscription."
  log "Start it with:  ./scripts/run.sh   (then open http://127.0.0.1:8000)"
else
  warn "The test call FAILED — output:"
  printf '%s\n' "$PING_OUT" >&2
  die "the plan route isn't working yet. Usual causes: the login has no subscription, the model id isn't available on your plan, or the CLI needs an update ('claude update'). .env keeps the settings above — fix the cause and re-run this script."
fi
