# Benchmark feature coverage (runtime pointer)

**Purpose.** A compact map of which `llm-d-benchmark` features this agent covers, so the
agent can answer "can you do X?" honestly and point at the canonical catalog. Read on demand.

**This is a MAP, not the mechanism.** For per-harness *capability* answers (multi-turn,
think-time, conversation recycling, user-centric / steady-state modes)
`read_knowledge('multi_harness')`; for sweep mechanics (TP/DoE factors and their canonical
keys) `read_knowledge('sweep_authoring')`. A ✅/🟡 here means "covered" — never extrapolate a
blank cell or a 🟡 into "unsupported"; open the per-harness guide before answering.

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

## Default-on (with a knowledge-driven opt-out)
- **`--monitoring`** — lights up `results.observability` (KV-cache hit rate, schedule delay,
  GPU util) the report parses. Default ON is the AGENT's policy (upstream `run` defaults it OFF,
  and only standup accepts `--no-monitoring` — `build_argv` omits the flag elsewhere); opt out on
  Kind / clusters lacking Prometheus-operator CRDs. Decision lives in `knowledge/observability_monitoring.md`.

## Optional to surface to users (offer when their context calls for it)
- Model override (`-m/--model`, singular, on `run`; `-m/--models` on plan/standup/teardown/experiment) · cluster access `-k`/URL/token · HuggingFace gated-model
  secret · gateway class `--gateway-class` · multi-stack `--stack`/`--parallel` · WVA
  `-u/--wva` (Workload Variant Autoscaler — for multi-variant deployments across heterogeneous
  GPU types; HPA+WVA path, not OpenShift-only, 🟡) · cloud results sink `-r gs://`/`s3://` ·
  local `--analyze` plot families · kustomize `kustomize.*` config block · distributed tracing
  `tracing:` block.

## Harnesses (Area C)
Seven workload harnesses ship on disk under `workload/profiles/`: **inference-perf** (the
default, SLO/latency), **guidellm** (throughput sweeps), **vllm-benchmark** (dataset replay /
max-concurrency), **aiperf**, **inferencemax**, **eval-containers**, **nop**. The agent primarily
surfaces inference-perf + guidellm + vllm-benchmark (and nop for plumbing);
aiperf/inferencemax/eval-containers are present but less commonly surfaced — they still have
real, documented knobs (e.g. aiperf's `turn_delay` think-time, conversation recycling, and a
user-centric steady-state mode), so `read_knowledge('multi_harness')` for the per-harness
capabilities before telling a user a harness can't do something. For the live, harness-scoped workload set, see
`usecase_to_profile.yaml` / `list_catalog` — that is the source of truth for what's runnable.

Full catalog: docs/reference/BENCHMARK_FEATURE_COVERAGE.md
