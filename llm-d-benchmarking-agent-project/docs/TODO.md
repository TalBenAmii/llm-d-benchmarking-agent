# TODO — informal working backlog

> Promoted from the loose monorepo-root `todo` scratchpad (2026-07-02). Author's shorthand,
> kept verbatim; prune items as they ship.

- allow to remove runs from trends
- fix Provenance bundle capture
- add response available feature that light up the session in sidebar
- make another view of sidebar sessions. make the new view as default. the new view should be
  sorted by last updated or last created (user can choose, it should work similar to how
  vscode's copilot chat works). user could switch to the other view (which is the divide by
  namespace view). it should also support the same sorting feature.
- find if there is an indication of command approval request that was timeout
- fix the agent interactive tool and capture images + make docs + demo video + presentation
  with bonus

## Dev-loop / tooling improvements (proposed 2026-07-05)

- **Bootstrap the git hooks (HIGH).** `.git/hooks/{pre-commit,pre-merge-commit}` — the main-branch ruff+pytest+dangling-skill merge gate — are local-only and un-versioned. A fresh clone or new machine silently loses the entire gate while `block_local_test_lint.sh` still forbids manual ruff/pytest → zero verification is possible. Add an idempotent `scripts/install-git-hooks.sh` that (re)writes them, call it from the install/setup flow, and note it in `tests/CLAUDE.md`.
- **Add mypy (+ optional coverage) to the local gate (MED).** The pre-commit hook runs ruff+pytest only; type regressions and coverage drops reach local `main` and only surface later in CI. Add a fast `mypy` step to the hook (already allowed by `block_local_test_lint.sh`), or a `pre-push` gate.

(Note: the broader global/`~/.claude` dev-loop proposals live in the `dev-loop-improvements` memory, not here.)
