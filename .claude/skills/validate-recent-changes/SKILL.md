---
name: validate-recent-changes
description: After-the-fact validator for parallel/recent work. Reads the recent session transcripts, distills what each session REQUESTED, CLAIMED to have done, and ACTUALLY edited, then verifies against the live code + git that every promised change was really made and that concurrent sessions didn't collide or undo each other. Use to audit a burst of parallel-agent work, confirm "is everything actually implemented?", or hunt cross-session conflicts/mistakes after the fact.
---

# Validate recent changes (after the fact)

Run this when several agents worked in parallel (or across recent sessions) and you want to be
sure **what was promised was actually delivered**, nothing collided, and no session silently
undid another's change. It's the standalone, on-demand counterpart to the merge-time
`reconcile-before-merge` gate — invoke it any time, no worktree or sentinel required.

## Step 1 — get the digest (mechanism)
From the repo root:
```bash
python3 .claude/skills/validate-recent-changes/session_digest.py --hours 12
```
It prints, per recent session: the branch, time window, **requested** (user prompts),
**claimed** (the final assistant summary), **files touched** (Edit/Write counts), and **commits**
— then a **cross-session file-collision** table (the same logical file edited by >1 session, with
worktree copies normalized to one repo-relative path).

Flags: `--hours N` (widen/narrow the window — it's relative to the most-recent session, so it
works regardless of wall-clock), `--max-sessions N`, `--branch BR` (only sessions on a branch
matching BR), `--json` (raw structure for programmatic use).

## Step 2 — validate each session's claim against reality (judgment)
For every session in the digest, treat "claimed" as a hypothesis, not truth. Verify:
- **Promised edits exist.** For each change the user asked for or the summary claims, open the
  file(s) and confirm the code is actually there and correct — not a stub, not reverted. `git log
  --oneline -- <path>` and `git show` confirm it landed and survived later commits.
- **Claim ⊇ request.** Cross-check the *requested* items against what was touched. A request with
  no corresponding edit/commit is an **unkept promise** — flag it.
- **No silent regressions.** If the summary claims X but tests/behavior say otherwise, say so with
  the evidence. Run the suite if a change is load-bearing (worktree-aware invocation in the
  project `CLAUDE.md`).

## Step 3 — resolve cross-session collisions (judgment)
For every entry in the collision table (same logical file, multiple sessions):
- Diff what each session did to that file and decide whether both intents are present in the
  current version, or whether the later write **clobbered** the earlier one. Use `git log -p --
  <path>` to see the order changes landed.
- If intent isn't clear from the diff, read that session's transcript for the *why*
  (`/home/roots/.claude/projects/-home-roots-llm-d-benchmarking-agent/<sid>.jsonl`; the digest
  prints each `<sid>`). Grep it for the file path or feature.
- When a collision lost work, reconstruct the union of both intents and propose/apply the fix
  (respecting project rules — thin-code/thick-agent, READ-ONLY upstream repos, prompt-cache
  byte-stability for `app/agent/prompt.py`). If sessions are still in flight on overlapping files,
  the merge will be caught by `reconcile-before-merge` too — note it.

## Step 4 — report
Produce a short verdict table: per requested item → **delivered / partial / missing**, plus a
collisions section (resolved / needs-action). Lead with anything broken or unkept; don't bury it.
Don't claim "all implemented" unless you actually opened the code and confirmed it.

## Notes
- Pure read/analysis until Step 3 applies a fix — safe to run any time.
- Pairs with `reconcile-before-merge` (the live merge gate) and `parallel-fix-by-file-ownership`
  (partition work to avoid collisions up front). This skill is the *audit* that catches what
  slipped through.
- The digest's "requested/claimed" are heuristic extracts from the transcript — when in doubt,
  read the raw `<sid>.jsonl`; it's the source of truth.
