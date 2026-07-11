# Project brain: reference (read on demand)

Slim orientation hub for the reference/historical material that doesn't belong in the
always-on `CLAUDE.md`. Read it when you need orientation; it is not loaded every session. The
active `CLAUDE.md` keeps the non-negotiable rules, the folder map, and the on-demand pointers.

One focused reference was split out of this file (load it when the task needs it):
- **[`UPSTREAM_REUSE_PATHS.md`](UPSTREAM_REUSE_PATHS.md)**: where to look in the READ-ONLY
  `llm-d-benchmark/` repo (CLI entry, specs, harnesses/workloads, report schema, safe-preview commands).
  Read when generating configs or picking specs.

## What's built
Started as the `llm-d-benchmark` quickstart MVP (local kind cluster, CPU-only sim), driven
end-to-end (probe → ensure repo → `install.sh --uv` → `standup --spec cicd/kind` → `smoketest` →
`run -l inference-perf -w sanity_random.yaml` → parse report → summarize → offer teardown),
built and verified 2026-05-31. It has since grown well past that (orchestrator, analyzer,
multi-harness compare, capacity pre-flight, history/trends, observability, one-command deploy).

- **Authoritative, evidence-backed feature inventory + how to verify each:** `FEATURES.md` (read first).
- **Per-upstream-feature coverage status:** `docs/reference/BENCHMARK_FEATURE_COVERAGE.md`.
- **Design rationale + MVP implementation-status record:** git history only (`docs/history/plan.md`, removed 2026-07-10).
- **Tool count is never hard-coded here:** `app/tools/registry.py` (`build_registry()`) is the only source of truth.

## Documentation map
The full docs index lives in **[`README.md`](../README.md)** (every `docs/` page plus the repo-root
`README.md`, `docs/reference/FEATURES.md`, `knowledge/`). Not repeated here.

**Agent brain**: `knowledge/*.md|*.yaml` hold all judgment (loaded at runtime; not docs to edit
casually). See `knowledge/CLAUDE.md` before editing them.

## Run locally
See **[`DEPLOYMENT.md`](../guides/DEPLOYMENT.md)** for running locally and in-cluster (config, secrets, RBAC,
observability); `scripts/run.sh` is the quickest launch.
