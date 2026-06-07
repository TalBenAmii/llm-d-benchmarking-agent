# tests/ — running & writing the suite

Tests-local quick reference. The project `CLAUDE.md` has the full worktree mechanics; this is
the dir-scoped cheat sheet so the env gotchas aren't re-derived each session.

## Run the full suite
From the **primary** checkout:
```bash
.venv/bin/python -m pytest tests/
```
From a **worktree** (the primary `.venv` is an editable install pointing at the *primary* tree, so
PYTHONPATH must point at *your* worktree, and the empty sibling repos must be pointed back at primary):
```bash
cd <worktree>/llm-d-benchmarking-agent-project
PYTHONPATH=<worktree>/llm-d-benchmarking-agent-project \
REPOS_DIR=/home/tal/kind-quickstart-guide \
/home/tal/kind-quickstart-guide/llm-d-benchmarking-agent-project/.venv/bin/python -m pytest tests/
```
**Healthy baseline ≈ 1598 passed / ~20 skipped in ~15–20s.** Establish green BEFORE changing anything.

## Run a scoped subset
- One area: `pytest tests/test_orchestrator*.py` · one file: `pytest tests/test_allowlist.py` · one test: `pytest tests/test_foo.py::test_bar`.
- Flow replays (deterministic golden transcripts): `pytest tests/flows/`.
- The per-subsystem `CLAUDE.md` files list the exact scoped command for each area.

## Gotchas (the time-wasters)
- **Empty sibling repos in worktrees** (`conftest.py`): `llm-d/` + `llm-d-benchmark/` are untracked
  nested repos, EMPTY in any worktree → catalog/report tests fail unless `REPOS_DIR` points at primary.
- **`SIMULATE=0` is forced in `conftest.py`** — a dev `.env` with `SIMULATE=1` (or a live kind cluster)
  can deadlock the approval-gate tests. Don't override it in tests.
- **Per-test timeout** is set in `pyproject.toml` as a deadlock backstop; a single test should never approach it.
- **Never auto-run the live-LLM eval**: `LLM_EVAL_LIVE=1`, `tests/flows/test_flows_live.py`,
  `make validate-live` spend Max-plan quota → only on explicit user request. Plain `pytest` is safe and hermetic.
- **Never `git add -A` at the monorepo root** — it grabs `.claude/worktrees/*` gitlinks. Add specific paths.

## Fixtures / fakes worth knowing
- `conftest.py` — resolves the bench repo (`REPOS_DIR`-aware), schema/example paths, the allowlist, and a `tool_ctx` (ToolContext on real repos + an isolated temp workspace).
- `orchestrator_fakes.py` — in-memory `FakeKubeClient` + `make_job`/`make_pod`; the whole Job lifecycle runs with no cluster.
- `tests/integration/` — opt-in (`LLMD_SIM_INTEGRATION=1`); skipped by default.
