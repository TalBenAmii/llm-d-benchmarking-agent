#!/usr/bin/env bash
# Stop hook — auto-merge a finished worktree branch into `main`, GATED BY A SENTINEL FILE.
#
# WHY a sentinel and not "merge on every Stop": the Stop hook fires after EVERY turn, not when a
# feature is actually done. Merging unconditionally would push half-finished work to main on every
# reply. So this is opt-in PER FEATURE: when the work is genuinely complete, drop a marker —
#     touch .ready-to-merge          (from the worktree root)
# — and the next time Claude stops, this hook merges the branch into main (--no-ff, so it's one
# revertable commit) and removes the marker. No marker => this hook is a silent no-op.
#
# Guards (all must hold, else it reports to stderr and skips — never leaves main half-merged):
#   • we're inside a managed worktree under .claude/worktrees/  (normal sessions are skipped)
#   • the sentinel .ready-to-merge exists in the worktree root
#   • the worktree tree is CLEAN (everything committed — it won't auto-commit your work for you)
#   • the main checkout is actually on `main` (won't merge into some other checked-out branch)
#   • the merge applies without conflicts (on conflict: `git merge --abort`, report, drop sentinel)
#
# Optional test gate: set AUTO_MERGE_TEST_CMD to a command (run from the worktree root) that must
# exit 0 before the merge, e.g.  export AUTO_MERGE_TEST_CMD="make -C llm-d-benchmarking-agent-project test"
# Disable entirely: set AUTO_MERGE_OFF=1, or remove the Stop block from .claude/settings.json.
#
# Note: merges LOCALLY only (no push) and does NOT delete the worktree — run ExitWorktree for that.
set -u
[ "${AUTO_MERGE_OFF:-0}" = "1" ] && exit 0

SENTINEL_NAME=".ready-to-merge"
TARGET_BRANCH="main"

# Where are we? Resolve the worktree root from the Stop hook's cwd (falls back to PWD).
INPUT=$(cat)
CWD=$(printf '%s' "$INPUT" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("cwd",""))' 2>/dev/null)
[ -n "$CWD" ] || CWD=$PWD

WT_ROOT=$(git -C "$CWD" rev-parse --show-toplevel 2>/dev/null) || exit 0

# Only act inside a managed worktree — normal (main-checkout) sessions must be untouched.
case "$WT_ROOT" in */.claude/worktrees/*) : ;; *) exit 0 ;; esac

# Sentinel present? If not, nothing to do (the common case — silent no-op).
[ -f "$WT_ROOT/$SENTINEL_NAME" ] || exit 0

BRANCH=$(git -C "$WT_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null)
[ -n "$BRANCH" ] && [ "$BRANCH" != "HEAD" ] || { echo "auto-merge: worktree is in a detached HEAD — skipping." >&2; exit 0; }

# Refuse to merge uncommitted work — the sentinel says "done", but only committed work is "done".
if [ -n "$(git -C "$WT_ROOT" status --porcelain --untracked-files=no)" ]; then
  echo "auto-merge: '$BRANCH' has uncommitted changes — commit them, then re-touch $SENTINEL_NAME." >&2
  exit 0
fi

# Locate the MAIN working tree (the first entry of `git worktree list`, i.e. not a .claude worktree).
MAIN_ROOT=$(git -C "$WT_ROOT" worktree list --porcelain 2>/dev/null \
  | awk '/^worktree /{print substr($0,10); exit}')
[ -n "$MAIN_ROOT" ] && [ -d "$MAIN_ROOT" ] || { echo "auto-merge: could not locate the main checkout — skipping." >&2; exit 0; }

# The main checkout must be on `main`, or we'd merge into whatever it happens to have checked out.
MAIN_BRANCH=$(git -C "$MAIN_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null)
if [ "$MAIN_BRANCH" != "$TARGET_BRANCH" ]; then
  echo "auto-merge: main checkout is on '$MAIN_BRANCH', not '$TARGET_BRANCH' — refusing to merge there." >&2
  exit 0
fi

# Optional test gate (run from the worktree).
if [ -n "${AUTO_MERGE_TEST_CMD:-}" ]; then
  echo "auto-merge: running test gate ($AUTO_MERGE_TEST_CMD)…" >&2
  if ! ( cd "$WT_ROOT" && eval "$AUTO_MERGE_TEST_CMD" ) >/dev/null 2>&1; then
    echo "auto-merge: test gate FAILED — leaving '$BRANCH' unmerged and the sentinel in place." >&2
    exit 0
  fi
fi

# Merge. --no-ff keeps it as a single revertable merge commit. Abort cleanly on conflict.
rm -f "$WT_ROOT/$SENTINEL_NAME"   # consume the marker first, so a conflict can't cause a retry loop
if git -C "$MAIN_ROOT" merge --no-ff -m "merge $BRANCH into $TARGET_BRANCH (auto-merge on Stop)" "$BRANCH" >/dev/null 2>&1; then
  echo "auto-merge: merged '$BRANCH' into '$TARGET_BRANCH' (local, --no-ff). Worktree left intact — ExitWorktree to remove it." >&2
else
  git -C "$MAIN_ROOT" merge --abort >/dev/null 2>&1
  echo "auto-merge: '$BRANCH' CONFLICTS with '$TARGET_BRANCH' — aborted, main untouched. Resolve, then re-touch $SENTINEL_NAME." >&2
fi
exit 0
