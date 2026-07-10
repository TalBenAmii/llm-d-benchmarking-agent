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
- One area: `pytest tests/test_orchestrator*.py` · one file: `pytest tests/test_allowlist.py` · one test: `pytest tests/test_foo.py::test_bar`.
- Flow replays (deterministic golden transcripts): `pytest tests/flows/`.
- The per-subsystem `CLAUDE.md` files list the exact scoped command for each area.

## Where the tests live (area → files)
The unit suite is **flat** (`tests/test_*.py`, ~120 files); names mirror the `app/` subsystem they
exercise. Forward-lookup map (use it to find "which tests cover X"; `git grep` the symbol for the rest):
- **tools** (`app/tools/`) — `test_<toolname>.py` mirrors each tool: `test_analyze*.py`, `test_doe.py`, `test_workload_profile.py`, `test_catalog.py`, `test_repos.py`, `test_hf_secret.py`, `test_command_events.py`, `test_convert_guide.py`, `test_multiharness.py`, `test_aggregate_runs.py`, `test_manage_runs.py`, plus `test_new_tools.py` / `test_schemas.py` (registry + schema coverage).
- **orchestrator** — `test_orchestrator*.py`, `test_jobs_api.py`.
- **agent loop** — `test_deterministic_msgs.py`, `test_context_mgmt.py`, `test_tool_result_budget.py`, `test_events.py`, `test_loop.py`, `test_streaming_turn.py`, `test_suggest*.py`/`test_suggestions.py`, `test_ws*.py`, `test_prewarm.py`.
- **validation gates** — `test_report_validation.py`, `test_standard_metrics.py`, `test_runconfig_roundtrip.py`, `test_scenario_overrides.py`, `test_model_override.py`.
- **security / allowlist** — `test_allowlist.py`, `test_api_trust.py`, `test_governance.py`, `test_concurrency.py`, `test_sessions.py`, `test_run_shell.py`, `test_auto_approve.py`, `test_qafix_infra.py`, `test_product_boundary.py`.
- **capacity** — `test_capacity.py`, `test_capacity_gated.py`.
- **readiness** — `test_endpoint_readiness.py`, `test_gateway_readiness.py`, `test_serving_readiness.py`, `test_gateway_class.py`.
- **packaging / sharing** — `test_packaging.py`, `test_report_card.py`, `test_share.py`, `test_shared_chat_export.py`, `test_cloud_results_sink.py`.
- **storage** — `test_retention.py`, `test_results_store.py`, `test_history.py`, `test_run_lifecycle.py`, `test_provenance.py`.
- **observability** — `test_metrics.py`, `test_cot_trace.py`, `test_logging.py`, `test_tracing_config.py`, `test_resource_*.py`, `test_monitoring_activate.py`, `test_ops_docs.py`.
- **llm providers** — `test_agent_sdk_provider.py`, `test_provider_pack.py`, `test_llm_caching_usage.py`.
- **UI / HTTP e2e** — `test_ui.py`, `test_readyz.py`, `test_static_cache.py`, `test_streaming_turn.py`.
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
  multi-step DEPLOY walks only in simulate. ⚠️ In a worktree the gitignored `.env` is absent → the provider raises
  → every live test SKIPS silently; `cp <primary>/.env <worktree>/.env` first.
  - **Non-pytest path** (use this when `pytest` is hook-blocked by hand): `scripts/eval/validate_flows.py
    --live` and `--simulate` drive the SAME harness/scoring without pytest — `LLM_EVAL_LIVE=1 python
    scripts/eval/validate_flows.py --simulate`. Still spends quota; still needs the worktree `.env`.
  - **For any REAL run use the ISOLATED runner**: `make validate-live-iso` / `make validate-simulate-iso`
    (`scripts/eval/run_eval_isolated.sh`) — one process per flow (fresh SDK subprocess) under an EXTERNAL
    kernel-level `timeout`, so a stuck flow is killed, logged `TIMEOUT`, and the run continues. The
    in-process per-call watchdog (`LLM_EVAL_CALL_TIMEOUT`) and per-flow cap (`LLM_EVAL_FLOW_TIMEOUT`)
    are first-line only and DEFEATABLE (a frozen event loop never fires them; the shared SDK subprocess
    can deadlock between flows) — the force-kill mechanics + full rationale live in `docs/reference/VALIDATION.md`
    §"Isolated eval runner". `FLOWS="a b"` runs a subset; logs → `workspace/eval-logs/`; ⚠️ in a
    worktree set `REPOS_DIR` to the primary checkout (empty siblings).
  - **`load_tools` group scoring** (`score_flow`): the live eval verifies the model loaded the
    RIGHT tool group(s) for the grouped tools a flow requires; an EXTRA group is a NOTE (not a
    failure), never loading a needed one IS a failure. Hermetic guards in `tests/flows/test_eval_harness.py`.
  - **Skill-usage eval** (`tests/eval/simulate/test_skill_usage_live.py`, same `LLM_EVAL_LIVE=1` gate): asserts the
    agent grounds each operation in the RIGHT doc BEFORE acting, matching the spec-aware `skill_gate`: a
    kind/CPU-sim ask → `fetch_key_docs(task="quickstart")` (the runbook, NOT deploy_skill), a GPU/guide
    deploy/benchmark/teardown → its own `*_skill`, compare → `compare_skill`, autoscaling/WVA →
    `wva_skill` (via `fetch_key_docs(task=<key>)` or a `read_repo_doc` under the route's read prefix). 6
    scenarios × `SKILL_EVAL_RUNS` runs each (default 3, majority passes; `=1` = cheap smoke); ⚠️ worktree
    needs `REPOS_DIR=<primary>` (empty siblings, per the gotcha above). E.g. `LLM_EVAL_LIVE=1 REPOS_DIR=<primary>
    SKILL_EVAL_RUNS=1 .venv/bin/python -m pytest tests/eval/simulate/test_skill_usage_live.py -v`.
  - **Skill-gate is INERT under pytest, LIVE under the non-pytest harness** — the autouse
    `_ground_skills_by_default` fixture (`conftest.py`) pre-grounds every `ToolContext`, so the
    skill-grounding gate (`app/tools/run/skill_gate.py`) never fires in `pytest`. But `scripts/eval/validate_flows.py`
    / `scripts/eval/run_eval_isolated.sh` load NO conftest, so the gate runs LIVE there: a MUTATING flow must
    ground ITSELF (fetch the skill) or its plan/standup/run is refused. The golden transcripts now do
    (`fetch_key_docs(task="quickstart")` on `cicd/kind`; `deploy_skill`+`benchmark_skill` on a guide) and
    the live eval's real model grounds itself. Do NOT narrow the fixture — deterministic flows that don't
    script a fetch rely on it staying inert.
- **Self-eval (`tests/eval/live/`)**: the LLM judge (`test_judge_live.py`) + bug-hunter
  (`test_bughunt_live.py`) share the SAME `LLM_EVAL_LIVE` switch (bughunt also needs `BUGHUNT=1`)
  and SPEND quota → never auto-run them. `make eval-shadow` is the always-safe hermetic entry
  (the deterministic shadow/oracle tests run in plain `pytest` for free).
- **Never `git add -A` at the monorepo root** — it grabs `.claude/worktrees/*` gitlinks. Add specific paths.

## Fixtures / fakes worth knowing
- `conftest.py` — resolves the bench repo (`REPOS_DIR`-aware), schema/example paths, the allowlist, and a `tool_ctx` (ToolContext on real repos + an isolated temp workspace).
- `_helpers.py` — shared verbatim input-builders (`_real_repo_ctx`/`_ctx`/`_session`/`_approve_all`/`_argv`); import these instead of re-pasting a ToolContext/Session/argv builder into a new test.
- `orchestrator_fakes.py` — in-memory `FakeKubeClient` + `make_job`/`make_pod`; the whole Job lifecycle runs with no cluster.
- `tests/integration/` — opt-in (`LLMD_SIM_INTEGRATION=1`); skipped by default.
