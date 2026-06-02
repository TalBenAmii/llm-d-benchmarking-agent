# Benchmark feature coverage (runtime pointer)

**Purpose.** A compact map of which `llm-d-benchmark` features this agent covers, so the
agent can answer "can you do X?" honestly and point at the canonical catalog. Read on demand.

## Legend
- ✅ covered (agent drives or reimplements it) · 🟡 partial · ⬜ missing (documented upstream, not surfaced)
- Optional? Y/N · Default on/off/n-a

## Coverage summary (67 features: 35 ✅ / 16 🟡 / 16 ⬜)

| Area | Description | ✅ | 🟡 | ⬜ |
|---|---|---:|---:|---:|
| A | Lifecycle subcommands, CLI flags & deploy options | 17 | 11 | 6 |
| B | Configuration system | 5 | 1 | 0 |
| C | Workloads, harnesses & run/harness contract | 2 | 0 | 2 |
| D | Design of Experiments | 1 | 0 | 0 |
| E | Results parsing, analysis, comparison & history | 3 | 2 | 2 |
| F | Observability | 2 | 1 | 2 |
| H | Utilities & CLI internals | 4 | 1 | 1 |
| I | Project meta | 1 | 0 | 3 |

**Headline gap:** benchmark metrics collection (`--monitoring`) is 🟡 — the consumer ships
(`app/validation/report.py` + `knowledge/standard_metrics.yaml`, Phase 25) but the producer
(`--monitoring` / `metricsScrapeEnabled`) is never activated, so `results.observability` is
empty in practice. Closing it is ROADMAP_V4 Phase 27.

## Recommended default-on (with a knowledge-driven opt-out)
- **`--monitoring`** — light up `results.observability` (KV-cache hit rate, schedule delay,
  GPU util) the report already knows how to parse. Default ON; emit `--no-monitoring` on
  Kind / clusters lacking Prometheus-operator CRDs. Decision lives in `knowledge/observability.md`.

## Optional to surface to users (offer when their context calls for it)
- Model override `-m/--models` · cluster access `-k`/URL/token · HuggingFace gated-model
  secret · gateway class `--gateway-class` · multi-stack `--stack`/`--parallel` · WVA
  `-u/--wva` (OpenShift-only) · cloud results sink `-r gs://`/`s3://` · local `--analyze`
  plot families · kustomize `kustomize.*` config block · distributed tracing `tracing:` block.

Full catalog: docs/BENCHMARK_FEATURE_COVERAGE.md
