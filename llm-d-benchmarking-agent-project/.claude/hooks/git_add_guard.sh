#!/usr/bin/env bash
# PreToolUse(Bash) hook — block the `git add -A` / `git add --all` / `git add .` footgun AT THE
# MONOREPO ROOT. Per CLAUDE.md: a blanket add there stages the .claude/worktrees/* gitlinks (and
# other untracked cruft) into a commit. This DENIES the blanket form only when the command runs
# from the repo root with no narrowing path; scoped adds (`git add <path>`) and adds from inside a
# subdir/worktree are untouched. One-off bypass: GIT_ADD_GUARD_OFF=1.
set -u
[ "${GIT_ADD_GUARD_OFF:-0}" = "1" ] && exit 0

INPUT=$(cat)
MSG=$(HOOK_INPUT="$INPUT" python3 - <<'PY' 2>/dev/null
import json, sys, os, re
try:
    d = json.loads(os.environ.get("HOOK_INPUT", "") or "{}")
except Exception:
    sys.exit(0)
ti = d.get("tool_input", {}) or {}
cmd = ti.get("command") or ""
cwd = d.get("cwd") or ""
root = os.environ.get("CLAUDE_PROJECT_DIR", "")
# Only guard at the monorepo root (where the worktree gitlinks live). Scoped/subdir adds are fine.
if not root or os.path.realpath(cwd or ".") != os.path.realpath(root):
    sys.exit(0)
for seg in re.split(r'&&|\|\||;|\|', cmd):
    m = re.search(r'\bgit\s+add\b(.*)', seg)
    if not m:
        continue
    toks = m.group(1).split()
    blanket = any(t in ('-A', '--all', '.') for t in toks)
    has_path = any((not t.startswith('-') and t != '.') for t in toks)
    if blanket and not has_path:
        print("git add guard: refusing a blanket `git add` at the monorepo root (%s)." % root)
        print("It would stage the .claude/worktrees/* gitlinks and other untracked cruft.")
        print("Stage specific paths instead, e.g.  git add llm-d-benchmarking-agent-project/<path>")
        print("(one-off bypass: GIT_ADD_GUARD_OFF=1)")
        sys.exit(7)
sys.exit(0)
PY
)
if [ $? -eq 7 ]; then
  # Record the block so the inject hook warns before the next blanket add (PreToolUse denials
  # never reach the PostToolUse capture path).
  LTOOL="Bash" LINPUT="git add (blanket, at monorepo root)" \
  LERROR="git add guard: a blanket 'git add -A/--all/.' at the monorepo root is refused (it stages .claude/worktrees/* gitlinks). Stage specific paths, e.g. git add llm-d-benchmarking-agent-project/<path>." \
    bash "$(dirname "$0")/record_lesson.sh" 2>/dev/null || true
  printf '%s\n' "$MSG" >&2
  exit 2
fi
exit 0
