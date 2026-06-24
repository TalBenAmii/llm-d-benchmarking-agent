# Project brain — reference (read on demand)

Slim **orientation hub** for the reference/historical material that doesn't belong in the
always-on `CLAUDE.md`. Read it when you need orientation; it is not loaded every session. The
active `CLAUDE.md` keeps the non-negotiable rules, the folder map, and the on-demand pointers.

Two focused references were split out of this file (load whichever the task needs):
- **[`CONFIG_AUDIT_LOG.md`](CONFIG_AUDIT_LOG.md)** — dated config / model-drift audit entries
  (the per-turn-config reorganizations + post-model-release reviews). Read before the next such review.
- **[`UPSTREAM_REUSE_PATHS.md`](UPSTREAM_REUSE_PATHS.md)** — where to look in the READ-ONLY
  `llm-d-benchmark/` repo (CLI entry, specs, harnesses/workloads, report schema, safe-preview commands).
  Read when generating configs or picking specs.

## What's built
Started as the `llm-d-benchmark` quickstart MVP (local kind cluster, CPU-only sim) — driven
end-to-end (probe → ensure repo → `install.sh --uv` → `standup --spec cicd/kind` → `smoketest` →
`run -l inference-perf -w sanity_random.yaml` → parse report → summarize → offer teardown),
**built and verified 2026-05-31**. It has since grown well past that (orchestrator, analyzer,
multi-harness compare, capacity pre-flight, history/trends, observability, one-command deploy).

- **Authoritative, evidence-backed feature inventory + how to verify each →** `FEATURES.md` (read first).
- **Remaining / deferred work →** `ROADMAP_V4.md` (only the DEFERRED phases remain).
- **Design rationale + MVP implementation-status record →** `docs/history/plan.md`.
- **Tool count is never hard-coded here** — `app/tools/registry.py` (`build_registry()`) is the only source of truth.

## Documentation map
The full docs index lives in **[`README.md`](README.md)** (every `docs/` page + the project-root
docs: `README.md`, `FEATURES.md`, `ROADMAP_V4.md`, `knowledge/`; plus `docs/history/plan.md`). Not repeated here.
Items the index doesn't list:
- `docs/history/llm-d-benchmarking-agent-proposal.md` — the original proposal / requirements (the "north star").
- `docs/INTERACTIVE_TEST_GUIDE.md` — follow-along runbook to drive every feature by hand with a real LLM.
- `docs/BENCHMARK_FEATURE_COVERAGE.md` — benchmark-CLI feature-coverage catalog (✅/🟡/⬜).
- `docs/USEFUL_REPO_DOCS.md` — curated index of which upstream `llm-d` / `llm-d-benchmark` docs matter and why.

**Agent brain** — `knowledge/*.md|*.yaml` hold all *judgment* (loaded at runtime; not docs to edit
casually). See `knowledge/CLAUDE.md` before editing them.

## Run locally
See **[`DEPLOYMENT.md`](DEPLOYMENT.md)** for running locally and in-cluster (config, secrets, RBAC,
observability). The short version: `cp .env.example .env` (fill `ANTHROPIC_API_KEY`; never commit it)
→ `pip install -e .` → `uvicorn app.main:app --reload` → http://127.0.0.1:8000; `pytest tests/` runs
hermetically without a key.
