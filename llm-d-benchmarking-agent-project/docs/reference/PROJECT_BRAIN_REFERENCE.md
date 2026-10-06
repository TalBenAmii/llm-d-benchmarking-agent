# Project brain: reference (read on demand)

This page collects project history and reference links. The main `CLAUDE.md` contains the
working rules and folder map; this page is read only when needed.

One focused reference was split out of this file (load it when the task needs it):
- **[`UPSTREAM_REUSE_PATHS.md`](UPSTREAM_REUSE_PATHS.md)**: where to look in the read-only
  `llm-d-benchmark/` repo (CLI entry, specs, harnesses/workloads, report schema, safe-preview commands).
  Read when generating configs or picking specs.

## What's built
Started as the `llm-d-benchmark` quickstart MVP (local kind cluster, CPU-only sim), driven
end-to-end (probe → ensure repo → `install.sh --uv` → `standup --spec cicd/kind` → `smoketest` →
`run -l inference-perf -w sanity_random.yaml` → parse report → summarize → offer teardown),
built and verified 2026-05-31. It has since grown well past that (orchestrator, analyzer,
multi-harness compare, capacity pre-flight, history/trends, observability, one-command deploy).

- **Features and verification:** `FEATURES.md` (read first).
- **Per-upstream-feature coverage status:** `docs/reference/BENCHMARK_FEATURE_COVERAGE.md`.
- **Design rationale + MVP implementation-status record:** git history only (`docs/history/plan.md`, removed 2026-07-10).
- **Tool count is never hard-coded here:** `app/tools/registry.py` (`build_registry()`) is the only source of truth.
- **Tool review (2026-06-19):** the result tools, `run_shell`, `execute_llmdbenchmark`,
  `fetch_key_docs`, and `read_repo_doc` each serve a distinct purpose covered by a live-eval
  flow. Keep them separate. Advanced GPU flag support remains deferred:
  `wva`, `deep`, `serviceaccount`, `release`, `non_admin`, `envvarspod`, and `full_infra`.
  Each addition needs a command-policy entry and coverage in `test_command_policy.py`
  and `test_command_events.py`. The `-d` and `-r` flag collisions need separate keys.

## Documentation map
The full docs index lives in **[`README.md`](../README.md)** (every `docs/` page plus the repo-root
`README.md`, `docs/reference/FEATURES.md`, `knowledge/`). Not repeated here.

**Agent brain**: `knowledge/*.md|*.yaml` hold all judgment (loaded at runtime; not docs to edit
casually). See `knowledge/CLAUDE.md` before editing them.

## Run locally
See **[`DEPLOYMENT.md`](../guides/DEPLOYMENT.md)** for running locally and in-cluster (config, secrets, RBAC,
observability); `scripts/run.sh` is the quickest launch.
