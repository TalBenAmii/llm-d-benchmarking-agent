# Benchmark feature coverage (runtime pointer)

**Purpose.** A compact map of which `llm-d-benchmark` features this agent covers, so the
agent can answer "can you do X?" honestly and point at the canonical catalog. Read on demand.

## Legend
- ✅ covered (agent drives or reimplements it) · 🟡 partial · ⬜ missing (documented upstream, not surfaced)
- Optional? Y/N · Default on/off/n-a

## Coverage summary (68 features audited: 60 ✅ / 2 🟡 / 6 ⬜)

| Area | Description | ✅ | 🟡 | ⬜ |
|---|---|---:|---:|---:|
| A | Lifecycle subcommands, CLI flags & deploy options | 31 | 1 | 2 |
| B | Configuration system | 5 | 0 | 0 |
| C | Workloads, harnesses & run/harness contract | 3 | 0 | 1 |
| D | Design of Experiments | 2 | 0 | 0 |
| E | Results parsing, analysis, comparison & history | 8 | 0 | 0 |
| F | Observability | 4 | 1 | 0 |
| H | Utilities & CLI internals | 5 | 0 | 1 |
| I | Project meta | 2 | 0 | 2 |
| **Total** | | **60** | **2** | **6** |

**Headline gap — CLOSED (Phase 27, 2026-06-03).** Benchmark metrics collection
(`--monitoring`) is now ✅: the consumer shipped in Phase 25 (`app/validation/report.py` +
`knowledge/standard_metrics.yaml` parse `results.observability`) and the producer is activated —
`build_argv` emits `--monitoring`/`--no-monitoring` subcommand-aware via `ExecuteInput.flags["monitoring"]`,
with a read-only `_probe_prometheus_crds` CRD check feeding the knowledge-driven opt-out. The 3 standard
`results.observability` metrics are wired into the trend store (Phase 49). Roadmap v4 Phases 27-66 are
merged DONE (57 & 58 deferred).

## Default-on (with a knowledge-driven opt-out)
- **`--monitoring`** — lights up `results.observability` (KV-cache hit rate, schedule delay,
  GPU util) the report parses. Default ON; emit `--no-monitoring` on Kind / clusters lacking
  Prometheus-operator CRDs. Decision lives in `knowledge/observability.md`.

## Optional to surface to users (offer when their context calls for it)
- Model override `-m/--models` · cluster access `-k`/URL/token · HuggingFace gated-model
  secret · gateway class `--gateway-class` · multi-stack `--stack`/`--parallel` · WVA
  `-u/--wva` (Workload Variant Autoscaler — for multi-variant deployments across heterogeneous
  GPU types; HPA+WVA path, not OpenShift-only, 🟡) · cloud results sink `-r gs://`/`s3://` ·
  local `--analyze` plot families · kustomize `kustomize.*` config block · distributed tracing
  `tracing:` block.

## Harnesses (Area C)
Six workload harnesses ship on disk under `workload/profiles/`: **inference-perf** (the
default, SLO/latency), **guidellm** (throughput sweeps), **vllm-benchmark** (dataset replay /
max-concurrency), **aiperf**, **inferencemax**, **nop**. The agent primarily surfaces
inference-perf + guidellm + vllm-benchmark (and nop for plumbing); aiperf/inferencemax are
present but rarely surfaced. For the live, harness-scoped workload set, see
`usecase_to_profile.yaml` / `list_catalog` — that is the source of truth for what's runnable.

Full catalog: docs/BENCHMARK_FEATURE_COVERAGE.md
