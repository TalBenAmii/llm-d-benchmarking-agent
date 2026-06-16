---
name: reconcile-before-merge
description: Invoked when the auto-merge Stop hook HOLDS a finished feature branch because concurrent sessions touched overlapping ground. Reconcile a worktree branch against everything else that changed while it was in flight — pull the concurrent changes in, resolve textual + semantic conflicts, and validate that each session actually delivered what its chat promised — THEN re-arm the merge. Use whenever you see "auto-merge: HOLDING the merge … needs reconciliation first".
---

# Reconcile before merge

The auto-merge-on-Stop hook (`auto_merge_worktree.sh`) only fires when a feature is marked done
(`.ready-to-merge` present). Before it merges your worktree branch into `main`, it runs a
**reconciliation gate** (`recon_lib.sh detect`). If other sessions ran concurrently and touched
the same files — or `main` advanced under you, or a trial merge would conflict — it **holds the
merge** and hands control here. Your job: make this branch safe to merge, then re-arm it.

You are reconciling **inside the worktree**. `main` stays untouched until the branch is clean.

## The detected signals mean
- **`main` advanced by N commits** — other sessions committed to `main` while you worked. Even a
  clean git merge can be *semantically* wrong if they changed something your branch relies on.
- **Files BOTH sides changed** — the highest-risk set. Inspect every one.
- **Trial merge reports CONFLICTS** — textual conflicts you must resolve by hand.
- **Unmerged sibling worktree branch also edits your files** — another live session hasn't merged
  yet; coordinate so whoever merges second isn't blindsided.

## Procedure
1. **Establish your branch is green first** — run the scoped tests for your changes (see the
   project `CLAUDE.md` "running the suite" block). Reconcile from a known-good baseline.
2. **Bring the concurrent work in** — from the worktree, merge the target in:
   `git merge main` (or rebase onto it if that better fits the branch's history).
3. **Resolve every conflict with judgment, not by picking a side blindly.** For each conflicted
   or both-sides-touched file, read both versions and understand each change's *intent*. If the
   intent isn't obvious from the diff/commit messages, read the relevant session transcript:
   `/home/roots/.claude/projects/-home-roots-llm-d-benchmarking-agent/*.jsonl` (newest first;
   grep for the branch name, the file path, or the feature). Produce a unified result that
   preserves BOTH intents. Honour the project's non-negotiables (thin-code/thick-agent →
   judgment lives in `knowledge/` not Python `if/elif`; READ-ONLY upstream repos;
   prompt-cache byte-stability for `app/agent/prompt.py`).
4. **Validate promises kept** — this is the "after-the-fact" check the hook exists for. For each
   concurrent change that interacts with yours, confirm it actually did what its chat said it
   would (the interface it claimed to add exists, the rename is complete, the caller it promised
   to update was updated). If a session left a promise half-done and it breaks your branch, fix
   the seam here and note it; don't merge a known-broken integration.
5. **Re-run the FULL suite once** — the reconciled tree is new code; catch cross-session
   regressions. Green is the bar.
6. **Commit the resolution ON THIS BRANCH.** Stage specific paths (never `git add -A` at the
   monorepo root — it grabs the `.claude/worktrees/*` gitlinks). `main` must stay untouched.
7. **Re-arm the merge:**
   ```bash
   bash <project>/.claude/hooks/recon_lib.sh mark <WT_ROOT> main <BRANCH>
   touch <WT_ROOT>/.ready-to-merge
   ```
   (`<WT_ROOT>` = this worktree's root; the hold message printed the exact paths.) `mark` records
   a signature of the current concurrent state. When you next stop, the gate sees the signature
   matches, the trial merge is now clean, and the hook merges `--no-ff` and removes the worktree.

## Notes
- **If a sibling session commits again after you reconcile**, the signature changes and the gate
  re-holds on your next stop — re-run from step 2 against the new state. That's intended: it
  guarantees you never merge stale against a moving target.
- **The gate only triggers on real overlap.** Disjoint concurrent work merges straight through
  (no reconciliation needed). One-off bypass for the whole gate: `RECON_OFF=1`.
- This pairs with `parallel-fix-by-file-ownership` (partition work so overlaps are rare in the
  first place) and `qa-fleet` (the live-QA loop that produces concurrent sessions).
