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
**Healthy baseline ≈ 1598 passed / ~20 skipped in ~15–20s.** No need to establish a green baseline when
you branch — feature branches aren't gated; the `main`-only hook verifies green at merge.

## Run a scoped subset
- One area: `pytest tests/test_orchestrator*.py` · one file: `pytest tests/test_allowlist.py` · one test: `pytest tests/test_foo.py::test_bar`.
- Flow replays (deterministic golden transcripts): `pytest tests/flows/`.
- The per-subsystem `CLAUDE.md` files list the exact scoped command for each area.

## Where the tests live (area → files)
The unit suite is **flat** (`tests/test_*.py`, ~120 files); names mirror the `app/` subsystem they
exercise. Forward-lookup map (use it to find "which tests cover X"; `git grep` the symbol for the rest):
- **tools** (`app/tools/`) — `test_<toolname>.py` mirrors each tool: `test_analyze*.py`, `test_autotune.py`, `test_doe.py`, `test_workload_profile.py`, `test_catalog.py`, `test_repos.py`, `test_hf_secret.py`, `test_command_events.py`, `test_convert_guide.py`, `test_multiharness.py`, `test_aggregate_runs.py`, `test_manage_runs.py`, plus `test_new_tools.py` / `test_schemas.py` (registry + schema coverage).
- **orchestrator** — `test_orchestrator*.py`, `test_chaos_injection.py`, `test_resilience*.py`, `test_jobs_api.py`.
- **agent loop** — `test_deterministic_msgs.py`, `test_context_mgmt.py`, `test_tool_result_budget.py`, `test_events.py`, `test_loop.py`, `test_streaming_turn.py`, `test_suggest*.py`/`test_suggestions.py`, `test_ws*.py`, `test_prewarm.py`.
- **validation gates** — `test_report_validation.py`, `test_standard_metrics.py`, `test_runconfig_roundtrip.py`, `test_scenario_overrides.py`, `test_model_override.py`.
- **security / allowlist** — `test_allowlist.py`, `test_api_trust.py`, `test_governance.py`, `test_concurrency.py`, `test_sessions.py`, `test_run_shell.py`, `test_auto_approve.py`, `test_qafix_infra_*.py`, `test_product_boundary.py`.
- **capacity** — `test_capacity.py`, `test_capacity_gated.py`.
- **readiness** — `test_endpoint_readiness.py`, `test_gateway_readiness.py`, `test_serving_readiness.py`, `test_gateway_class.py`.
- **packaging / sharing** — `test_packaging.py`, `test_report_card.py`, `test_share.py`, `test_shared_chat_export.py`, `test_gist_publish.py`, `test_publish_shared_chat.py`, `test_cloud_results_sink.py`.
- **storage** — `test_retention.py`, `test_results_store.py`, `test_history.py`, `test_run_lifecycle.py`, `test_provenance.py`.
- **observability** — `test_metrics.py`, `test_cot_trace.py`, `test_logging.py`, `test_tracing_config.py`, `test_resource_*.py`, `test_monitoring_activate.py`, `test_ops_docs.py`.
- **llm providers** — `test_agent_sdk_provider.py`, `test_provider_pack.py`, `test_llm_caching_usage.py`.
- **UI / HTTP e2e** — `test_ui_*.py`, `test_readyz.py`, `test_static_cache.py`, `test_streaming_turn.py`.
- **subdirs** — `tests/flows/` (golden-transcript replays) · `tests/eval/` (LLM-judge/bughunt — gated, never auto-run) · `tests/integration/` (opt-in).

## Gotchas (the time-wasters)
- **Empty sibling repos in worktrees** (`conftest.py`): `llm-d/` + `llm-d-benchmark/` are untracked
  nested repos, EMPTY in any worktree → catalog/report tests fail unless `REPOS_DIR` points at primary.
- **`SIMULATE=0` is forced in `conftest.py`** — a dev `.env` with `SIMULATE=1` (or a live kind cluster)
  can deadlock the approval-gate tests. Don't override it in tests.
- **Per-test timeout** is set in `pyproject.toml` as a deadlock backstop; a single test should never approach it.
- **Never auto-run the live-LLM eval**: `LLM_EVAL_LIVE=1`, `tests/flows/test_flows_live.py`,
  `make validate-live` spend Max-plan quota → only on explicit user request. Plain `pytest` is safe and hermetic.
  Two modes (both gated on explicit request): `LLM_EVAL_LIVE=1 pytest tests/flows/test_flows_live.py` (live set)
  and `LLM_EVAL_LIVE=1 LLM_EVAL_SIMULATE=1 pytest …` (simulate set) — error/safety flows are honest only live,
  multi-step DEPLOY walks only in simulate. ⚠️ In a worktree the gitignored `.env` is absent → the provider raises
  → every live test SKIPS silently; `cp <primary>/.env <worktree>/.env` first.
- **Self-eval (`tests/eval/`)**: the LLM judge (`test_judge_live.py`) + bug-hunter
  (`test_bughunt_live.py`) share the SAME `LLM_EVAL_LIVE` switch (bughunt also needs `BUGHUNT=1`)
  and SPEND quota → never auto-run them. `make eval-shadow` is the always-safe hermetic entry
  (the deterministic shadow/oracle tests run in plain `pytest` for free).
- **Never `git add -A` at the monorepo root** — it grabs `.claude/worktrees/*` gitlinks. Add specific paths.

## Fixtures / fakes worth knowing
- `conftest.py` — resolves the bench repo (`REPOS_DIR`-aware), schema/example paths, the allowlist, and a `tool_ctx` (ToolContext on real repos + an isolated temp workspace).
- `_helpers.py` — shared verbatim input-builders (`_real_repo_ctx`/`_ctx`/`_session`/`_approve_all`/`_argv`); import these instead of re-pasting a ToolContext/Session/argv builder into a new test.
- `orchestrator_fakes.py` — in-memory `FakeKubeClient` + `make_job`/`make_pod`; the whole Job lifecycle runs with no cluster.
- `tests/integration/` — opt-in (`LLMD_SIM_INTEGRATION=1`); skipped by default.
