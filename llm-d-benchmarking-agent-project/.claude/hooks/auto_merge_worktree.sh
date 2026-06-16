#!/usr/bin/env bash
# Stop hook — auto-merge a FINISHED worktree branch into `main`. The AGENT is the decider, and
# COMMITTING the work IS the "done" signal — there is no sentinel file to touch.
#
# WHY commit-as-signal: the Stop hook fires after EVERY turn, not when a feature is actually done.
# We can't ask the model from a hook, so we use the one deliberate "I'm done" act the agent already
# performs — a commit. A clean, committed, ahead-of-`main` worktree means the agent decided it's
# done; anything uncommitted means it's still mid-flight.
#
# Behaviour on Stop, only inside a managed worktree under .claude/worktrees/ (normal main-checkout
# sessions are always skipped):
#   • Clean tree + branch ahead of `main`  → agent committed = done. Reconcile-gate, test-gate,
#     then merge (--no-ff, local) into `main` and remove the worktree.
#   • Uncommitted changes present          → still mid-flight. ONE-TIME per worktree, block the stop
#     (exit 2, fed back to the model) with a short note: "commit when done → I'll merge; not done →
#     stop again." A `.merge-nudged` marker makes it fire exactly once (no nagging, no loop).
#   • Clean but not ahead of `main`        → nothing to merge → silent no-op.
#   • `.hold` file in the worktree root    → silent no-op (opt-out: commit progress but keep working
#     across turns without merging; delete the file to re-enable).
#
# This whole instruction set lives ONLY here, surfaced via the hook exactly when relevant, so it
# never bloats the agent's standing context/memory.
#
# Reconcile gate: BEFORE merging, if concurrent sessions (other worktree branches, or commits that
# landed on `main` while this was in flight) touched the same ground, HOLD and dispatch the
# reconcile-before-merge skill; merge only once reconciliation is recorded. Disable: RECON_OFF=1.
# Test gate: AUTO_MERGE_TEST_CMD (if set) runs on every merge; otherwise the project suite runs only
# when overlap was just reconciled. Disable: RECON_TEST_OFF=1.
#
# Merges LOCALLY only (no push), then removes the merged worktree (branch ref kept — it's in main).
# Keep the worktree: AUTO_MERGE_KEEP_WORKTREE=1. Disable everything: AUTO_MERGE_OFF=1.
set -u
[ "${AUTO_MERGE_OFF:-0}" = "1" ] && exit 0

TARGET_BRANCH="main"

# Where are we? Resolve the worktree root from the Stop hook's cwd (falls back to PWD).
INPUT=$(cat)
CWD=$(printf '%s' "$INPUT" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("cwd",""))' 2>/dev/null)
[ -n "$CWD" ] || CWD=$PWD

WT_ROOT=$(git -C "$CWD" rev-parse --show-toplevel 2>/dev/null) || exit 0

# Only act inside a managed worktree — normal (main-checkout) sessions must be untouched.
case "$WT_ROOT" in */.claude/worktrees/*) : ;; *) exit 0 ;; esac

# Opt-out: agent committed but wants to keep working without merging yet.
[ -f "$WT_ROOT/.hold" ] && exit 0

BRANCH=$(git -C "$WT_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null)
[ -n "$BRANCH" ] && [ "$BRANCH" != "HEAD" ] || { echo "auto-merge: worktree is in a detached HEAD — skipping." >&2; exit 0; }

# State: is there uncommitted tracked work, and is the branch ahead of main?
DIRTY=0; [ -n "$(git -C "$WT_ROOT" status --porcelain --untracked-files=no)" ] && DIRTY=1
AHEAD=$(git -C "$WT_ROOT" rev-list --count "$TARGET_BRANCH..HEAD" 2>/dev/null || echo 0)

# Uncommitted work → still mid-flight. The agent is the decider: nudge it ONCE to commit when done.
# The `.merge-nudged` marker (untracked; ignored by the DIRTY check) guarantees exactly one prompt
# per worktree — no per-turn nagging, no stop loop.
if [ "$DIRTY" = "1" ]; then
  NUDGE_MARK="$WT_ROOT/.merge-nudged"
  [ -f "$NUDGE_MARK" ] && exit 0
  : > "$NUDGE_MARK" 2>/dev/null || true
  cat >&2 <<EOF
auto-merge (one-time note for worktree '$BRANCH'): this branch merges into '$TARGET_BRANCH'
automatically when you COMMIT — committing is your "done" signal, there is no sentinel to touch.
  • When the user's request is COMPLETE → commit everything on this branch (stage the project paths,
    then git commit with a clear message). On your next stop I reconcile + merge into '$TARGET_BRANCH'
    and remove the worktree.
  • Not done yet, or pausing to ask the user something → just respond and stop again; you won't see
    this note again.
  • Want to commit progress but keep working across turns without merging → touch '$WT_ROOT/.hold'
    (delete it to re-enable auto-merge).
EOF
  exit 2
fi

# Clean tree but nothing ahead of main → nothing to merge (silent no-op).
[ "${AHEAD:-0}" -gt 0 ] 2>/dev/null || exit 0

# ── Clean + ahead of main: the agent committed = done. Reconcile-gate → test-gate → merge. ──
# Locate the MAIN working tree (first entry of `git worktree list`, i.e. not a .claude worktree).
MAIN_ROOT=$(git -C "$WT_ROOT" worktree list --porcelain 2>/dev/null \
  | awk '/^worktree /{print substr($0,10); exit}')
[ -n "$MAIN_ROOT" ] && [ -d "$MAIN_ROOT" ] || { echo "auto-merge: could not locate the main checkout — skipping." >&2; exit 0; }

# The main checkout must be on `main`, or we'd merge into whatever it happens to have checked out.
MAIN_BRANCH=$(git -C "$MAIN_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null)
if [ "$MAIN_BRANCH" != "$TARGET_BRANCH" ]; then
  echo "auto-merge: main checkout is on '$MAIN_BRANCH', not '$TARGET_BRANCH' — refusing to merge there." >&2
  exit 0
fi

# Reconciliation gate — see header. Empty detect ⇒ no-op. Disable: RECON_OFF=1.
RECON_LIB="$(dirname "$0")/recon_lib.sh"
HAD_OVERLAP=0   # set when overlap is detected — drives the test gate below
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
if a change's intent is unclear. Then record reconciliation and COMMIT your resolution on this
branch ('$TARGET_BRANCH' stays untouched):
    bash "$RECON_LIB" mark "$WT_ROOT" "$TARGET_BRANCH" "$BRANCH"
On your next stop the merge proceeds automatically (the commit is your done-signal).
EOF
      exit 2
    fi
  fi
fi

# Test gate. AUTO_MERGE_TEST_CMD (if set) runs on every merge. Otherwise the project suite runs
# ONLY when overlap was just reconciled (HAD_OVERLAP), built with the worktree-aware env so the
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
    echo "auto-merge: TEST GATE FAILED for '$BRANCH' (rc=$rc) — NOT merging; '$TARGET_BRANCH' untouched. The reconciled tree doesn't pass the suite: fix the failing tests, commit on this branch, then stop again. (Bypass once: RECON_TEST_OFF=1.)" >&2
    exit 0
  fi
fi

# Merge. --no-ff keeps it as a single revertable merge commit. Abort cleanly on conflict.
rm -f "$WT_ROOT/.reconciled" "$WT_ROOT/.merge-nudged"   # consume markers first (no retry loop)
if git -C "$MAIN_ROOT" merge --no-ff -m "merge $BRANCH into $TARGET_BRANCH (auto-merge on Stop)" "$BRANCH" >/dev/null 2>&1; then
  if [ "${AUTO_MERGE_KEEP_WORKTREE:-0}" = "1" ]; then
    echo "auto-merge: merged '$BRANCH' into '$TARGET_BRANCH' (local, --no-ff). Worktree kept (AUTO_MERGE_KEEP_WORKTREE=1) — ExitWorktree to remove it." >&2
  # Remove the merged worktree. Driven from MAIN_ROOT (never from inside the dir we're deleting).
  # --force: the tree holds untracked bits (empty nested sibling-repo gitlinks, local markers) that
  # would otherwise make `worktree remove` refuse. The branch ref is left in place — it's now in main.
  elif git -C "$MAIN_ROOT" worktree remove --force "$WT_ROOT" >/dev/null 2>&1; then
    # The worktree we just deleted is THIS still-live session's working directory. Removing it out
    # from under the running Claude process leaves its cwd dangling, and the harness spawns every
    # hook with cwd=<that dir> — so the NEXT UserPromptSubmit (and every later hook) dies with
    # `ENOENT: posix_spawn '/bin/sh'` even though /bin/sh is fine (Node reports the bad-cwd spawn
    # failure against the executable). A subprocess can't chdir its parent, so we restore a VALID
    # cwd the only way we can from here: recreate the exact session dir as an inert empty stub.
    # Cleanup intent is preserved — the git worktree is gone; only a harmless empty dir remains.
    if [ -n "$CWD" ] && [ ! -d "$CWD" ]; then
      if mkdir -p "$CWD" 2>/dev/null; then
        printf '%s\n' \
          "Inert stub left by auto_merge_worktree.sh after it merged this session's branch and" \
          "removed the git worktree that lived here. It exists ONLY so the still-running Claude" \
          "session keeps a valid working directory — without it, every hook fails with" \
          "  Error occurred while executing hook command: ENOENT ... posix_spawn '/bin/sh'" \
          "because the harness spawns hooks with cwd set to this (now-deleted) path. Safe to" \
          "delete once the session ends." > "$CWD/README.worktree-removed" 2>/dev/null || true
      fi
    fi
    echo "auto-merge: merged '$BRANCH' into '$TARGET_BRANCH' (local, --no-ff) and removed the worktree at $WT_ROOT (branch ref kept; recreated an empty stub at the live session's cwd so its hooks keep working — safe to delete)." >&2
  else
    echo "auto-merge: merged '$BRANCH' into '$TARGET_BRANCH' (local, --no-ff), but could NOT remove the worktree at $WT_ROOT — remove it manually: git worktree remove --force '$WT_ROOT'." >&2
  fi
else
  git -C "$MAIN_ROOT" merge --abort >/dev/null 2>&1
  echo "auto-merge: '$BRANCH' CONFLICTS with '$TARGET_BRANCH' — aborted, main untouched. Resolve on this branch, commit, then stop again." >&2
fi
exit 0
