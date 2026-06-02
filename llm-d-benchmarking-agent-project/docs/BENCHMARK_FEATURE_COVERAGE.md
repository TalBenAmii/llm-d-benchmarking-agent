# Benchmark Feature-Coverage Catalog

> **What this is.** A grounded cross-reference of every feature documented in the
> [`llm-d-benchmark`](https://github.com/llm-d/llm-d-benchmark) repo against THIS agent's
> coverage (the 22 tools in `app/tools/registry.py`, `security/allowlist.yaml`, the
> `knowledge/` brain, and `app/validation/`). Each row is verified against source: every ✅
> cites the tool + file that backs it; every ⬜ cites the benchmark doc that documents the
> missing capability.
>
> **Generated** by the `benchmark-catalog-gap` workflow (extract → cross-reference →
> 3-lens adversarial skeptic → synthesize). Re-runnable when the benchmark docs change.
> Canonical source of the gap roadmap: [`ROADMAP_V4.md`](../ROADMAP_V4.md) — each ⬜/🟡 row
> below maps 1:1 to a v4 phase (27-58).

## Legend

| Symbol | Meaning |
|---|---|
| ✅ | **covered** — the agent drives or reimplements this capability (tool + file cited) |
| 🟡 | **partial** — partially reachable (e.g. via generic passthrough) or the consumer ships but the producer is not activated |
| ⬜ | **missing** — documented by the benchmark, not surfaced by the agent |

- **Optional?** `Y` = optional knob a user may want; `N` = core / always-on capability.
- **Default** `on` / `off` / `n/a` — the benchmark's documented default state for the knob.

## Coverage summary

67 documented features audited: **35 ✅ / 16 🟡 / 16 ⬜**.

| Area | Description | ✅ | 🟡 | ⬜ |
|---|---|---:|---:|---:|
| A | Lifecycle subcommands, CLI flags & deploy options | 17 | 11 | 6 |
| B | Configuration system (override chain, schema, vLLM knobs) | 5 | 1 | 0 |
| C | Workloads, harnesses & the run/harness contract | 2 | 0 | 2 |
| D | Design of Experiments (factors/levels/treatments lifecycle) | 1 | 0 | 0 |
| E | Results parsing, analysis, comparison & history | 3 | 2 | 2 |
| F | Observability (metrics collection, tracing, dashboards, logging) | 2 | 1 | 2 |
| H | Utilities & CLI internals (capacity, cluster, pod lifecycle, workspace) | 4 | 1 | 1 |
| I | Project meta (governance, discovery tool, placeholder docs) | 1 | 0 | 3 |
| **Total** | | **35** | **16** | **16** |

> **Headline gap (priority).** Area F's *Benchmark metrics collection (`--monitoring`)* is
> **🟡 partial**, not covered: the *consumer* ships (Phase 25 parses `results.observability`
> via `knowledge/standard_metrics.yaml` + `app/validation/report.py`) but the *producer*
> (`--monitoring` / `metricsScrapeEnabled`) is never activated, so the observability metrics
> are perpetually empty in practice. Closing it is **ROADMAP_V4 Phase 27** and the first
> entry under *Recommended default-on features* below.

---

## Area A — Lifecycle subcommands, CLI flags & deploy options

| Feature | Source doc | Options / knobs | Optional? | Default | Coverage | Evidence / notes |
|---|---|---|---|---|---|---|
| Six lifecycle subcommands (plan/standup/smoketest/run/teardown/experiment) | llmdbenchmark/README.md#cli-commands; config/README.md#usage | plan, standup, smoketest, run, teardown, experiment, results | N | n/a | ✅ | `execute_llmdbenchmark` → `app/tools/execute.py` (`_SUBCOMMANDS`); `app/tools/schemas.py:ExecuteInput.subcommand`; allowlisted in `security/allowlist.yaml`. |
| Standup deploy method (-t modelservice/standalone/kustomize/fma) | docs/standup.md#methods; config/README.md#scenario-organization | -t/--methods enum | N | n/a | ✅ | `build_argv` emits `-t`; `security/allowlist.yaml` methods enum `[standalone, modelservice, kustomize, fma]`. |
| Scenario / specification selection (--spec) | docs/standup.md#use; config/README.md#specifications | --spec, -c/--scenario (bare/category/full-path) | N | n/a | ✅ | `execute.py` emits `--spec`; `list_catalog` (`app/tools/probe.py`) grounds names; `SessionPlan` validates against live catalog (`app/validation/session_plan.py`). |
| Model list selection (-m/--models) | docs/standup.md#use; config/README.md#config-variable-substitution | -m/--models, model.name, LLMDBENCH_DEPLOY_MODEL_LIST | N | n/a | 🟡 | Reasoned about + fed to capacity pre-flight (`app/tools/capacity.py`, `CheckCapacityInput`), but `build_argv` emits no `-m`; model rides on the chosen spec. Gap: docs/standup.md#use. Fix → ROADMAP_V4 Phase 28. |
| Namespace selection (-p/--namespace) | docs/lifecycle.md#standup; docs/run.md#use | -p/--namespace | N | n/a | ✅ | `build_argv` emits `-p`; `SessionPlan.namespace`; `probe_environment` checks a namespace for an existing stack (`app/tools/probe.py`). |
| Cluster access / auth (kubeconfig, URL, token) | docs/lifecycle.md; docs/standup.md#use | -k/--kubeconfig, cluster.url, cluster.token, LLMDBENCH_CLUSTER_URL=auto | Y | n/a | 🟡 | `probe_environment` checks reachability (`app/tools/probe.py`); the agent uses the ambient kube context. No `-k`/URL/token override emitted. Gap: docs/standup.md#use. Fix → ROADMAP_V4 Phase 29. |
| HuggingFace token / gated-model auth | docs/standup.md#use; config/README.md#huggingface-configuration | LLMDBENCH_HF_TOKEN, huggingface.token, huggingface.enabled | Y | off | 🟡 | HF token used for the capacity lookup + kept backend-only/scrubbed (`app/config.py:child_env`), but the agent does not provision the cluster HF secret for gated standups. Gap: docs/standup.md#use. Fix → ROADMAP_V4 Phase 30. |
| Standup/run step list + re-run steps (-s/--step) | docs/lifecycle.md#list-of-standup-steps; llmdbenchmark/run/README.md#run-specific-steps-only | -h, -s/--step, step ranges (3-5, 5,7) | Y | off | 🟡 | Reachable only via the generic `extra` passthrough (`build_argv` appends `extra`); no modeled `--step` key. Gap: llmdbenchmark/run/README.md. Fix → ROADMAP_V4 Phase 31. |
| Dry-run mode (plan / --dry-run / --list-endpoints) | docs/lifecycle.md#to-dry-run; llmdbenchmark/standup/README.md#dry-run-behavior | -n, --dry-run, plan, --list-endpoints, --generate-config | Y | off | ✅ | `build_argv` emits `--dry-run`/`--list-endpoints`; `--dry-run` is `read_only_trigger:true` in `security/allowlist.yaml` so previews auto-run (the determinism gate). |
| Capacity Planner validation gating | docs/standup.md#use; llmdbenchmark/utilities/README.md#capacity_validator-py | LLMDBENCH_IGNORE_FAILED_VALIDATION, run_capacity_planner, ValidationParams | Y | on | ✅ | `check_capacity` (`app/tools/capacity.py`) runs the repo's own `run_capacity_planner` over the rendered config; `knowledge/capacity.md`; auto-runs as project-script (Phase 6). |
| Gateway class / provider (istio/agentgateway/gke/epponly) | docs/standup.md#gateway-provider | gateway.className, --gateway-class | Y | on | ⬜ | docs/standup.md#gateway-provider: no `--gateway-class` flag; provider rides entirely on the chosen scenario; no knowledge guidance to choose one. Fix → ROADMAP_V4 Phase 32. |
| Multi-stack scenarios + --stack subset + --parallel | docs/standup.md#multi-stack-scenarios; docs/run.md#targeting-a-single-pool-stack | scenario:, shared:, httpRoute.mode: shared, --stack, --parallel | Y | off | ⬜ | docs/standup.md#multi-stack-scenarios: a multi-stack spec would run, but `build_argv` emits no `--stack`/`--parallel`; no per-pool guidance. Fix → ROADMAP_V4 Phase 33. |
| Workload Variant Autoscaler (WVA) (-u/--wva, wva.* knobs) | docs/workload-variant-autoscaler.md; llmdbenchmark/interface/README.md | -u/--wva, LLMDBENCH_WVA, wva.controller/variantAutoscaling/hpa.*, WVA guides | Y | off | 🟡 | `knowledge/welllit_path_advisor.yaml` references WVA guides as advisory; a WVA spec would render its `wva:` block. No `-u/--wva` flag, not allowlisted; HPA/VA knobs + 8 smoketests + OpenShift gate unsurfaced. Gap: docs/workload-variant-autoscaler.md. Fix → ROADMAP_V4 Phase 34. |
| Standup monitoring flag (PodMonitor/ServiceMonitor + EPP verbosity) | llmdbenchmark/standup/README.md#--monitoring-flag; docs/observability.md#cli-monitoring-flags | --monitoring, --no-monitoring, monitoring.podmonitor.enabled, monitoring.installPrometheusCrds | Y | on | 🟡 | Consumer ships (`app/validation/report.py` + `knowledge/standard_metrics.yaml`), but `build_argv` emits no `--monitoring`; `ExecuteInput.flags` has no key; not allowlisted under standup. Part of the headline gap. Fix → ROADMAP_V4 Phase 35 (activation rides on Phase 27). |
| Skip auto post-standup smoketests (--skip-smoketest) + smoketest steps | llmdbenchmark/standup/README.md#post-standup-smoketests; llmdbenchmark/smoketests/README.md#steps | --skip-smoketest, smoketest (subcommand), health_check, inference_test | Y | on | ✅ | `build_argv` emits `--skip-smoketest`; smoketest is a gated first-class subcommand; the MVP runs smoketest between standup and run. |
| Run harness selection (-l/--harness) | docs/run.md#harnesses; workload/README.md#available-harnesses | -l/--harness: inference-perf, guidellm, vllm-benchmark, inferencemax, nop | Y | on | ✅ | `build_argv` emits `-l`; `list_catalog` discovers harnesses from the repo (no hardcoded whitelist); `knowledge/usecase_to_profile.yaml`, `knowledge/multi_harness.md`. |
| Run workload profile (-w/--workload) + .yaml.in profile system | docs/run.md#profiles; workload/README.md#what-is-a-profile | -w/--workload, profiles/{harness}/*.yaml.in, REPLACE_ENV_* substitution | Y | on | ✅ | `build_argv` emits `-w`; `list_catalog` returns `workloads_by_harness`; `knowledge/usecase_to_profile.yaml`. Profile rendering is the CLI's job (read at runtime). |
| Workload profile overrides (-o/--overrides) | docs/run.md#profiles; workload/README.md#single-override---overrides | -o/--overrides, comma-separated key=value, dotted.key.path=value | Y | off | ✅ | `build_argv` emits `-o`; allowlisted on run/experiment; `generate_doe_experiment` authors override-key sweeps (`app/tools/doe.py`). |
| Run-only mode against an endpoint (-U/--endpoint-url) | docs/run.md#use; workload/README.md#run-only-mode | -U/--endpoint-url (skips auto-detect + model verify) | Y | off | ✅ | `build_argv` emits `-U`; `check_endpoint_readiness` corroborates via `run --list-endpoints` (`app/tools/readiness.py`); `OrchestrateBenchmarkInput.require_ready_endpoint`. |
| Skip execution / collect existing results (-z/--skip) | llmdbenchmark/run/README.md#cli-flags; workload/README.md#skip-mode-result-collection | -z/--skip (collect/analyze only) | Y | off | 🟡 | Reachable via the raw `extra` passthrough; report re-parsing covered independently (`locate_and_parse_report`). No first-class `skip` key. Gap: llmdbenchmark/run/README.md. Fix → ROADMAP_V4 Phase 36. |
| Harness debug mode (-d/--debug, sleep infinity) | llmdbenchmark/run/README.md#debug-mode; workload/README.md#debug-mode | -d/--debug, sleep infinity harness pod, interactive llm-d-benchmark.sh | Y | off | ⬜ | llmdbenchmark/run/README.md#debug-mode: interactive in-pod exec is outside the agent's automated, approval-gated flow. Fix → ROADMAP_V4 Phase 37. |
| Parallel harness pods / load parallelism (-j/--parallelism) | docs/run.md#use; docs/doe.md#parallelism-levels | -j/--parallelism, LLMDBENCH_HARNESS_LOAD_PARALLELISM, per-pod results subdir | Y | on | ✅ | `build_argv` emits `-j`; allowlisted; the orchestrator separately caps concurrent sweep Jobs (`app/orchestrator/controller.py`). |
| Harness wait / data-access / deploy timeouts | docs/run.md#use; llmdbenchmark/run/README.md#cli-flags | -s/--wait, --wait-timeout, --data-access-timeout, --*-deploy-timeout, --pvc-bind-timeout | Y | on | 🟡 | Governed at the runner/orchestrator layer (`security/allowlist.yaml` `timeout_s`; `OrchestrateBenchmarkInput.active_deadline_seconds`). The CLI's per-phase timeout flags are not modeled. Gap: llmdbenchmark/run/README.md. Fix → ROADMAP_V4 Phase 38. |
| Run results destination / cloud upload (-r/--output local/gs/s3) | llmdbenchmark/run/README.md#upload-results-to-cloud-storage | -r/--output, local, gs://bucket, s3://bucket | Y | on | 🟡 | `build_argv` emits `-r` (local, anchored to the session workspace); gs://, s3:// deliberately NOT allowlisted for the MVP. Gap: security/allowlist.yaml. Fix → ROADMAP_V4 Phase 39. |
| Local analysis after collection (--analyze, matplotlib plots) | docs/analysis.md#local-analysis---analyze; docs/analysis.md#5-prometheus-metric-visualization | --analyze, LLMDBENCH_RUN_EXPERIMENT_ANALYZE_LOCALLY, per-request/session/metric PNGs | Y | off | 🟡 | The agent surfaces the harness's own PNGs (`locate_and_parse_report` charts) + does its own SLO/goodput/Pareto math (`app/tools/analyze.py`), but does not pass `--analyze` for the extra matplotlib plot families. Gap: llmdbenchmark/run/README.md. Fix → ROADMAP_V4 Phase 40. |
| Dataset replay URL (-x/--dataset) | llmdbenchmark/run/README.md#cli-flags; workload/README.md#run-subcommand | -x/--dataset, REPLACE_ENV_LLMDBENCH_RUN_DATASET_DIR | Y | off | ⬜ | llmdbenchmark/run/README.md#cli-flags: no dataset flag; the supported path uses synthetic workload profiles. Fix → ROADMAP_V4 Phase 41. |
| Generate / reuse a run config YAML (--generate-config / -c) | llmdbenchmark/run/README.md#generate-a-run-config-for-reuse | --generate-config, -c/--config | Y | off | 🟡 | `write_and_validate_config` authors+validates configs in-workspace (`app/tools/config_artifact.py`), but the CLI's own `--generate-config`/`-c` reuse flags are not modeled. Gap: llmdbenchmark/run/README.md. Fix → ROADMAP_V4 Phase 42. |
| experiment subcommand (full DoE standup+run+teardown) | docs/doe.md#use; workload/README.md#experiment-subcommand | experiment, -e/--experiments, --stop-on-error, --skip-teardown, setup×run matrix | Y | off | ✅ | `build_argv` emits `-e`/`--stop-on-error`/`--skip-teardown` + anchors `--workspace`; `generate_doe_experiment` authors the YAML; allowlisted (timeout_s 14400). |
| Teardown (deep-clean -d/--deep + step pipeline) | llmdbenchmark/teardown/README.md#step-03-delete-resources | teardown, -d/--deep, cluster-role cleanup gating | Y | off | ✅ | `subcommand=teardown` (approval-gated, mutating); the MVP ends with "offer teardown"; the `--deep` variant reachable via extra-args (relaxed flag policy `security/allowlist.yaml`). |
| Phase orchestration internals (step partitions, executor, wait helpers) | llmdbenchmark/executor/README.md#architecture; #wait-helpers | pre-global/per-stack/post-global, max_parallel_stacks, wait_for_pods/job/pvc | N | n/a | ✅ | CLI internals driven via `execute_llmdbenchmark`; the agent layers its own K8s-native Job lifecycle/fault classification/readiness waits (`app/orchestrator/controller.py`, `faults.py`, `readiness.py`). |
| Kind local quickstart lifecycle (CPU-only sim) | docs/quickstart.md#what-you-will-build; config/README.md#scenarioscicd | --spec cicd/kind, llm-d-inference-sim, kind create cluster | Y | n/a | ✅ | The MVP vertical; `run_command` creates/deletes the kind cluster (`app/tools/command.py`); `knowledge/quickstart_playbook.md`; `tests/integration` + `knowledge/sim_integration.md`. |
| install.sh bootstrap + tool / Python prerequisites | README.md#install; llmdbenchmark/executor/README.md#dependency-checker-depspy | install.sh, --uv/--no-uv, REQUIRED_TOOLS (kubectl/helm/helmfile/jq/yq), Python>=3.11 | N | n/a | ✅ | `run_setup` (`app/tools/repos.py`) runs `install.sh`; `ensure_repos` clones the repos; allowlisted (install.sh, install_prereqs.sh); `knowledge/preconditions.md`; `/readyz`. |
| Administrative privilege / --non-admin skip | README.md#administrative-requirements; llmdbenchmark/executor/README.md#executioncontext | --non-admin, -i, context.non_admin | N | off | ⬜ | README.md#administrative-requirements: no `--non-admin` flag; the local kind path runs cluster-admin so the skip is not needed. Fix → ROADMAP_V4 Phase 43. |
| Telemetry push (queue-based async usage reporting) | llmdbenchmark/README.md#telemetry; llmdbenchmark/telemetry/README.md | --telemetry-enabled, LLMDBENCH_TELEMETRY_*, --telemetry-provider=http | Y | off | ⬜ | llmdbenchmark/telemetry/README.md: never enabled/surfaced; the agent ships its own Prometheus `/metrics` for operability instead. Fix → ROADMAP_V4 Phase 44. |

## Area B — Configuration system

| Feature | Source doc | Options / knobs | Optional? | Default | Coverage | Evidence / notes |
|---|---|---|---|---|---|---|
| Config override chain (defaults < scenario < env < CLI < treatments) | config/README.md#config-override-chain | scenario YAML, LLMDBENCH_* env, CLI flags, experiment treatments | N | n/a | ✅ | The CLI owns the merge; the agent feeds each tier — spec via `--spec`, per-call flags via `build_argv`, treatments via `generate_doe_experiment` — and validates via plan/--dry-run. |
| Spec auto-discovery & catalog grounding (--spec name forms, base_dir) | config/README.md#specification-auto-discovery; #the-base_dir-variable | --spec bare/category/full-path, --bd base_dir, guide/example/cicd specs | Y | n/a | ✅ | `list_catalog` enumerates real specs/harnesses/workloads (`app/tools/probe.py`); `SessionPlan` enums checked against the live catalog; `tests/flows/catalog_snapshot.py` guards drift; "never invent a name". |
| vLLM tuning knobs (command gen, flags, ports, KV-transfer, accelerator, affinity, storage, scheduling) | config/README.md#vllm-command-generation; #kv-transfer-configuration; #accelerator-resources; #affinity-configuration | customCommand, vllmCommon.flags.*, servicePort/port, kvTransfer.*, accelerator/parallelism.tensor, affinity.*, priorityClassName, schedulerName, ephemeralStorage, networkResource | Y | n/a | 🟡 | Parallelism/memory/context knobs reflected in capacity pre-flight (`CheckCapacityInput`) + sweepable via DoE (any dotted key); finer vLLM/scheduling/storage knobs are inherited from the spec, not agent-authored. Gap: config/README.md#vllm-command-generation. Fix → ROADMAP_V4 Phase 45. |
| Config schema validation (Pydantic) + render + resolvers + Jinja | llmdbenchmark/parser/README.md#3-config-schema-validation; #2-plan-rendering-renderplans; #resolver-chain | validate_config(), RenderPlans, deep_merge, 13 resolvers, Jinja templates+filters | N | n/a | ✅ | CLI internals the agent invokes via plan/--dry-run for the determinism gate (`app/tools/execute.py`); never vendored — reads repo truth at runtime. |
| Pinned versions / SBOM / chartVersions / image versions | docs/upstream-versions.md; config/README.md#chart-versions; #container-images | chartVersions.*, images.*, auto version resolution, generate_sbom.py, pinned tools | N | on | ✅ | Repo-internal, resolved live by the CLI plan; the agent never vendors copies (read repo truth at runtime). |

## Area C — Workloads, harnesses & the run/harness contract

| Feature | Source doc | Options / knobs | Optional? | Default | Coverage | Evidence / notes |
|---|---|---|---|---|---|---|
| Harness script contract + ConfigMap mounting + 11-step run lifecycle | workload/README.md#harness-script-contract; #step-by-step-execution-flow | fixed env-var interface, llmdbench-harness-scripts ConfigMap, run steps 00-10 | N | n/a | ✅ | CLI/harness internals invoked by `execute_llmdbenchmark run`; the agent never edits harness scripts (repos read-only); the orchestrator can alternatively run the benchmark as a K8s Job (`app/orchestrator/job.py`). |
| Multi-harness in one session (cross-harness comparison) | workload/README.md#available-harnesses | inference-perf vs guidellm vs vllm-benchmark, same stack | Y | off | ✅ | `compare_harness_runs` (`app/tools/multiharness.py`) contrasts reports from different harnesses (harness read from the report, never guessed); `knowledge/multi_harness.md` (Phase 10). |
| Multi-turn trace replay benchmark (experimental) | experimental/multi-turn/README.md#overview | production-trace-replay-qwen.py, --trace-file (JSONL), TTFT-by-turn-buckets | Y | off | ⬜ | experimental/multi-turn/README.md: an experimental standalone script outside the supported lifecycle; not surfaced. Fix → ROADMAP_V4 Phase 52. |
| convert-guide skill (guide → scenario/experiment file gen) | skills/convert-guide/SKILL.md#purpose | /convert-guide <url-or-path>, ai.<name>.sh/.yaml, LLMDBENCH_* mappings | Y | off | ⬜ | skills/convert-guide/SKILL.md: writes into the (read-only) benchmark repo; the agent grounds on the existing catalog instead. Fix → ROADMAP_V4 Phase 53. |

## Area D — Design of Experiments

| Feature | Source doc | Options / knobs | Optional? | Default | Coverage | Evidence / notes |
|---|---|---|---|---|---|---|
| DoE — factors / levels / treatments, setup×run matrix, experiment YAML | docs/doe.md#concept; workload/README.md#design-of-experiments-doe-concepts; llmdbenchmark/experiment/README.md#setup-treatments | factors, levels, treatments, setup.treatments, full_factorial, dotted-key overrides, NA placeholder, constants | Y | off | ✅ | `generate_doe_experiment` (`app/tools/doe.py`) cross-products factors×levels into the deduped treatments matrix, emits valid experiment YAML, and validates structurally against the repo's example experiments; `knowledge/sweep_playbook.md` (Phase 19). |
| Nested setup×run treatment execution lifecycle (N×M result sets) | docs/doe.md#treatment-execution-lifecycle; #setup-treatment-cycling | per setup: standup → all run treatments → teardown, sequential | N | n/a | ✅ | `execute_llmdbenchmark subcommand=experiment` runs the CLI's setup×run lifecycle + anchors per-treatment reports under `--workspace`; `compare_reports` discovers every treatment's report; the orchestrator adds checkpoint/resume. |

## Area E — Results parsing, analysis, comparison & history

| Feature | Source doc | Options / knobs | Optional? | Default | Coverage | Evidence / notes |
|---|---|---|---|---|---|---|
| Benchmark Report v0.2 schema parsing + conversion pipeline | llmdbenchmark/analysis/benchmark_report/README.md#v02-format-description; docs/benchmark_report.md | version=0.2, run/scenario/results, request_performance, v0.1+v0.2 conversion, summary.txt | N | on | ✅ | `locate_and_parse_report` validates against the repo BR-v0.2 JSON schema + returns a plain-language summary (`app/validation/report.py:summarize_report`); read from the schema, never scraped. |
| request_performance metrics (TTFT/TPOT/ITL/E2E, throughput, percentiles) | llmdbenchmark/analysis/benchmark_report/README.md#resultsrequest_performance | latencies, throughput (token/request rates), counts, p50/p90/p95/p99/p99p9 | N | on | ✅ | `report.py` extracts the full percentile ladder (p0p1..p99p9) from `request_performance.aggregate`; `analysis.py:evaluate_slo`; report-card surfaces formatted ms/tok-s + p99 table. |
| session_performance metrics (multi-turn sessions) | llmdbenchmark/analysis/benchmark_report/README.md#resultssession_performance | results.session_performance, session_rate/duration, events/tokens per session, multi_turn.enabled | Y | off | ⬜ | benchmark_report/README.md#resultssession_performance: `report.py` reads session data only for multi-harness provenance, not the session_performance stats block. Fix → ROADMAP_V4 Phase 48. |
| Cross-treatment comparison + Pareto/latency-vs-throughput + goodput | docs/analysis.md#4-cross-treatment-comparison; llmdbenchmark/analysis/README.md#cross-treatment-comparison | treatment_comparison.csv, Pareto curves, best-treatment highlight, goodput | Y | off | ✅ | `compare_reports` (per-metric deltas + winner); `analyze_results` + `app/validation/analysis.py` (SLO filtering, honest goodput estimate, Pareto frontier, SLO-feasible frontier); `knowledge/analysis.md`. |
| Standard resource/serving metrics (KV-cache hit rate, schedule delay, GPU util) from results.observability | llmdbenchmark/analysis/benchmark_report/README.md#resultsobservability; docs/metrics_collection.md#report-integration | results.observability, kv_cache_hit_rate, schedule_delay, gpu_utilization, standardized + native field names | Y | off | 🟡 | Consumer fully built (Phase 25): `report.py:_extract_standard_metric` + `knowledge/standard_metrics.yaml`; informational Pareto objectives in `analyze.py`. Stays empty because the producer (`--monitoring`) is never activated. Gap: docs/metrics_collection.md. Fix → ROADMAP_V4 Phase 49 (rides on Phase 27). |
| Results Store (git-like result mgmt: init/remote/status/add/push/ls/pull) | llmdbenchmark/result_store/README.md | results init/remote/status/add/push/ls/pull, .result_store/ | Y | off | 🟡 | The need (cross-session tracking + trends) is met by the agent's own history store (`result_history` → `app/storage/history.py`, store/list/get/trend/delete; GET /api/history). The CLI's remote/push/pull taxonomy is not wired. Gap: llmdbenchmark/result_store/README.md. Fix → ROADMAP_V4 Phase 50. |
| Jupyter analysis notebook / standalone plotting scripts | docs/analysis.md#jupyter-notebook-analysis; docs/analysis/to_be_incorporated/README.md | analysis.ipynb, plot_itl_vs_qps.py, matplotlib (optional) | Y | off | ⬜ | docs/analysis.md#jupyter-notebook-analysis: interactive notebook / experimental scripts are manual exploratory tooling; the agent surfaces the harness's own PNGs instead. Fix → ROADMAP_V4 Phase 51. |

## Area F — Observability (PRIORITY)

| Feature | Source doc | Options / knobs | Optional? | Default | Coverage | Evidence / notes |
|---|---|---|---|---|---|---|
| **Benchmark metrics collection (--monitoring)** — vLLM/EPP Prometheus + DCGM GPU + cAdvisor + replica/startup/EPP-log snapshots, processed under `results.observability` | docs/metrics_collection.md#configuration; docs/observability.md#benchmark-built-in-metrics; #cli-monitoring-flags; docs/metrics_collection.md#report-integration | `--monitoring`, `--no-monitoring`, `scenario.metricsScrapeEnabled=true`, `LLMDBENCH_VLLM_COMMON_METRICS_SCRAPE_ENABLED`, `vllm:*` cache/queue/memory/nixl, `DCGM_FI_*`, `container_*` (cAdvisor), `inference_pool_*`/`inference_extension_*` (EPP), `replica_status.json`, `pod_startup_times.json`, `metrics_summary.json`, `results.observability`, PodMonitor/ServiceMonitor | Y | off | 🟡 | **HEADLINE GAP.** The **consumer** ships: `app/validation/report.py:_extract_standard_metric` (lines ~171-243) parses `results.observability`; `knowledge/standard_metrics.yaml` maps KV-cache/schedule-delay/GPU-util to standardized+native field names; `analyze.py` treats them as informational Pareto objectives (Phase 25). The **activation is missing**: `build_argv` (`app/tools/execute.py`) emits no `--monitoring`/`--no-monitoring`; `ExecuteInput.flags` (`app/tools/schemas.py`) has no monitoring key; `security/allowlist.yaml` does not permit it under standup/run/experiment — so `results.observability` is perpetually **empty in practice**. **Fix → ROADMAP_V4 Phase 27.** |
| Distributed tracing config (OpenTelemetry `tracing:` block) | docs/observability.md#distributed-tracing | scenario YAML `tracing:` block (endpoint, sampling rate, service names) | Y | off | ⬜ | docs/observability.md#distributed-tracing: the agent neither configures a `tracing:` block nor collects traces (the benchmark itself only configures, never collects). Fix → ROADMAP_V4 Phase 54. |
| Cluster Prometheus/Grafana dashboards (external monitoring stack) | docs/observability.md#prometheus-grafana-dashboards | upstream llm-d/docs/monitoring, PromQL queries, Grafana dashboards | Y | n/a | ✅ | The agent ships its own: `app/observability/metrics.py` (GET /metrics) + `deploy/observability/{grafana-dashboard.json,prometheus-scrape.yaml,alerts.rules.yaml}`; `observe_run_metrics` reads `kubectl top`; `knowledge/observability.md`. |
| Structured logging (core lifecycle logger) | llmdbenchmark/logging/README.md | get_logger, per-instance stdout/stderr files, verbose DEBUG, EmojiFormatter | N | on | ✅ | The CLI's logger runs in-subprocess (streamed to the UI); the agent layers structured JSON logging + correlation IDs (`app/observability/logging.py`, `logctx.py`; `knowledge/logging.md`); LLMDBENCH_LOG_LEVEL=DEBUG reachable via env. |
| Real-time metric streaming / custom Prometheus queries | docs/metrics_collection.md#not-yet-implemented | (explicitly unimplemented upstream) | Y | off | ⬜ | docs/metrics_collection.md#not-yet-implemented: unimplemented in the benchmark itself; the agent's live coverage is `kubectl top` (`observe_run_metrics`) + real-time pod log streaming. Fix → ROADMAP_V4 Phase 55. |

## Area H — Utilities & CLI internals

| Feature | Source doc | Options / knobs | Optional? | Default | Coverage | Evidence / notes |
|---|---|---|---|---|---|---|
| Reproducible work directory / artifact layout | docs/reproducibility.md#reproducibility; docs/run.md#use | LLMDBENCH_CONTROL_WORK_DIR, --workspace, analysis/setup/results/workload/environment layout | N | on | ✅ | `execute.py` anchors `--workspace` to the session dir for run + per-treatment reports; `workspace/` is gitignored per-session scratch; `knowledge/workspace_lifecycle.md`; `app/storage/retention.py` GC. |
| Kustomize deploy method config block (-t kustomize) | docs/kustomize.md#enabling; #config-reference-kustomize-block | -t kustomize, kustomize.guideName/repoPath/repoRef/patches/monitoring, --llmd-repo-path | Y | off | 🟡 | `-t kustomize` is allowlisted; `knowledge/deploy_path_playbook.md` notes guides deploy via helm+kustomize. The many `kustomize.*` knobs are not authored/surfaced. Gap: docs/kustomize.md. Fix → ROADMAP_V4 Phase 46. |
| Capacity validation internals (GPU mem & KV-cache planner) | llmdbenchmark/utilities/README.md#capacity_validator-py | run_capacity_planner, ValidationParams (replicas/gpu_memory/tp/pp/dp/max_model_len/hf_token), ignore_failures | N | on | ✅ | `check_capacity` (`app/tools/capacity.py`) invokes the repo's `run_capacity_planner` over the rendered config + returns its diagnostics; `CheckCapacityInput` maps the ValidationParams; `knowledge/capacity.md` (Phase 6). |
| Cluster connectivity / platform detection / endpoint discovery / model verification | llmdbenchmark/utilities/README.md#cluster-py; #endpoint-py | resolve_cluster, is_openshift/is_kind/is_minikube, find_*_endpoint, test_model_serving, /v1/models | N | on | ✅ | `probe_environment` detects runtime/kind/kube-context/reachability/namespaces/stack (`app/tools/probe.py`); `check_endpoint_readiness` reads `kubectl get endpoints` + corroborates with `run --list-endpoints` (`app/orchestrator/readiness.py`). |
| Pod lifecycle / crash-state detection / result collection / log capture | llmdbenchmark/utilities/README.md#kube_helpers-py | CRASH_STATES, wait_for_pods, collect_pod_results, capture_pod_logs, process_epp_logs.py | N | on | ✅ | The orchestrator classifies OOM/timeout/unschedulable/evicted/image/run-error (`app/orchestrator/faults.py`); `kube.stream_logs(follow=True)` streams pod logs into live `output` events; `knowledge/orchestrator.md`. |
| Cloud results upload internals (GCS/S3 upload helpers) | llmdbenchmark/utilities/README.md#cloud_upload-py | upload_results_dir, gcloud storage cp, aws s3 cp, output=local (no-op) | Y | off | ⬜ | llmdbenchmark/utilities/README.md#cloud_upload-py: gs://, s3:// destinations are intentionally NOT permitted in `security/allowlist.yaml` for the MVP; results stay local. Fix → ROADMAP_V4 Phase 47. |

## Area I — Project meta

| Feature | Source doc | Options / knobs | Optional? | Default | Coverage | Evidence / notes |
|---|---|---|---|---|---|---|
| Contribution / governance / quality (DCO, pre-commit, tests, CoC, security policy) | CONTRIBUTING.md; PR_SIGNOFF.md; tests/README.md; docs/developer-guide.md | git commit -s (DCO), pre-commit (pytest/ruff/detect-secrets), pytest tests/, OWNERS | N | n/a | ✅ | The agent has its own governance/quality stack: `knowledge/governance.md`; pytest suite; ruff+mypy+coverage gates (`pyproject.toml`, `Makefile`); flow-validation harness; root CI. Repos read-only, so it does not contribute upstream. |
| Stack discovery tool (llm-d-discover: URL → live stack config, BR-v0.2 output) | llm_d_stack_discovery/README.md; ARCHITECTURE.md | llm-d-discover <url>, --output-format benchmark-report, collectors (vLLM/GAIE/Gateway), env-var redaction + read-only RBAC | Y | off | ⬜ | llm_d_stack_discovery/README.md: a standalone non-lifecycle tool; the agent's endpoint/readiness probing covers the practical need without the BFS stack-graph walk + metadata backfill. Fix → ROADMAP_V4 Phase 56. |
| flexibility.md (placeholder doc) | docs/flexibility.md | (none) | N | n/a | ⬜ | docs/flexibility.md: an unpopulated stub ("To be populated."); zero substantive features; recorded only for doc-completeness. Fix → ROADMAP_V4 Phase 57. |
| FAQ / RBAC-audit placeholder docs | docs/faq.md; util/rbac_audit_report.md | (none) | N | n/a | ⬜ | docs/faq.md, util/rbac_audit_report.md: empty/placeholder docs with no documented features; recorded for doc-completeness. Fix → ROADMAP_V4 Phase 58. |

---

## Recommended default-on features

These are knobs the agent should enable by default (with a knowledge-driven opt-out), because
they materially improve the benchmark's value with low cost on the supported path.

1. **Benchmark metrics collection — `--monitoring` (THE headline fix).**
   - **Knobs:** `--monitoring` / `--no-monitoring` on `standup` (and threaded into `run` /
     `experiment`); `scenario.metricsScrapeEnabled=true` /
     `LLMDBENCH_VLLM_COMMON_METRICS_SCRAPE_ENABLED`; PodMonitor/ServiceMonitor creation;
     optional `monitoring.installPrometheusCrds`.
   - **Why default-on:** the agent already ships the full *consumer*
     (`app/validation/report.py` + `knowledge/standard_metrics.yaml`, Phase 25) that parses
     `results.observability` for KV-cache hit rate, schedule delay (queue-depth proxy), and
     GPU utilization — but those fields are perpetually `None` because the *producer*
     (`--monitoring` / `metricsScrapeEnabled`) is never activated. Turning it on lights up
     the serving-side metrics that distinguish a good benchmark from a bare latency number,
     at no extra GPU cost.
   - **Opt-out (knowledge, not Python):** Kind / clusters without the Prometheus operator
     CRDs cannot create PodMonitor/ServiceMonitor. The decision to emit `--no-monitoring`
     (or `monitoring.installPrometheusCrds`) belongs in `knowledge/observability.md`, keyed
     on a `probe_environment` CRD check — not an `if/elif` in Python.
   - **Closes:** ROADMAP_V4 Phase 27 (+ Phase 35 standup flag, + Phase 49 surfacing).

## Optional features to surface to users

Knobs worth offering to a user when their context calls for them (not default-on):

- **Model override `-m/--models`** (Phase 28) — per-call model selection rather than only via spec.
- **Cluster access `-k`/URL/token** (Phase 29) — for a remote cluster instead of the ambient context.
- **HuggingFace gated-model secret provisioning** (Phase 30) — for gated standups (`huggingface.enabled`).
- **Gateway class `--gateway-class`** (Phase 32) — istio / agentgateway / gke / epponly.
- **Multi-stack `--stack` / `--parallel`** (Phase 33) — N models behind one gateway, per-pool targeting.
- **WVA `-u/--wva`** (Phase 34) — Workload Variant Autoscaler (OpenShift-only; out of the kind MVP).
- **Cloud results sink `-r gs://` / `s3://`** (Phases 39 + 47) — for users with a bucket.
- **Local `--analyze` plot families** (Phase 40) — extra per-request / session / Prometheus matplotlib PNGs.
- **Kustomize `kustomize.*` config block** (Phase 46) — guideName/repoPath/patches/overlays.
- **Distributed tracing `tracing:` block** (Phase 54) — for advanced users with an OTel backend.

**Full catalog is this document.** The gap roadmap is [`ROADMAP_V4.md`](../ROADMAP_V4.md).
