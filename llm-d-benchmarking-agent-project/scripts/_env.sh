#!/usr/bin/env bash
# Bootstrap helpers shared by install.sh (full setup) and run.sh (standalone launcher), kept here so
# both source one copy instead of duplicating it. The sourcing script must define `log` first.
#
# Only the .env bootstrap is shared: the venv / editable-install steps are deliberately NOT here —
# install.sh resolves the backend for a bare box and honours --uv/--dev, while run.sh stays a minimal
# `command -v uv` launcher, so a single shared shape would either lose behavior or over-parameterize.

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
