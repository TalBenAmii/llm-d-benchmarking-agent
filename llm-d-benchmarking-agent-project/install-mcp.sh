#!/usr/bin/env bash
# install-mcp.sh — one interactive command to install the llm-d-bench MCP server and wire it
# into your agent client (Claude Code, Claude Desktop, Cursor, VS Code, or OpenAI Codex CLI).
#
# It does everything end-to-end:
#   1. fetches the project (if you ran it via curl outside a checkout)
#   2. clones the read-only sibling repos (llm-d / llm-d-benchmark / llm-d-skills)
#   3. builds a .venv and installs the agent (`pip install -e .` → the `llm-d-bench-mcp` command)
#   4. asks which LLM provider to use and writes your .env
#   5. asks which client(s) to register, and writes their MCP config for you (idempotent, backed up)
#
# Usage:
#   bash <(curl -fsSL https://raw.githubusercontent.com/TalBenAmii/llm-d-benchmarking-agent/main/llm-d-benchmarking-agent-project/install-mcp.sh)
#   ./install-mcp.sh            # same script, run from inside a checkout
#   ./install-mcp.sh -h
#
# Env overrides:
#   INSTALL_DIR=/path   where to clone the project in curl-bootstrap mode (default: ~/llm-d-benchmarking-agent)
#   REPOS_DIR=/path     where the sibling repos live (default: the project's parent dir)
#
# Transport is stdio / local single-user (the server runs on YOUR machine against YOUR kubeconfig);
# there is no network/remote mode. See the repo-root README.md for the security model and manual config.
set -euo pipefail

# ── Logging (matches install.sh house style) ──────────────────────────────
log()  { printf '\033[35m▸\033[0m %s\n' "$*"; }            # llm-d purple bullet
step() { printf '\n\033[1;35m━━ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[install-mcp] %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31m[install-mcp] ERROR: %s\033[0m\n' "$*" >&2; exit 1; }
trap 'rc=$?; [[ $rc -ne 0 ]] && printf "\n\033[1;31m[install-mcp] aborted (exit %s).\033[0m Fix the issue above and re-run — the script is idempotent.\n" "$rc" >&2' EXIT

case "${1:-}" in -h|--help) sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'; trap - EXIT; exit 0 ;; esac

# ── Interactive reads, robust to both `curl | bash` and `bash <(curl …)` ───
# Prefer /dev/tty so prompts work even when stdin is the curl pipe; fall back to stdin.
TTY=/dev/tty; { [[ -r $TTY && -w $TTY ]]; } 2>/dev/null || TTY=""
ask() {  # $1 prompt, $2 default → echoes the answer (or default if blank / non-interactive)
  local ans=""
  if [[ -n "$TTY" ]]; then printf '\033[36m?\033[0m %s ' "$1" >"$TTY"; IFS= read -r ans <"$TTY" || ans=""
  else IFS= read -r ans || ans=""; fi
  printf '%s' "${ans:-$2}"
}
ask_secret() {  # $1 prompt → reads without echo
  local ans=""
  if [[ -n "$TTY" ]]; then printf '\033[36m?\033[0m %s ' "$1" >"$TTY"; IFS= read -rs ans <"$TTY" || ans=""; printf '\n' >"$TTY"
  else IFS= read -rs ans || ans=""; fi
  printf '%s' "$ans"
}

# ── Locate the project; bootstrap-clone if we were piped in from outside one ─
find_project_root() {
  local d="$PWD"
  while [[ "$d" != "/" && -n "$d" ]]; do
    [[ -f "$d/app/mcp/__main__.py" ]] && { printf '%s' "$d"; return 0; }
    [[ -f "$d/llm-d-benchmarking-agent-project/app/mcp/__main__.py" ]] && { printf '%s' "$d/llm-d-benchmarking-agent-project"; return 0; }
    d="$(dirname "$d")"
  done
  local sd; sd="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" 2>/dev/null && pwd || true)"
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
  [[ -f "$PROJECT_DIR/install-mcp.sh" ]] || die "cloned repo is missing $PROJECT_DIR/install-mcp.sh"
  export _MCP_BOOTSTRAPPED=1
  exec bash "$PROJECT_DIR/install-mcp.sh" "$@"   # re-run on-disk so paths resolve normally
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

# ── Step 4: pick an LLM provider and write .env ───────────────────────────
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

step "Choose the LLM the agent will think with"
echo "  1) claude-agent-sdk — uses your local 'claude' CLI login, NO API key   (default)"
echo "  2) anthropic        — needs an ANTHROPIC_API_KEY"
echo "  3) openai           — needs an OPENAI_API_KEY (+ optional OPENAI_BASE_URL)"
case "$(ask 'Provider [1/2/3]:' 1)" in
  2) set_env_var LLM_PROVIDER anthropic
     k="$(ask_secret 'ANTHROPIC_API_KEY (paste, hidden):')"
     [[ -n "$k" ]] && set_env_var ANTHROPIC_API_KEY "$k" || warn "No key entered — set ANTHROPIC_API_KEY in $PROJECT_DIR/.env before running." ;;
  3) set_env_var LLM_PROVIDER openai
     k="$(ask_secret 'OPENAI_API_KEY (paste, hidden):')"
     [[ -n "$k" ]] && set_env_var OPENAI_API_KEY "$k" || warn "No key entered — set OPENAI_API_KEY in $PROJECT_DIR/.env before running."
     b="$(ask 'OPENAI_BASE_URL (blank for the default):' '')"; [[ -n "$b" ]] && set_env_var OPENAI_BASE_URL "$b" ;;
  *) set_env_var LLM_PROVIDER claude-agent-sdk
     log "Using claude-agent-sdk — no API key. Make sure the 'claude' CLI is installed and logged in." ;;
esac

HF_TOKEN_VAL="$(ask 'Optional HF_TOKEN for gated model deploys (blank to skip):' '')"
[[ -n "$HF_TOKEN_VAL" ]] && set_env_var HF_TOKEN "$HF_TOKEN_VAL"

# ── Resolve the launch command clients will spawn ─────────────────────────
if [[ -x "$VENV/bin/llm-d-bench-mcp" ]]; then
  CMD_BIN="$VENV/bin/llm-d-bench-mcp"; CMD_ARGS_JSON="[]"; CMD_ARGV=("$CMD_BIN"); CMD_DISPLAY="$CMD_BIN"
else
  CMD_BIN="$PY"; CMD_ARGS_JSON='["-m","app.mcp"]'; CMD_ARGV=("$PY" -m app.mcp); CMD_DISPLAY="$PY -m app.mcp"
fi
if [[ -n "$HF_TOKEN_VAL" ]]; then ENV_JSON="{\"HF_TOKEN\":\"$HF_TOKEN_VAL\"}"; else ENV_JSON="{}"; fi

# ── JSON config merge (Claude Desktop / Cursor / VS Code) — idempotent, backed up ──
json_register() {  # $1 file  $2 topkey  $3 want_type(0/1)  $4 overwrite(0/1) → prints OK/EXISTS/…; rc 0/2/!=0
  MCP_FILE="$1" MCP_TOPKEY="$2" MCP_WANTTYPE="$3" MCP_OVERWRITE="${4:-0}" \
  MCP_NAME="$SERVER_NAME" MCP_CMD="$CMD_BIN" MCP_ARGS="$CMD_ARGS_JSON" MCP_ENV="$ENV_JSON" \
  "$PY" - <<'PYEOF'
import json, os, pathlib
f = pathlib.Path(os.path.expanduser(os.environ["MCP_FILE"]))
topkey, name = os.environ["MCP_TOPKEY"], os.environ["MCP_NAME"]
want_type = os.environ["MCP_WANTTYPE"] == "1"
overwrite = os.environ["MCP_OVERWRITE"] == "1"
data = {}
if f.exists() and f.stat().st_size:
    try:
        data = json.loads(f.read_text())
    except Exception as e:
        print("PARSEFAIL " + str(e)); raise SystemExit(3)
section = data.setdefault(topkey, {})
if not isinstance(section, dict):
    print("BADSECTION"); raise SystemExit(4)
if name in section and not overwrite:
    print("EXISTS"); raise SystemExit(2)
server = {}
if want_type:
    server["type"] = "stdio"
server["command"] = os.environ["MCP_CMD"]
args = json.loads(os.environ["MCP_ARGS"])
if args:
    server["args"] = args
env = json.loads(os.environ["MCP_ENV"])
if env:
    server["env"] = env
section[name] = server
f.parent.mkdir(parents=True, exist_ok=True)
if f.exists():
    bak = f.with_name(f.name + ".bak")
    if not bak.exists():   # keep the user's TRUE original; don't clobber it on a re-run
        import shutil
        shutil.copy2(str(f), str(bak))
f.write_text(json.dumps(data, indent=2) + "\n")
print("OK " + str(f))
PYEOF
}

do_json_register() {  # $1 file  $2 topkey  $3 want_type  $4 label  $5 follow-up note
  # NOTE: `rc=$?` must NOT follow `out="$(…)"` on its own line — under `set -e` a non-zero
  # command-substitution (EXISTS=2 / PARSEFAIL=3) aborts the script before `rc=$?` runs. The
  # `|| rc=$?` form captures the code AND exempts it from `set -e`.
  local file="$1" topkey="$2" wt="$3" label="$4" followup="$5" out rc=0
  out="$(json_register "$file" "$topkey" "$wt" 0 2>&1)" || rc=$?
  if [[ $rc -eq 2 ]]; then
    if [[ "$(ask "$label already has a '$SERVER_NAME' entry. Overwrite? [y/N]:" N)" =~ ^[Yy] ]]; then
      rc=0; out="$(json_register "$file" "$topkey" "$wt" 1 2>&1)" || rc=$?
    else log "Left $label unchanged."; return 0; fi
  fi
  if [[ $rc -eq 0 ]]; then
    log "Registered with $label → ${out#OK }"; [[ -n "$followup" ]] && log "  ↳ $followup"
    return 0
  fi
  warn "Could not edit the $label config ($out) — here's the block to paste yourself:"
  print_json_block "$topkey" "$wt"
}

# ── Snippet printers (for 'print only' and any failed auto-edit) ──────────
print_json_block() {  # $1 topkey  $2 want_type
  MCP_TOPKEY="$1" MCP_WANTTYPE="$2" MCP_NAME="$SERVER_NAME" MCP_CMD="$CMD_BIN" \
  MCP_ARGS="$CMD_ARGS_JSON" MCP_ENV="$ENV_JSON" "$PY" - <<'PYEOF'
import json, os
server = {}
if os.environ["MCP_WANTTYPE"] == "1":
    server["type"] = "stdio"
server["command"] = os.environ["MCP_CMD"]
a = json.loads(os.environ["MCP_ARGS"])
if a: server["args"] = a
e = json.loads(os.environ["MCP_ENV"])
if e: server["env"] = e
print(json.dumps({os.environ["MCP_TOPKEY"]: {os.environ["MCP_NAME"]: server}}, indent=2))
PYEOF
}
print_codex_block() {
  printf '[mcp_servers.llm_d_bench]\ncommand = "%s"\n' "$CMD_BIN"
  [[ "$CMD_ARGS_JSON" != "[]" ]] && printf 'args = ["-m", "app.mcp"]\n'
  [[ -n "$HF_TOKEN_VAL" ]] && printf '\n[mcp_servers.llm_d_bench.env]\nHF_TOKEN = "%s"\n' "$HF_TOKEN_VAL"
  return 0   # else a blank HF_TOKEN makes the trailing `&&` return non-zero → `set -e` aborts the caller
}
print_all_snippets() {
  echo; echo "── Claude Code (CLI) ───────────────────────────────"
  printf '  claude mcp add %s -s user -- %s\n' "$SERVER_NAME" "$CMD_DISPLAY"
  echo; echo "── Claude Desktop / Cursor (claude_desktop_config.json, ~/.cursor/mcp.json) ──"
  print_json_block "mcpServers" 0
  echo; echo "── VS Code (.vscode/mcp.json) ──────────────────────"
  print_json_block "servers" 1
  echo; echo "── OpenAI Codex CLI (~/.codex/config.toml) ─────────"
  print_codex_block
}

# ── Per-client registration ───────────────────────────────────────────────
register_claude_code() {
  if ! command -v claude >/dev/null 2>&1; then
    warn "The 'claude' CLI is not on PATH — run this later:"; printf '  claude mcp add %s -s user -- %s\n' "$SERVER_NAME" "$CMD_DISPLAY"; return 0
  fi
  local scope; scope="$(ask 'Scope? [local|user|project] (default: user):' user)"
  local args=(mcp add "$SERVER_NAME" -s "$scope"); [[ -n "$HF_TOKEN_VAL" ]] && args+=(-e "HF_TOKEN=$HF_TOKEN_VAL")
  args+=(-- "${CMD_ARGV[@]}")
  if claude "${args[@]}"; then log "Registered with Claude Code (scope: $scope). Check it with 'claude mcp list' or '/mcp' in a session."
  else warn "'claude mcp add' failed — register manually:"; printf '  claude mcp add %s -s %s -- %s\n' "$SERVER_NAME" "$scope" "$CMD_DISPLAY"; fi
}
register_claude_desktop() {
  local cfg
  case "$(uname -s)" in
    Darwin) cfg="$HOME/Library/Application Support/Claude/claude_desktop_config.json" ;;
    Linux)
      if grep -qi microsoft /proc/version 2>/dev/null; then
        warn "Detected WSL: Claude Desktop is a Windows app, so auto-editing its config across the filesystem boundary is unsafe. Paste this into %APPDATA%\\Claude\\claude_desktop_config.json:"
        print_json_block "mcpServers" 0; return 0
      fi
      cfg="$HOME/.config/Claude/claude_desktop_config.json" ;;
    *) warn "Unrecognised OS — paste this into your claude_desktop_config.json:"; print_json_block "mcpServers" 0; return 0 ;;
  esac
  do_json_register "$cfg" "mcpServers" 0 "Claude Desktop" "Fully quit and reopen Claude Desktop to load the server."
}
register_cursor() {
  do_json_register "$HOME/.cursor/mcp.json" "mcpServers" 0 "Cursor" "Reload Cursor; verify under Settings → MCP."
}
register_vscode() {
  if command -v code >/dev/null 2>&1; then
    local obj
    obj="$(MCP_NAME="$SERVER_NAME" MCP_CMD="$CMD_BIN" MCP_ARGS="$CMD_ARGS_JSON" MCP_ENV="$ENV_JSON" "$PY" - <<'PYEOF'
import json, os
o = {"name": os.environ["MCP_NAME"], "command": os.environ["MCP_CMD"]}
a = json.loads(os.environ["MCP_ARGS"]);  o["args"] = a if a else []
e = json.loads(os.environ["MCP_ENV"])
if e: o["env"] = e
print(json.dumps(o))
PYEOF
)"
    if code --add-mcp "$obj"; then log "Registered with VS Code via 'code --add-mcp' (user scope)."; return 0; fi
    warn "'code --add-mcp' failed — writing a workspace .vscode/mcp.json instead."
  fi
  do_json_register "$PROJECT_DIR/.vscode/mcp.json" "servers" 1 "VS Code (workspace .vscode/mcp.json)" "Open $PROJECT_DIR in VS Code; it offers to start the server. For all workspaces use 'code --add-mcp' or MCP: Open User Configuration."
}
register_codex() {
  if command -v codex >/dev/null 2>&1; then
    local args=(mcp add "$SERVER_NAME"); [[ -n "$HF_TOKEN_VAL" ]] && args+=(--env "HF_TOKEN=$HF_TOKEN_VAL")
    args+=(-- "${CMD_ARGV[@]}")
    if codex "${args[@]}"; then log "Registered with Codex CLI."; return 0; fi
    warn "'codex mcp add' failed — appending to ~/.codex/config.toml instead."
  fi
  local cfg="$HOME/.codex/config.toml"; mkdir -p "$(dirname "$cfg")"; touch "$cfg"
  if grep -q '^\[mcp_servers\.llm_d_bench\]' "$cfg"; then
    log "~/.codex/config.toml already has [mcp_servers.llm_d_bench] — leaving it untouched."; return 0
  fi
  { printf '\n'; print_codex_block; } >>"$cfg"
  log "Appended [mcp_servers.llm_d_bench] to $cfg"
}

# ── Step 5: register with the chosen client(s) ────────────────────────────
step "Register with your agent client(s)"
echo "  1) Claude Code (CLI)"
echo "  2) Claude Desktop"
echo "  3) Cursor"
echo "  4) VS Code"
echo "  5) OpenAI Codex CLI"
echo "  6) Just print the config — make no changes  (default)"
echo "  0) Skip — I'll wire it up myself"
SEL="$(ask 'Choice(s), comma-separated (e.g. 1,3):' 6)"
IFS=',' read -ra CHOICES <<<"$SEL"
for c in "${CHOICES[@]}"; do
  case "$(echo "$c" | tr -d '[:space:]')" in
    1) register_claude_code ;;
    2) register_claude_desktop ;;
    3) register_cursor ;;
    4) register_vscode ;;
    5) register_codex ;;
    6) print_all_snippets ;;
    0|"") log "Skipping client registration." ;;
    *) warn "Ignoring unrecognised choice '$c'." ;;
  esac
done

# ── Summary ───────────────────────────────────────────────────────────────
step "Done"
log "Launch command : $CMD_DISPLAY"
log "Smoke-test it  : npx @modelcontextprotocol/inspector $CMD_DISPLAY   (lists 37 tools, 5 prompts, knowledge resources)"
log "Provider/config: $PROJECT_DIR/.env"
log "The server is stdio/local — it runs on this machine against your kubeconfig. Advisory tools work"
log "with no cluster; deploy/run tools need a reachable cluster. Mutations are approved in YOUR client's"
log "own tool-permission prompt. Full details: the repo-root README.md"
trap - EXIT
