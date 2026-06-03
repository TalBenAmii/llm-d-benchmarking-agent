# Useful upstream docs (llm-d & llm-d-benchmark) — runtime pointer

Which upstream `.md` to open for a given task. Curated from every doc in both repos
(`llm-d` = the inference stack + deploy guides; `llm-d-benchmark` = the `llmdbenchmark`
CLI + Benchmark Report schema). Tiered by how directly each helps THIS agent deploy
stacks, drive the benchmark lifecycle, and parse/explain results.

**Legend:** ⭐⭐⭐ must-read · ⭐⭐ feature-specific reference · ⭐ background · — skip (governance/CI/stub).
Counts: 52 ⭐⭐⭐ · 56 ⭐⭐ · 63 ⭐ · 24 — across 195 files (58 llm-d-benchmark, 137 llm-d); the appendix also lists one byte-identical `util/` duplicate so nothing is silently dropped.
**Full annotated index (tables, reference points, external links, skip appendix):** `docs/USEFUL_REPO_DOCS.md`.

## Start here (the must-reads)

1. `llm-d-benchmark/README.md` — every `llmdbenchmark` subcommand/flag/`LLMDBENCH_*` var + the spec/scenario/harness/profile vocabulary.
2. `llm-d-benchmark/llmdbenchmark/interface/README.md` — per-subcommand flag + env-var enumeration that backs our allowlist + `build_argv`.
3. `llm-d-benchmark/llmdbenchmark/README.md` — package map of the CLI: six subcommands + the standup→smoketest→run→teardown lifecycle.
4. `llm-d-benchmark/docs/developer-guide.md` — best single map of how scenarios/experiments/harnesses/profiles/lifecycle fit together.
5. `llm-d-benchmark/config/README.md` — config override chain + every scenario knob (model/replicas/namespace/monitoring/vLLM).
6. `llm-d-benchmark/docs/standup.md` — scenario-parameter vocabulary to set/validate when deploying (model, TP, max-model-len, accelerator, gateway class).
7. `llm-d-benchmark/docs/run.md` — use case → harness + workload profile, and the resulting metrics (TTFT/TPOT/ITL/throughput).
8. `llm-d-benchmark/docs/quickstart.md` — the exact `cicd/kind` CPU-only path for non-experts, with preconditions + failure modes.
9. `llm-d-benchmark/docs/doe.md` — the `experiment` (DoE) file format (factors/levels/treatments) we generate for sweeps.
10. `llm-d-benchmark/docs/metrics_collection.md` — every `results.observability` metric + the flags/env vars that turn collection on.
11. `llm-d-benchmark/llmdbenchmark/analysis/benchmark_report/README.md` — THE Benchmark Report v0.2 schema we parse (every field + converter CLI).
12. `llm-d-benchmark/skills/convert-guide/references/mappings.md` — the definitive Helm-value → `LLMDBENCH_*` lookup for translating a guide into a scenario.
13. `llm-d/docs/well-lit-paths/README.md` — catalog of every deploy path we might stand up (optimized-baseline vs pd-disaggregation vs autoscaling).
14. `llm-d/guides/optimized-baseline/README.md` — THE primary guide we deploy: env vars, helm/kubectl, monitoring toggle, `run_only.sh` flow.
15. `llm-d/helpers/benchmark.md` — the `run_only.sh` `config.yaml` schema (endpoint/harness/workload) + the standardized report we mirror.
16. `llm-d/docs/resources/observability/metrics.md` — the exact vLLM/EPP metric names we read/explain + how to enable monitoring.
17. `llm-d/docs/readiness-probes.md` — deploy-and-wait logic: poll `/v1/models` (not `/health`) to know a server is truly Ready.

## Other high-value docs (⭐⭐⭐) by topic

**llm-d-benchmark — lifecycle/CLI:** `docs/lifecycle.md` (phase ordering, auth/HF token, `-s` step filtering) · `llmdbenchmark/standup/README.md` · `llmdbenchmark/run/README.md` (run-only `-U`, result paths) · `llmdbenchmark/teardown/README.md` (`--deep`) · `llmdbenchmark/smoketests/README.md` (health/inference/config checks).
**llm-d-benchmark — workloads/harnesses:** `workload/README.md` (run 11-step pipeline, profiles, DoE) · `docs/tutorials/run/run_against_existing_example.md` (run-only happy path) · `docs/tutorials/run/run_interactively_example.md` (guidellm/interactive) · `docs/tutorials/kubecon/README.md` (worked e2e) · `docs/kustomize.md` (`-t kustomize` to deploy guides; `-m`/model.name + DoE setup sweeps don't apply) · `skills/convert-guide/references/harnesses.md` (valid harness+profile menu).
**llm-d-benchmark — analysis/DOE/report:** `docs/analysis.md` (artifacts + `--analyze`) · `docs/benchmark_report.md` (report contract) · `llmdbenchmark/analysis/README.md` (artifact file list) · `llmdbenchmark/experiment/README.md` (experiment file format).
**llm-d-benchmark — observability:** `docs/observability.md` (`--monitoring`/`--no-monitoring`, metric catalog).
**llm-d-benchmark — resources:** `docs/resource_requirements.md` (`LLMDBENCH_HARNESS_CPU_NR`, default 16; lower for kind).
**llm-d-benchmark — config/convert-guide:** `skills/convert-guide/SKILL.md` (guide→config playbook) · `skills/convert-guide/references/patterns.md` (correctness rules: keep env vars, MULTINODE for LWS, pd-config for P/D) · `skills/convert-guide/references/templates.md` (output shape).
**llm-d — deploy guides:** `README.md` (entry map) · `guides/README.md` · `docs/getting-started/quickstart.md` · `docs/well-lit-paths/optimized-baseline.md` · `guides/pd-disaggregation/README.md` · `guides/precise-prefix-cache-routing/README.md` · `guides/predicted-latency-routing/README.md` (SLO headers) · `guides/tiered-prefix-cache/cpu/README.md` · `guides/wide-ep-lws/README.md` (carries v0.2 report schema).
**llm-d — helpers/preconditions:** `helpers/smoke-test/README.md` (healthcheck.sh) · `helpers/hf-token.md` (`llm-d-hf-token` secret) · `helpers/client-setup/README.md` (tool + min-version checklist) · `docs/infrastructure.md` (K8s ≥1.29; host/accel sizing).
**llm-d — observability resources:** `docs/resources/observability/promql.md` (PromQL idioms) · `docs/resources/observability/setup.md` (install Prometheus/Grafana/OTel).

## Reference points & everything else

For the distilled **API/CLI/CRD/flag/report-field reference points** (benchmark CLI &
lifecycle flags, workload & scenario keys, Benchmark Report v0.2 fields, llm-d CRDs & EPP
config, observability knobs), the **medium/low-tier** per-feature docs (CRD reference,
well-lit-path internals, infra providers, CLI module internals), the **external references**,
and the **skipped** files — see the full index: `docs/USEFUL_REPO_DOCS.md`.
