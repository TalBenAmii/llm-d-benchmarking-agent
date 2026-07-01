# app/tools/ — the agent tools (mechanism layer)

Tools validate their args against Pydantic schemas, gate mutating commands, and return a flat
JSON-serializable dict to the agent. The DEDICATED command tools (execute_llmdbenchmark, probes,
orchestrator) run allowlisted argv through the executor; the agent's ad-hoc `run_shell` tool runs
an arbitrary `bash -lc` string (classifier + approval gate, NOT the allowlist). `registry.py` is
the authoritative list. Judgment about *what to do with* results lives in `knowledge/`, not here.

## How to add a tool (the pattern to copy)
1. **Handler** — `app/tools/<name>.py`: `async def my_tool(ctx: ToolContext, *, arg: str) -> dict[str, Any]`.
2. **Input schema** — a Pydantic model in the `app/tools/schemas/` package (drop it in the module for
   the tool's family, e.g. `schemas/execute.py`); every `Field(..., description=...)` is **exposed to
   the LLM** in the JSON Schema, so write the description for the model (point it at
   `read_knowledge('<topic>')` for judgment), not as an impl note.
3. **Register** — in `registry.py::build_registry()` add a `ToolSpec(name, _DESCRIPTIONS[name],
   InputModel, handler)` and a `_DESCRIPTIONS[name]` entry. `dispatch()` (`registry.py`) validates
   `raw_input` against the model and **returns** `{"error": ...}` on a `ValidationError` (the agent
   self-corrects) — it does **not** raise.

## Local invariants
- **Read-only vs mutating is decided by the executor, not the tool.** Call `ctx.run_readonly(argv)`
  for probes (auto-runs) and `ctx.run_command(argv)` for mutations (routes through approval; rejection
  raises `ApprovalRejected`). A handler that calls neither just auto-runs (pure Python, e.g. analyze).
- **Raise `ToolError` for any non-retryable failure** (bad input, missing repo, allowlist denial) — the
  loop turns it into a clean `{"error": ...}`. Never raise *other* exceptions (they break the session).
- **An allowlist denial is defense, not a bug** — widen capability in `security/allowlist.yaml` (data),
  don't work around it. (The allowlist-vs-`run_shell` scope split → `app/security/CLAUDE.md`.)
- **Write only to `ctx.workspace`** (per-session). Never write into the READ-ONLY repos or `/tmp`.
- **Return flat, JSON-serializable, secret-free dicts.** Pass HF tokens via `ctx.run_command(..., env=…)`,
  never in argv or the result. Emitted command events carry argv only, never env.
- **After cloning repos, call `ctx.catalog(refresh=True)`** or later tools see the stale (empty) catalog.

## Infra files (not tools)
- `registry.py` — `build_registry()` (name→`ToolSpec`) + `dispatch()` (validate → handler). Authoritative.
- `context.py` — `ToolContext` DI container + thin `run_command`/`run_readonly` delegators; `ToolError`/`ApprovalRejected`/`QuotaError`.
- `command_exec.py` — `CommandExecutor`: validate → quota → approval → run → record. Tools don't touch it directly.
- `schemas/` — package of Pydantic input models, one module per tool family (`execute.py`, `orchestrate.py`, `probe.py`, `analysis.py`, `config.py`, `command.py`, `provenance.py`, `autotune.py`, `docs.py`).
- `probe_parse.py` — pure parser for `probe.py` output. · `json_tail.py` — tail-of-JSON helper.
- `gated_access.py` — gated-model deploy refusal (`gated_block`) at the command chokepoint; wired into `command_exec.py`/`shell.py`, verdicts recorded by the capacity bridge.
- `catalog.py` — `build_catalog()`: live spec/harness/workload listing from the bench repo (+ `catalog_for_allowlist`); used by `context.py`/`workload_profile.py`.

## Tool index (grouped by workflow phase)
The files sit flat; this is the map. `registry.py` is the source of truth for the registered set/order.
Most tool schemas are grouped (`registry._TOOL_GROUPS`: setup/run/analyze/advanced) and HIDDEN by
default; only the `registry.STARTER_KIT` is shown. The model loads a group with `tool_loader.py`
(load_tools) when a request needs it — see `app/agent/CLAUDE.md`.
- **Probe & discover** — `probe.py` (probe_environment · list_catalog · advise_accelerators) · `workload_profile.py` (inspect_workload_profile · estimate_run_duration) · `discover.py` (discover_stack) · `capacity.py` (check_capacity) · `check_endpoint_readiness` lives in `app/readiness/`.
- **Knowledge & advice** — `knowledge_access.py` (read_knowledge · search_knowledge · read_repo_doc · fetch_key_docs) · `convert_guide.py` (convert_guide_to_scenario) · `suggest.py` (suggest_next_steps) · `tool_loader.py` (load_tools — loads a hidden tool group on demand).
- **Plan, config & setup** — `plan.py` (propose_session_plan) · `repos.py` (ensure_repos · run_setup) · `config_artifact.py` (write_and_validate_config) · `doe.py` (generate_doe_experiment) · `hf_secret.py` (provision_hf_secret).
- **Run & orchestrate** — `execute.py` (execute_llmdbenchmark) · `shell.py` (run_shell — the agent's always-on ad-hoc command tool) · `orchestrate.py` (orchestrate_benchmark_run · orchestrate_sweep) · `observe.py` (observe_run_metrics) · `cancel.py` (cancel_run) · `manage_runs.py` (manage_orchestrated_runs) · `resilience.py` (run_resilience_drill).
- **Analyze & results** — `report_locate.py` (locate_and_parse_report) · `analyze.py` (analyze_results) · `compare.py` (compare_reports) · `multiharness.py` (compare_harness_runs) · `history.py` (result_history) · `aggregate_runs.py` (aggregate_runs) · `autotune.py` (autotune_search) · `reproducibility.py` (export_run_bundle · reproduce_run).

## Gotchas
- Schema validation errors are **returned, not raised** — surface your own enum/range errors as a dict with `"error"`, don't raise mid-handler.
- A `timeout_s` declared in `allowlist.yaml` **overrides** any `timeout=` you pass.
- Result dicts are not schema-checked — a typo'd key silently misleads the agent; assert key presence in tests.

## Audit note (don't re-litigate)
A 2026-06-19 verified audit found the set genuinely lean — every result-cluster tool, `run_shell` (ad-hoc)
vs `execute_llmdbenchmark` (the CLI), and `fetch_key_docs` vs `read_repo_doc` has a distinct role pinned by a live-eval flow;
do NOT re-propose merging them. DEFERRED (only if advanced-GPU-flag coverage is wanted): `execute_llmdbenchmark`
flag passthroughs (wva/deep/serviceaccount/release/non_admin/envvarspod/full_infra) — each needs an allowlist +
`test_allowlist.py`/`test_command_events.py` entry, and the `-d`/`-r` flag collisions need disjoint keys.

## Scoped tests
```bash
pytest tests/test_new_tools.py tests/test_schemas.py tests/test_command_events.py tests/test_allowlist.py
```
