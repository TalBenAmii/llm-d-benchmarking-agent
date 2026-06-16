#!/usr/bin/env bash
# Stop hook — auto-merge a finished worktree branch into `main`, GATED BY A SENTINEL FILE.
#
# WHY a sentinel and not "merge on every Stop": the Stop hook fires after EVERY turn, not when a
# feature is actually done. Merging unconditionally would push half-finished work to main on every
# reply. So this is opt-in PER FEATURE: when the work is genuinely complete, drop a marker —
#     touch .ready-to-merge          (from the worktree root)
# — and the next time Claude stops, this hook merges the branch into main (--no-ff, so it's one
# revertable commit), removes the marker, and removes the now-merged worktree. No marker => this
# hook is a silent no-op.
#
# Guards (all must hold, else it reports to stderr and skips — never leaves main half-merged):
#   • we're inside a managed worktree under .claude/worktrees/  (normal sessions are skipped)
#   • the sentinel .ready-to-merge exists in the worktree root
#   • the worktree tree is CLEAN (everything committed — it won't auto-commit your work for you)
#   • the main checkout is actually on `main` (won't merge into some other checked-out branch)
#   • the merge applies without conflicts (on conflict: `git merge --abort`, report, drop sentinel)
#
# Test gate (two modes):
#   • Set AUTO_MERGE_TEST_CMD to a command (run from the worktree root) that must exit 0 → runs on
#     EVERY merge, e.g.  export AUTO_MERGE_TEST_CMD="make -C llm-d-benchmarking-agent-project test"
#   • Otherwise, a worktree-aware project-suite run fires automatically ONLY when the reconciliation
#     gate found overlap (concurrent work was reconciled) — cheap on disjoint merges, a safety net
#     exactly when concurrent sessions touched shared ground. Built with the right PYTHONPATH /
#     REPOS_DIR / .venv so the empty nested repos in a worktree don't false-fail. Disable: RECON_TEST_OFF=1.
# Disable everything: set AUTO_MERGE_OFF=1, or remove the Stop block from .claude/settings.json.
#
# Note: merges LOCALLY only (no push), then removes the now-merged worktree (the branch ref is
# kept — it's already in main). Keep the worktree after merge instead: set AUTO_MERGE_KEEP_WORKTREE=1.
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

# Reconciliation gate — BEFORE merging, check whether concurrent sessions (other worktree
# branches, or commits that landed on the target while this feature was in flight) touched the
# same ground. If so, HOLD the merge and dispatch the agent to reconcile first: pull those
# changes into this worktree, resolve conflicts, and validate that what each session promised
# was actually delivered. Only once reconciliation is recorded (signature matches current
# concurrent state) does the merge proceed. No overlap detected ⇒ this gate is a no-op.
# Disable the gate (old behaviour: merge straight away): RECON_OFF=1.
RECON_LIB="$(dirname "$0")/recon_lib.sh"
HAD_OVERLAP=0   # set when the reconciliation gate sees concurrent overlap — drives the test gate below
if [ "${RECON_OFF:-0}" != "1" ] && [ -f "$RECON_LIB" ]; then
  SIGNALS=$(bash "$RECON_LIB" detect "$WT_ROOT" "$TARGET_BRANCH" "$BRANCH" 2>/dev/null)
  if [ -n "$SIGNALS" ]; then
    HAD_OVERLAP=1
    CUR_SIG=$(bash "$RECON_LIB" signature "$WT_ROOT" "$TARGET_BRANCH" "$BRANCH" 2>/dev/null)
    DONE_SIG=$(cat "$WT_ROOT/.reconciled" 2>/dev/null)
    if [ -z "$CUR_SIG" ] || [ "$CUR_SIG" != "$DONE_SIG" ]; then
      cat >&2 <<EOF
auto-merge: HOLDING the merge of '$BRANCH' into '$TARGET_BRANCH' — concurrent session activity
needs reconciliation first.

Detected:
$SIGNALS

Run the **reconcile-before-merge** skill now. In short: bring '$TARGET_BRANCH' (and any
overlapping sibling branches' changes) into THIS worktree, resolve every conflict, and verify
that each session actually delivered what it promised in its chat — read the relevant session
transcripts under
  /home/roots/.claude/projects/-home-roots-llm-d-benchmarking-agent/*.jsonl
if a change's intent is unclear. Commit your resolution ON THIS BRANCH ('$TARGET_BRANCH' stays
untouched), then record reconciliation and re-arm the merge:
    bash "$RECON_LIB" mark "$WT_ROOT" "$TARGET_BRANCH" "$BRANCH"
    touch "$WT_ROOT/$SENTINEL_NAME"
The next time you stop, this hook will merge cleanly and remove the worktree.
EOF
      exit 2
    fi
  fi
fi

# Test gate. AUTO_MERGE_TEST_CMD (if set) runs on every merge. Otherwise the project suite runs
# ONLY when overlap was just reconciled (HAD_OVERLAP) — built with the worktree-aware env so the
# empty nested repos in a worktree don't false-fail. See the header note for the rationale.
PROJ_SUBDIR="llm-d-benchmarking-agent-project"
WT_PROJ="$WT_ROOT/$PROJ_SUBDIR"
VENV_PY="$MAIN_ROOT/$PROJ_SUBDIR/.venv/bin/python"
TEST_KIND=""
if [ -n "${AUTO_MERGE_TEST_CMD:-}" ]; then
  TEST_KIND="custom"
elif [ "$HAD_OVERLAP" = "1" ] && [ "${RECON_TEST_OFF:-0}" != "1" ]; then
  if [ -x "$VENV_PY" ] && [ -d "$WT_PROJ/tests" ]; then
    TEST_KIND="auto"
  else
    echo "auto-merge: overlap was reconciled but the validation suite can't run (no $VENV_PY or $WT_PROJ/tests) — merging on the reconcile skill's own test run." >&2
  fi
fi

if [ -n "$TEST_KIND" ]; then
  echo "auto-merge: ${TEST_KIND} test gate for '$BRANCH' before merging…" >&2
  rc=0
  if [ "$TEST_KIND" = "custom" ]; then
    ( cd "$WT_ROOT" && eval "$AUTO_MERGE_TEST_CMD" ) >/dev/null 2>&1 || rc=$?
  else
    ( cd "$WT_PROJ" && PYTHONPATH="$WT_PROJ" REPOS_DIR="$MAIN_ROOT" "$VENV_PY" -m pytest tests/ -q ) >/dev/null 2>&1 || rc=$?
  fi
  if [ "$rc" -ne 0 ]; then
    echo "auto-merge: TEST GATE FAILED for '$BRANCH' (rc=$rc) — NOT merging; '$TARGET_BRANCH' untouched, sentinel kept. The reconciled tree doesn't pass the suite: fix the failing tests, commit on this branch, then stop again. (Bypass once: RECON_TEST_OFF=1.)" >&2
    exit 0
  fi
fi

# Merge. --no-ff keeps it as a single revertable merge commit. Abort cleanly on conflict.
rm -f "$WT_ROOT/$SENTINEL_NAME" "$WT_ROOT/.reconciled"   # consume markers first, so a conflict can't cause a retry loop
if git -C "$MAIN_ROOT" merge --no-ff -m "merge $BRANCH into $TARGET_BRANCH (auto-merge on Stop)" "$BRANCH" >/dev/null 2>&1; then
  if [ "${AUTO_MERGE_KEEP_WORKTREE:-0}" = "1" ]; then
    echo "auto-merge: merged '$BRANCH' into '$TARGET_BRANCH' (local, --no-ff). Worktree kept (AUTO_MERGE_KEEP_WORKTREE=1) — ExitWorktree to remove it." >&2
  # Remove the merged worktree. Driven from MAIN_ROOT (never from inside the dir we're deleting).
  # --force: the tree holds untracked bits (the empty nested sibling-repo gitlinks) that would
  # otherwise make `worktree remove` refuse. The branch ref is left in place — it's now in main.
  elif git -C "$MAIN_ROOT" worktree remove --force "$WT_ROOT" >/dev/null 2>&1; then
    echo "auto-merge: merged '$BRANCH' into '$TARGET_BRANCH' (local, --no-ff) and removed the worktree at $WT_ROOT (branch ref kept)." >&2
  else
    echo "auto-merge: merged '$BRANCH' into '$TARGET_BRANCH' (local, --no-ff), but could NOT remove the worktree at $WT_ROOT — remove it manually: git worktree remove --force '$WT_ROOT'." >&2
  fi
else
  git -C "$MAIN_ROOT" merge --abort >/dev/null 2>&1
  echo "auto-merge: '$BRANCH' CONFLICTS with '$TARGET_BRANCH' — aborted, main untouched. Resolve, then re-touch $SENTINEL_NAME." >&2
fi
exit 0
