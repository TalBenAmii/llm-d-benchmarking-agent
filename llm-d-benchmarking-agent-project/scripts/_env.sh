#!/usr/bin/env bash
# Bootstrap helpers shared by install.sh (full setup), run.sh (standalone launcher), and
# setup-claude-plan.sh (Claude-plan wiring) — and by the external llm-d-bench-mcp installer, which
# sources this same file cross-repo — kept here so all source
# one copy instead of duplicating it. The sourcing script must define `log` first (and `die`
# too, if it calls clone_if_missing). read_env/set_env_var operate on ./.env — callers cd to
# the project root before sourcing.
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
