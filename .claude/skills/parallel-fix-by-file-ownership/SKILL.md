---
name: parallel-fix-by-file-ownership
description: Playbook for large multi-bug fix/refactor tasks (>~200k tokens). Fan out subagents partitioned by DISJOINT FILE/DIRECTORY OWNERSHIP (not by bug), each sized ~100-200k tokens of work, then integrate with one full test run. Use when a fix spans many findings across many files and would overflow a single context.
---

# Parallel fix by file ownership

When a fix/refactor task spans many findings and would exceed ~200k tokens, decompose it
across **parallel subagents partitioned by disjoint file/directory ownership** rather than by
finding — so concurrent edits to a shared working tree never conflict and no merge step is
needed. Size each agent to ~100–200k tokens of work for good utilization.

**Why:** parallel agents on one tree are safe *iff* their file sets are disjoint. Partitioning
by bug causes the same hot file (e.g. `app/agent/prompt.py`, `knowledge/*`) to be edited by
multiple agents → conflicts/lost edits. Ownership partitioning sidesteps that entirely.

**How to apply:**
- List the hot files first; assign each file/dir to exactly ONE agent.
- Map every finding to the owner of the file it must change. A cross-cutting finding gets its
  code-side in one owner and behavior-side in another — never the same file edited twice.
- Tell each agent: stay in your lane; if a fix belongs in another agent's file, REPORT it
  (don't edit). Don't commit/push. Keep your scoped tests green.
- Respect project rules (here: thin-code/thick-agent → judgment goes in `knowledge/`, not
  Python if/elif; READ-ONLY upstream repos; prompt-cache byte-stability for `prompt.py`).
- Integrator runs the FULL suite once at the end to catch cross-agent regressions, then
  reports / commits.

Related: the `qa-fleet` skill (continuous live QA loop) produces the findings this method then
fixes in bulk.
