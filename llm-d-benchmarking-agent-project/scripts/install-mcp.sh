#!/usr/bin/env bash
# install-mcp.sh — one interactive command to install the llm-d-bench MCP server and register it
# with Claude Code (the CLI).
#
# It does everything end-to-end:
#   1. fetches the project (if you ran it via curl outside a checkout)
#   2. clones the read-only sibling repos (llm-d / llm-d-benchmark / llm-d-skills)
#   3. builds a .venv and installs the agent (`pip install -e .` → the `llm-d-bench-mcp` command)
#   4. configures the claude-agent-sdk provider (no API key — uses your local `claude` login) + writes .env
#   5. registers the server with Claude Code (or just prints the config for you to paste)
#
# Scope (for now): the only verified path is the claude-agent-sdk provider + the Claude Code CLI
# client. Other providers (anthropic, openai) and clients (Claude Desktop, Cursor, VS Code, Codex
# CLI) are planned for a future release — see docs/MCP.md.
#
# Usage:
#   bash <(curl -fsSL https://raw.githubusercontent.com/TalBenAmii/llm-d-benchmarking-agent/main/llm-d-benchmarking-agent-project/scripts/install-mcp.sh)
#   ./scripts/install-mcp.sh            # same script, run from inside a checkout
#   ./scripts/install-mcp.sh -h
#
# Env overrides:
#   INSTALL_DIR=/path   where to clone the project in curl-bootstrap mode (default: ~/llm-d-benchmarking-agent)
#   REPOS_DIR=/path     where the sibling repos live (default: the project's parent dir)
#
# Transport is stdio / local single-user (the server runs on YOUR machine against YOUR kubeconfig);
# there is no network/remote mode. See docs/MCP.md for the security model and manual config.
set -euo pipefail

# ── Logging (matches install.sh house style) ──────────────────────────────
log()  { printf '\033[35m▸\033[0m %s\n' "$*"; }            # llm-d purple bullet
step() { printf '\n\033[1;35m━━ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[install-mcp] %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31m[install-mcp] ERROR: %s\033[0m\n' "$*" >&2; exit 1; }
trap 'rc=$?; [[ $rc -ne 0 ]] && printf "\n\033[1;31m[install-mcp] aborted (exit %s).\033[0m Fix the issue above and re-run — the script is idempotent.\n" "$rc" >&2' EXIT

case "${1:-}" in -h|--help) sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; trap - EXIT; exit 0 ;; esac

# ── Interactive reads, robust to both `curl | bash` and `bash <(curl …)` ───
# Prefer /dev/tty so prompts work even when stdin is the curl pipe; fall back to stdin.
TTY=/dev/tty; { [[ -r $TTY && -w $TTY ]]; } 2>/dev/null || TTY=""
ask() {  # $1 prompt, $2 default → echoes the answer (or default if blank / non-interactive)
  local ans=""
  if [[ -n "$TTY" ]]; then printf '\033[36m?\033[0m %s ' "$1" >"$TTY"; IFS= read -r ans <"$TTY" || ans=""
  else IFS= read -r ans || ans=""; fi
  printf '%s' "${ans:-$2}"
}

# ── Locate the project; bootstrap-clone if we were piped in from outside one ─
find_project_root() {
  local d="$PWD"
  while [[ "$d" != "/" && -n "$d" ]]; do
    [[ -f "$d/app/mcp/__main__.py" ]] && { printf '%s' "$d"; return 0; }
    [[ -f "$d/llm-d-benchmarking-agent-project/app/mcp/__main__.py" ]] && { printf '%s' "$d/llm-d-benchmarking-agent-project"; return 0; }
    d="$(dirname "$d")"
  done
  local sd; sd="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")/.." 2>/dev/null && pwd || true)"   # script lives in scripts/
  [[ -n "$sd" && -f "$sd/app/mcp/__main__.py" ]] && { printf '%s' "$sd"; return 0; }
  return 1
}

PROJECT_DIR="$(find_project_root || true)"
if [[ -z "$PROJECT_DIR" ]]; then
  [[ "${_MCP_BOOTSTRAPPED:-0}" == 1 ]] && die "could not locate the project after cloning (bootstrap loop)."
  command -v git >/dev/null 2>&1 || die "git is required to fetch the project — install git and re-run."
  INSTALL_DIR="${INSTALL_DIR:-$HOME/llm-d-benchmarking-agent}"
  if [[ -d "$INSTALL_DIR/.git" ]]; then
    log "Project already cloned at $INSTALL_DIR — reusing it."
  else
    step "Fetching the project → $INSTALL_DIR"
    git clone "https://github.com/TalBenAmii/llm-d-benchmarking-agent" "$INSTALL_DIR"
  fi
  PROJECT_DIR="$INSTALL_DIR/llm-d-benchmarking-agent-project"
  [[ -f "$PROJECT_DIR/scripts/install-mcp.sh" ]] || die "cloned repo is missing $PROJECT_DIR/scripts/install-mcp.sh"
  export _MCP_BOOTSTRAPPED=1
  exec bash "$PROJECT_DIR/scripts/install-mcp.sh" "$@"   # re-run on-disk so paths resolve normally
fi

cd "$PROJECT_DIR"
REPOS_DIR="${REPOS_DIR:-$(dirname "$PROJECT_DIR")}"   # sibling repos live next to the project
VENV="$PROJECT_DIR/.venv"
SERVER_NAME="llm-d-bench"
log "Project: $PROJECT_DIR"

# ── Privilege helper (root → no sudo; else sudo if present) ───────────────
SUDO=""; if [[ "$(id -u)" -ne 0 ]] && command -v sudo >/dev/null 2>&1; then SUDO="sudo"; fi

# ── Step 1: base tools (git, curl) ────────────────────────────────────────
step "Prerequisites"
ensure_tool() {  # $1 = command name
  command -v "$1" >/dev/null 2>&1 && return 0
  if command -v apt-get >/dev/null 2>&1; then
    log "Installing $1…"; $SUDO apt-get update -y >/dev/null 2>&1 || true
    $SUDO apt-get install -y "$1" ca-certificates >/dev/null 2>&1 || true
  fi
  command -v "$1" >/dev/null 2>&1 || die "$1 is required but could not be installed automatically — install it and re-run."
}
ensure_tool git
ensure_tool curl

# Find a Python >=3.11 interpreter.
PYBIN=""
for c in python3.13 python3.12 python3.11 python3; do
  command -v "$c" >/dev/null 2>&1 || continue
  v="$("$c" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo 0.0)"
  if [[ "${v%%.*}" -eq 3 && "${v##*.}" -ge 11 ]]; then PYBIN="$c"; break; fi
done
[[ -n "$PYBIN" ]] || die "Python >=3.11 is required and none was found. Install python3.11+ (e.g. 'apt install python3.11 python3.11-venv') and re-run."
log "Python: $PYBIN ($("$PYBIN" -V 2>&1))"

# Pick a venv backend: prefer uv; else a python3 that can build a venv; else bootstrap uv.
if command -v uv >/dev/null 2>&1; then
  USE_UV=1
elif "$PYBIN" -c 'import ensurepip' >/dev/null 2>&1; then
  USE_UV=0
else
  warn "python cannot create virtualenvs here (python3-venv/ensurepip missing) — bootstrapping uv."
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 || die "uv bootstrap failed — install python3-venv and re-run."
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  command -v uv >/dev/null 2>&1 || die "uv bootstrapped but not on PATH — add ~/.local/bin to PATH and re-run."
  USE_UV=1
fi
log "venv backend: $([[ "$USE_UV" == 1 ]] && echo uv || echo 'python3 -m venv')"

# ── Step 2: clone the read-only sibling repos (the server reads them at runtime) ──
step "Sibling repos (read-only, under $REPOS_DIR)"
clone_if_missing() {  # $1 = repo name, $2 = github owner (org)
  local name="$1" owner="$2" dest="$REPOS_DIR/$1"
  if [[ -d "$dest" && -n "$(ls -A "$dest" 2>/dev/null)" ]]; then
    log "$name present — skipping clone."
  else
    log "Cloning $name → $dest"; git clone --depth 1 "https://github.com/$owner/$name" "$dest"
  fi
}
# llm-d + llm-d-benchmark live under the llm-d org; the skills library is in llm-d-incubation.
clone_if_missing llm-d           llm-d
clone_if_missing llm-d-benchmark llm-d
clone_if_missing llm-d-skills    llm-d-incubation

# ── Step 3: venv + editable install (gives us the `llm-d-bench-mcp` command) ──
step "Install the agent (.venv + pip install -e .)"
if [[ ! -x "$VENV/bin/python" ]]; then
  if [[ "$USE_UV" == 1 ]]; then log "Creating venv with uv…"; uv venv --python "$PYBIN" "$VENV" >/dev/null
  else log "Creating venv with python3 -m venv…"; "$PYBIN" -m venv "$VENV"; fi
fi
PY="$VENV/bin/python"
if [[ "$USE_UV" == 1 ]]; then
  uv pip install --python "$PY" -e . >/dev/null
else
  "$PY" -m pip install --upgrade pip >/dev/null
  "$PY" -m pip install -e . >/dev/null
fi
"$PY" -c "import app.mcp" >/dev/null 2>&1 || die "the MCP server failed to import after install."
log "Installed. The agent imports OK."

# ── Step 4: configure the LLM provider (claude-agent-sdk) and write .env ──
# shellcheck source-path=SCRIPTDIR/..
# shellcheck source=scripts/_env.sh
source "$PROJECT_DIR/scripts/_env.sh"
ensure_env   # create .env from .env.example if missing

set_env_var() {  # $1 KEY  $2 VALUE — replace-or-append in .env (pure bash; values printf'd verbatim)
  local key="$1" val="$2" f="$PROJECT_DIR/.env" tmp
  touch "$f"; tmp="$(mktemp)"
  grep -vE "^${key}=" "$f" >"$tmp" 2>/dev/null || true
  printf '%s=%s\n' "$key" "$val" >>"$tmp"
  mv "$tmp" "$f"
}

step "LLM provider"
set_env_var LLM_PROVIDER claude-agent-sdk
log "Using claude-agent-sdk — no API key. Make sure the 'claude' CLI is installed and logged in."
log "(Other providers — anthropic, openai — are planned for a future release.)"

HF_TOKEN_VAL="$(ask 'Optional HF_TOKEN for gated model deploys (blank to skip):' '')"
[[ -n "$HF_TOKEN_VAL" ]] && set_env_var HF_TOKEN "$HF_TOKEN_VAL"

# ── Resolve the launch command Claude Code will spawn ─────────────────────
if [[ -x "$VENV/bin/llm-d-bench-mcp" ]]; then
  CMD_ARGV=("$VENV/bin/llm-d-bench-mcp"); CMD_DISPLAY="$VENV/bin/llm-d-bench-mcp"
else
  CMD_ARGV=("$PY" -m app.mcp); CMD_DISPLAY="$PY -m app.mcp"
fi

# ── Claude Code (CLI) registration + the manual snippet (for 'print only') ──
print_claude_code_snippet() {
  echo; echo "── Claude Code (CLI) ───────────────────────────────"
  if [[ -n "$HF_TOKEN_VAL" ]]; then
    printf '  claude mcp add %s -s user -e "HF_TOKEN=%s" -- %s\n' "$SERVER_NAME" "$HF_TOKEN_VAL" "$CMD_DISPLAY"
  else
    printf '  claude mcp add %s -s user -- %s\n' "$SERVER_NAME" "$CMD_DISPLAY"
  fi
}
register_claude_code() {
  if ! command -v claude >/dev/null 2>&1; then
    warn "The 'claude' CLI is not on PATH — run this later:"; print_claude_code_snippet; return 0
  fi
  local scope; scope="$(ask 'Scope? [local|user|project] (default: user):' user)"
  local args=(mcp add "$SERVER_NAME" -s "$scope"); [[ -n "$HF_TOKEN_VAL" ]] && args+=(-e "HF_TOKEN=$HF_TOKEN_VAL")
  args+=(-- "${CMD_ARGV[@]}")
  if claude "${args[@]}"; then log "Registered with Claude Code (scope: $scope). Check it with 'claude mcp list' or '/mcp' in a session."
  else warn "'claude mcp add' failed — register manually:"; print_claude_code_snippet; fi
}

# ── Step 5: register with Claude Code ─────────────────────────────────────
step "Register with Claude Code"
echo "  1) Claude Code (CLI) — register it for you"
echo "  2) Just print the config — make no changes  (default)"
echo "  0) Skip — I'll wire it up myself"
log "(Claude Desktop, Cursor, VS Code, and Codex CLI are planned for a future release.)"
case "$(ask 'Choice [1/2/0]:' 2 | tr -d '[:space:]')" in
  1) register_claude_code ;;
  0) log "Skipping client registration." ;;
  *) print_claude_code_snippet ;;
esac

# ── Summary ───────────────────────────────────────────────────────────────
step "Done"
log "Launch command : $CMD_DISPLAY"
log "Smoke-test it  : npx @modelcontextprotocol/inspector $CMD_DISPLAY   (lists 35 tools, 5 prompts, knowledge resources)"
log "Provider/config: $PROJECT_DIR/.env"
log "The server is stdio/local — it runs on this machine against your kubeconfig. Advisory tools work"
log "with no cluster; deploy/run tools need a reachable cluster. Mutations are approved in YOUR client's"
log "own tool-permission prompt. Full details: docs/MCP.md"
trap - EXIT
