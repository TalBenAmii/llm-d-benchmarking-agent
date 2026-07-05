#!/usr/bin/env bash
# Bootstrap helpers shared by install.sh (full setup), run.sh (standalone launcher), and
# setup-claude-plan.sh (Claude-plan wiring) — and by the external llm-d-bench-mcp installer, which
# sources this same file cross-repo — kept here so all source
# one copy instead of duplicating it. The sourcing script must define `log` first (and `die`
# too, if it calls clone_if_missing; `warn`+`log` if it calls ensure_claude_cli; `warn`+`log` if it
# calls register_mcp_server). The menu helpers (menu_select/confirm) need no caller-defined helpers —
# they render on /dev/tty. read_env/set_env_var operate on ./.env — callers cd to root before sourcing.
#
# The venv / editable-install steps are deliberately NOT shared: install.sh resolves the backend for a
# bare box and honours --uv/--dev, while run.sh stays a minimal `command -v uv` launcher, so a single
# shared shape would either lose behavior or over-parameterize.

# Create .env from .env.example on first run; no-op once .env exists.
ensure_env() {
  [[ -f .env ]] && return 0
  if [[ -f .env.example ]]; then
    cp .env.example .env
    log "Created .env from .env.example — set your LLM provider/key (LLM_PROVIDER=claude-agent-sdk needs no key)."
  else
    log "No .env and no .env.example — continuing on built-in defaults."
  fi
}

# Read one KEY's value from ./.env (last assignment wins; `export KEY=…` lines count too —
# python-dotenv honors them, so ignoring them would misread a configured env). Strips only
# SURROUNDING whitespace/quotes (not every internal space/quote — `tr -d` mangled values like
# HOST="my host" into "myhost"); enough for the HOST/PORT/PROVIDER/KEY reads the callers do.
read_env() { [[ -f .env ]] && grep -E "^\s*(export\s+)?$1\s*=" .env | tail -1 | cut -d= -f2- | sed -E "s/^[[:space:]'\"]+//; s/[[:space:]'\"]+\$//" || true; }

# Replace-or-append KEY=VALUE in ./.env (pure bash; values printf'd verbatim). Also replaces
# an `export KEY=…` spelling of the same key so the file never ends up with two assignments.
set_env_var() {  # $1 KEY  $2 VALUE
  local key="$1" val="$2" f=".env" tmp
  touch "$f"; tmp="$(mktemp)"
  grep -vE "^\s*(export\s+)?${key}=" "$f" >"$tmp" 2>/dev/null || true
  printf '%s=%s\n' "$key" "$val" >>"$tmp"
  mv "$tmp" "$f"
}

# Clone an upstream sibling repo into $dest if it's absent/empty; no-op if present. With NO_CLONE=1
# (install.sh's --no-clone) a missing repo is a hard error instead. Needs `git`; uses `log`/`die`.
clone_if_missing() {
  local name="$1" dest="$2" owner="${3:-llm-d}"
  if [[ -d "$dest" && -n "$(ls -A "$dest" 2>/dev/null)" ]]; then
    log "$name present at $dest — skipping clone."
  elif [[ "${NO_CLONE:-0}" == 1 ]]; then
    die "$name not found at $dest and --no-clone was given. Clone it there or set REPOS_DIR."
  else
    command -v git >/dev/null 2>&1 || die "git is required to clone $name — install git (e.g. 'apt install git') and re-run, or pre-clone the repos and pass --no-clone."
    log "Cloning $name → $dest"
    git clone --depth 1 "https://github.com/$owner/$name" "$dest"
  fi
}

# Put ~/.local/bin on PATH (idempotent). The official `claude`/`uv` installers drop binaries there,
# and a non-login `curl | bash` shell usually doesn't have it on PATH — so an already-installed tool
# looks "missing" until this runs. Callers that only need to SURFACE an installed CLI stop here.
add_local_bin_to_path() {
  case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac
}

# Ensure the `claude` CLI (the credential holder for the claude-agent-sdk provider) is installed and on
# PATH: first surface an already-installed copy (add_local_bin_to_path), else offer the official no-sudo
# installer (consent via the arrow-key `confirm`) and re-add the dir. Uses the caller's log/warn. Returns
# 0 (available) · 2 (declined) · 1 (install failed) — the caller decides if non-zero is fatal
# (setup-claude-plan dies; the MCP installer warns and continues).
ensure_claude_cli() {
  add_local_bin_to_path
  command -v claude >/dev/null 2>&1 && return 0
  warn "The 'claude' CLI is not installed — your Claude login authenticates through it."
  confirm 'Install it now (official installer, no sudo → ~/.local/bin/claude)?' Y || return 2
  log "Installing the claude CLI…"
  curl -fsSL https://claude.ai/install.sh | bash || return 1
  add_local_bin_to_path
  command -v claude >/dev/null 2>&1 || return 1
  log "Installed the claude CLI → $(command -v claude)"
}

# menu_select PROMPT DEFAULT_INDEX OPTION [OPTION...]
#   Arrow-key menu rendered on /dev/tty. Up/Down arrows (and k/j) move; Enter selects.
#   Prints the SELECTED 0-BASED INDEX to stdout (nothing else on stdout).
#   No TTY available -> prints the options + a "(non-interactive: using default)" notice
#   to stderr and returns DEFAULT_INDEX. Never hangs without a terminal.
menu_select() {
  local prompt="$1" cur="$2"; shift 2
  local opts=("$@") n=$# i key esc
  local tty=/dev/tty; { : <"$tty"; } 2>/dev/null || tty=""
  if [[ -z "$tty" ]]; then
    printf '%s\n' "$prompt" >&2
    for i in "${!opts[@]}"; do printf '  %s) %s\n' "$i" "${opts[$i]}" >&2; done
    printf '(non-interactive: using default)\n' >&2
    printf '%s\n' "$cur"
    return 0
  fi
  (( cur < 0 )) && cur=0
  (( cur >= n )) && cur=$(( n - 1 ))
  local saved; saved="$(stty -g <"$tty" 2>/dev/null || true)"
  # Restore the cursor + terminal on Ctrl-C (this runs in the function's scope; $tty/$saved are live).
  trap 'printf "\033[?25h" >"$tty" 2>/dev/null; [[ -n "$saved" ]] && stty "$saved" <"$tty" 2>/dev/null; trap - INT; return 130' INT
  stty -echo -icanon min 1 time 0 <"$tty" 2>/dev/null || true
  printf '\033[?25l%s\n' "$prompt" >"$tty"          # hide cursor + print the prompt line
  local first=1
  while true; do
    (( first )) || printf '\033[%dA' "$n" >"$tty"   # after the first pass, redraw over the N option lines
    first=0
    for i in "${!opts[@]}"; do
      if (( i == cur )); then printf '\033[36m❯ %s\033[0m\033[K\n' "${opts[$i]}" >"$tty"
      else printf '  %s\033[K\n' "${opts[$i]}" >"$tty"; fi
    done
    IFS= read -rsn1 key <"$tty" || key=""
    case "$key" in
      $'\x1b') IFS= read -rsn2 -t 0.05 esc <"$tty" 2>/dev/null || esc=""   # arrow: ESC then '[' then A/B
               case "$esc" in
                 '[A') cur=$(( (cur - 1 + n) % n )) ;;
                 '[B') cur=$(( (cur + 1) % n )) ;;
               esac ;;
      k|K) cur=$(( (cur - 1 + n) % n )) ;;
      j|J) cur=$(( (cur + 1) % n )) ;;
      ''|$'\n'|$'\r') break ;;   # Enter selects the current row
    esac
  done
  printf '\033[%dA\r\033[J' "$(( n + 1 ))" >"$tty"  # erase the prompt + menu, then leave a one-line summary
  printf '\033[36m%s ❯ %s\033[0m\n' "$prompt" "${opts[$cur]}" >"$tty"
  printf '\033[?25h' >"$tty"
  [[ -n "$saved" ]] && stty "$saved" <"$tty" 2>/dev/null || true   # || true: a set -e stty failure must not skip the index print below
  trap - INT
  printf '%s\n' "$cur"
}

# confirm PROMPT [DEFAULT]   DEFAULT is Y or N (default N)
#   Arrow-key Yes/No built on menu_select. Returns 0 if Yes chosen, 1 if No.
#   No TTY -> returns per DEFAULT.
confirm() {
  local prompt="$1" default="${2:-N}" di=1 sel
  case "$default" in [Yy]*) di=0 ;; esac
  sel="$(menu_select "$prompt" "$di" Yes No)"
  [[ "$sel" == 0 ]]
}

# register_mcp_server LAUNCH_CMD [SCOPE] [INTERACTIVE]
#   Registers the llm-d-bench MCP server with Claude Code: `claude mcp add llm-d-bench <LAUNCH_CMD>` at SCOPE.
#   SCOPE default = user. If INTERACTIVE=1 and a TTY exists, first prompt for scope
#   (local/user/project) via menu_select. If the `claude` CLI is missing, print a short
#   note (warn) explaining how to register manually and return non-zero — never fail hard.
register_mcp_server() {
  local launch_cmd="$1" scope="${2:-user}" interactive="${3:-0}"
  local server="llm-d-bench"
  if [[ "$interactive" == 1 ]]; then
    local tty=/dev/tty; { : <"$tty"; } 2>/dev/null || tty=""
    if [[ -n "$tty" ]]; then
      local scopes=(local user project) sel
      sel="$(menu_select 'Registration scope?' 1 "${scopes[@]}")"
      scope="${scopes[$sel]}"
    fi
  fi
  if ! command -v claude >/dev/null 2>&1; then
    warn "The 'claude' CLI is not on PATH — register the server yourself once it's installed:"
    warn "  claude mcp add $server -s $scope -- $launch_cmd"
    return 1
  fi
  # Idempotent: re-running the installer must not report a scary "already exists" error.
  if claude mcp list 2>/dev/null | grep -q "$server"; then
    log "'$server' already registered with Claude Code — skipping."
    return 0
  fi
  # $launch_cmd is a command line — leave it UNQUOTED so it word-splits into argv after `--`.
  if claude mcp add "$server" -s "$scope" -- $launch_cmd; then
    log "Registered '$server' with Claude Code (scope: $scope). Verify with 'claude mcp list' or '/mcp'."
    return 0
  fi
  warn "'claude mcp add' failed — register it manually:"
  warn "  claude mcp add $server -s $scope -- $launch_cmd"
  return 1
}
