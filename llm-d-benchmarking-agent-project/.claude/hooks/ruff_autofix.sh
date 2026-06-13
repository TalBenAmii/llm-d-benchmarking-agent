#!/usr/bin/env bash
# PostToolUse(Edit|Write) hook — keep edited Python lint-clean automatically.
#
# Runs `ruff check --fix` (the project gate is `make lint` = `ruff check .`). The project does
# NOT use `ruff format`, so this is lint-autofix ONLY — never `ruff format` (which would diverge
# from the established style of the whole tree). Anything ruff cannot auto-fix is reported back
# to Claude (exit 2 + stderr) so it gets fixed in-band instead of surfacing at gate time.
#
# Caveat: `ruff --fix` rewrites the file, and Claude Code does NOT re-read a file a PostToolUse
# hook modified — so a *fixed* file can trigger a one-off "file modified since read" on the next
# Edit in the same session (just re-read it). On already-clean files this hook is a no-op.
# Disable: set RUFF_HOOK_OFF=1, or remove the PostToolUse block from .claude/settings.json.
set -u
[ "${RUFF_HOOK_OFF:-0}" = "1" ] && exit 0

INPUT=$(cat)
FILE=$(printf '%s' "$INPUT" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("tool_input",{}).get("file_path",""))' 2>/dev/null) || exit 0

# Only Python files, and only inside the agent project (never the READ-ONLY sibling repos).
case "$FILE" in *.py) : ;; *) exit 0 ;; esac
case "$FILE" in */llm-d-benchmarking-agent-project/*) : ;; *) exit 0 ;; esac
[ -f "$FILE" ] || exit 0

# Resolve ruff relative to THIS script's location (portable across machines/clones):
# script lives at <project>/.claude/hooks/, so the project venv is two dirs up.
HOOK_DIR=$(cd "$(dirname "$0")" && pwd)
RUFF="$HOOK_DIR/../../.venv/bin/ruff"
[ -x "$RUFF" ] || RUFF=$(command -v ruff 2>/dev/null) || exit 0

"$RUFF" check --fix --quiet "$FILE" >/dev/null 2>&1 || true
if ! OUT=$("$RUFF" check "$FILE" 2>&1); then
  echo "ruff: unresolved lint issues in $FILE (auto-fix could not clear all):" >&2
  echo "$OUT" >&2
  exit 2
fi
exit 0
