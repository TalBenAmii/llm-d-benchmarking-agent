# tests/ — running & writing the suite

Tests-local quick reference. The `finish-implementation` skill has the finish-loop/merge mechanics;
this is the dir-scoped env cheat sheet so the gotchas aren't re-derived each session.

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
REPOS_DIR=<repo-root> \
<repo-root>/llm-d-benchmarking-agent-project/.venv/bin/python -m pytest tests/
```
**Healthy baseline: the full suite is green in ~15–20s** (current pass/skip counts = the merge-gate
hook's own output — don't trust numbers written here). No need to establish a green baseline when
you branch — feature branches aren't gated; the `main`-only hook verifies green at merge.

**Fresh clone / new machine: run `scripts/install/install-git-hooks.sh` first.** `.git/hooks` is not
version-controlled, so a clone starts with NO merge gate — run the installer to (re)write the
`main`-only ruff + mypy + pytest + dangling-skill-ref hooks (`pre-commit` + `pre-merge-commit`).

## Run a scoped subset
- One area: `pytest tests/orchestrator/` · one file: `pytest tests/platform/test_command_policy.py` · one test: `pytest tests/tools/test_foo.py::test_bar`.
- Flow replays (deterministic golden transcripts): `pytest tests/flows/`.
- The per-subsystem `CLAUDE.md` files list the exact scoped command for each area.

## Where the tests live (bucket → app subsystem)
The unit suite is grouped into four subpackages; a file's bucket = the **dominant `app.*` import**
(ties / no `app.*` import → `platform/`). `git grep` the symbol to find "which test covers X".
- `tests/agent/` — `app/agent` + `app/llm`: the SDK-native engine, events, sessions, WS, model catalog, prompt stability.
- `tests/tools/` — `app/tools`: registry + schemas, the setup/run/analyze/access handlers, command exec.
- `tests/orchestrator/` — `app/orchestrator` + `app/capacity` + `app/readiness`: jobs, capacity pre-flight, readiness probes, infra preconditions.
- `tests/platform/` — everything else: config/dig/web/main, security + command policy, storage, packaging/sharing, observability, validation gates, knowledge-file checks, product boundary, UI/HTTP e2e.
- Shared plumbing stays at the root: `conftest.py` · `_helpers.py` · `_auth.py` · `orchestrator_fakes.py`.
- **subdirs** — `tests/flows/` (golden-transcript replays + shared harness/flows + hermetic skill-grounding guards — each golden operation-flow must fetch its grounding doc first (its `*_skill`, or the `quickstart` runbook on the kind/CPU-sim path)) · `tests/eval/` (live-LLM agent evals split into `live/` = default-live/real-app + `simulate/` = the SIMULATE-only skill-usage eval, plus hermetic shadow/oracle guards directly under `eval/` — gated, never auto-run) · `tests/integration/` (opt-in).

## Gotchas (the time-wasters)
- **Empty sibling repos in worktrees** (`conftest.py`): `llm-d/` + `llm-d-benchmark/` are untracked
  nested repos, EMPTY in any worktree → catalog/report tests fail unless `REPOS_DIR` points at primary.
- **`SIMULATE=0` is forced in `conftest.py`** — a dev `.env` with `SIMULATE=1` (or a live kind cluster)
  can deadlock the approval-gate tests. Don't override it in tests.
- **Per-test timeout** is set in `pyproject.toml` as a deadlock backstop; a single test should never approach it.
- **Never auto-run the live-LLM eval**: `LLM_EVAL_LIVE=1`, `tests/eval/live/test_flows_live.py`,
  `make validate-live` spend Max-plan quota → only on explicit user request. Plain `pytest` is safe and hermetic.
  Two modes (both gated on explicit request): `LLM_EVAL_LIVE=1 pytest tests/eval/live/test_flows_live.py` (live set)
  and `LLM_EVAL_LIVE=1 LLM_EVAL_SIMULATE=1 pytest …` (simulate set) — error/safety flows are honest only live,
  multi-step DEPLOY walks only in simulate. Live turns run on the logged-in `claude` CLI (keyless);
  a worktree's missing `.env` only matters if it carried a non-default `LLM_PROVIDER`/`AGENT_SDK_*`.
  - **Non-pytest path** (use this when `pytest` is hook-blocked by hand): `scripts/eval/validate_flows.py
    --live` / `--simulate` drives the SAME harness/scoring without pytest. Still spends quota; still needs the worktree `.env`.
  - **Any REAL run → the ISOLATED runner**: `make validate-live-iso` / `make validate-simulate-iso` — one
    process per flow under an EXTERNAL kernel-level `timeout` (the in-process watchdogs are DEFEATABLE);
    mechanics + rationale → `docs/reference/VALIDATION.md` §"Isolated eval runner". `FLOWS="a b"` runs a
    subset; logs → `workspace/eval-logs/`; ⚠️ worktree: copy `.env` + set `REPOS_DIR=<primary>` (empty siblings).
  - **Skill-usage eval** (`tests/eval/simulate/test_skill_usage_live.py`, same gate): asserts the agent grounds
    each op in the RIGHT doc BEFORE acting, matching the spec-aware `skill_gate` (kind/CPU-sim →
    `quickstart`, NOT deploy_skill); scenarios/knobs → `docs/reference/VALIDATION.md`. ⚠️ worktree needs `REPOS_DIR=<primary>`.
  - **Skill-gate is INERT under pytest, LIVE under `scripts/eval/*`** (no conftest there): the autouse
    `_ground_skills_by_default` fixture (`conftest.py`) pre-grounds every `ToolContext` — do NOT narrow it
    (deterministic flows rely on it); under the eval scripts a MUTATING flow must fetch its grounding doc or it's refused.
- **Self-eval (`tests/eval/live/`)**: the LLM judge (`test_judge_live.py`) + bug-hunter
  (`test_bughunt_live.py`) share the SAME `LLM_EVAL_LIVE` switch (bughunt also needs `BUGHUNT=1`)
  and SPEND quota → never auto-run them. `make eval-shadow` is the always-safe hermetic entry
  (the deterministic shadow/oracle tests run in plain `pytest` for free).
- **Never `git add -A` at the monorepo root** — it grabs `.claude/worktrees/*` gitlinks. Add specific paths.

## Fixtures / fakes worth knowing
- `conftest.py` — resolves the bench repo (`REPOS_DIR`-aware), schema/example paths, the command policy, and a `tool_ctx` (ToolContext on real repos + an isolated temp workspace).
- `_helpers.py` — shared verbatim input-builders (`_real_repo_ctx`/`_ctx`/`_session`/`_approve_all`/`_argv`); import these instead of re-pasting a ToolContext/Session/argv builder into a new test.
- `orchestrator_fakes.py` — in-memory `FakeKubeClient` + `make_job`/`make_pod`; the whole Job lifecycle runs with no cluster.
- `tests/integration/` — opt-in (`LLMD_SIM_INTEGRATION=1`); skipped by default.
