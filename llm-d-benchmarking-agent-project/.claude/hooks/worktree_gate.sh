#!/usr/bin/env bash
# PreToolUse(Edit|Write) hook — enforce worktree isolation for project code edits.
#
# Replaces the old `use-worktree-when-implementing` memory: instead of hoping the model
# remembers to call EnterWorktree, this DENIES any Edit/Write to a project file that lives in
# the shared checkout (not under `.claude/worktrees/`). A hook can't call EnterWorktree itself
# (that's a model-only tool) — it blocks + tells the model to isolate first.
#
# Scoped to `/llm-d-benchmarking-agent-project/` (the stable project folder — the monorepo dir
# name varies by clone) so it never touches files outside the project. Read-only/search/question
# work is unaffected (no Edit/Write). Disable for a one-off in-place edit: WORKTREE_GATE_OFF=1.
set -u
[ "${WORKTREE_GATE_OFF:-0}" = "1" ] && exit 0

INPUT=$(cat)
FILE=$(printf '%s' "$INPUT" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("tool_input",{}).get("file_path",""))' 2>/dev/null) || exit 0
TOOL=$(printf '%s' "$INPUT" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("tool_name",""))' 2>/dev/null) || TOOL="Edit"

# Harness config (anything under a `.claude/` dir — settings.json, hooks, skills, workflows) is
# NOT project code; never gate it. This also stops the gate from blocking edits to itself or to
# settings.json (which live under the project dir but are tooling, not application code).
case "$FILE" in */.claude/*) exit 0 ;; esac
# Only project files are gated; anything else (memory, /tmp, sibling repos) is none of our business.
case "$FILE" in */llm-d-benchmarking-agent-project/*) : ;; *) exit 0 ;; esac
# Already isolated in a worktree → allow.
case "$FILE" in */.claude/worktrees/*) exit 0 ;; esac

# Record this block as a lesson so the inject hook reminds us to isolate FIRST next time.
# (PreToolUse denials never reach the PostToolUse capture path, so we record at the source.)
LTOOL="${TOOL:-Edit}" LINPUT="$FILE" \
LERROR="worktree gate: Edit/Write to the shared checkout is refused. Call EnterWorktree FIRST, then edit the copy under .claude/worktrees/." \
  bash "$(dirname "$0")/record_lesson.sh" 2>/dev/null || true

cat >&2 <<EOF
worktree gate: refusing to edit the shared checkout.
  $FILE
Project code changes must be isolated. Call the EnterWorktree tool first, then edit the copy
under .claude/worktrees/. (Read-only/search work needs no worktree. One-off bypass: set
WORKTREE_GATE_OFF=1.)
EOF
exit 2
