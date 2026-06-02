# Proposal Coverage Roadmap — llm-d Benchmarking Agent

> Maps every feature in [`llm-d-benchmarking-agent-proposal.md`](llm-d-benchmarking-agent-proposal.md)
> to its implementation status, derived from an evidence-backed audit of the codebase
> (2026-06-02). The gaps at the bottom are implemented by the **v3 "proposal-completion"
> autopilot** (`.claude/workflows/roadmap-v3-autopilot.js`), which builds on
> `feature/roadmap-v2` and **never touches `main`**.

**Legend:** ✅ done · 🔄 in progress (running `roadmap-v2-autopilot`) · ⬜ missing → built by v3 · ◽ out of scope (documented below)

---

## §3.2 Conversational Agent (Workload Advisor & Profile Builder)

| Feature | Status | Evidence / Notes |
|---|---|---|
| Use-case → `<scenario, harness, workload>` triplet mapping | ✅ | `knowledge/usecase_to_profile.yaml`; `app/validation/session_plan.py` validates against the live catalog |
| Knowledge-as-config (not hardcoded logic) | ✅ | All judgment in `knowledge/*.md|yaml`; `app/agent/prompt.py` loads at runtime |
| Concrete `run` invocation output (selected triplet) | ✅ | `app/tools/execute.py` builds the `llmdbenchmark run` argv + dry-run preview |
| Structured interview: scale, QoS/SLO, infra | ✅ | `SessionPlan` captures SLO (TTFT/TBT/P99/throughput), spec, harness, workload |
| Structured interview: **token characteristics / prefix-reuse ratio** | ✅ | P19: explicit token-characteristics / prefix-reuse elicitation guidance in `knowledge/sweep_playbook.md` |
| Harness **recommendation** (guidellm sweep vs inference-perf SLO) | 🔶 | Knowledge present (`multi_harness.md`); reasoned by the LLM, no dedicated surfacing — strengthened by P19/P20 |
| **DOE experiment-FILE generation** (factors × levels → treatments matrix) | ✅ | P19: `generate_doe_experiment` (`app/tools/doe.py`) cross-products factors × levels → treatments, emits + structurally validates the experiment YAML |
| **Well-lit-path advisor** (workload shape → which scenario) | ✅ | P20: `knowledge/welllit_path_advisor.yaml` maps workload shape → llm-d scenario (prefix-heavy chat → precise-prefix-cache-routing; long-context RAG → pd-disaggregation; throughput → optimized-baseline) with selecting signals + `deploy_path`; referenced ids verified against the catalog; inlined into the prompt |

## §3.3 Benchmark Orchestrator (K8s Job Lifecycle)

| Feature | Status | Evidence / Notes |
|---|---|---|
| Manifest generation (Job per run / per DOE treatment) | ✅ | `app/orchestrator/job.py` `build_job_manifest`; `controller.py` `run_sweep` |
| OOM / timeout / eviction detection + fault classification | ✅ | `app/orchestrator/faults.py` (6 kinds); `tests/test_orchestrator_faults.py` |
| Retry policy + dead-letter for failing treatments | ✅ | `controller.py` `run_with_retries` |
| Parallel DOE sweeps w/ concurrency limit | ✅ | `controller.py` `run_sweep` (`asyncio.Semaphore`) |
| **Stateless design — reconstruct state from cluster** | ✅ | `controller.py` `reconstruct` from Job/pod labels (`LABEL_SESSION/SWEEP/TREATMENT`) — the 40% centerpiece |
| Cleanup of terminal Jobs / ConfigMaps; preserve artifacts | ✅ | `controller.py` `cleanup` (terminal-only default; results PVC preserved) |
| **Real-time log streaming** from pods → UI | ✅ | P21: `controller.run_with_retries`/`run_attempt` now tail the benchmark pod's live logs (`kube.stream_logs`) into per-line `output` events during the run; `orchestrate_benchmark_run` wires a `ctx.emit("output", …)` sink — same transport as streamed command output, best-effort (a failing tail never breaks the run) |
| **Checkpoint/resume for long DOE experiments** | ✅ | P22: `run_sweep(sweep_id, namespace)` persists each treatment's completed/in-flight state + outcome to a per-sweep **ConfigMap** (`app/orchestrator/checkpoint.py` `SweepCheckpoint`/`CheckpointStore`, over `kube.list_configmaps`); on resume, completed treatments are SKIPPED (prior outcome reconstructed, result still covers all N) and the sweep continues idempotently. `reconstruct_sweep` rebuilds purely from the ConfigMap; no `sweep_id` ⇒ stateless behavior unchanged |
| **Resource management** (node affinity, GPU-type selection, anti-starvation) | ✅ | P23: optional `Scheduling` on `JobSpec`/`build_job_manifest` — `node_selector`/`tolerations`/raw `affinity`/GPU resource+type label + pod anti-affinity from `avoid_labels`; unset ⇒ baseline manifest; judgment as DATA in `knowledge/resource_management.md` |
| Dependency mgmt: **endpoint health-check before submit** + optional auto-standup | ✅ | P24: `app/orchestrator/readiness.py` reads `kubectl get endpoints` (+ corroborates with `run --list-endpoints`) for a ready backing endpoint; read-only `check_endpoint_readiness` tool; `orchestrate_benchmark_run` gates on it by default (`require_ready_endpoint=true`) — unready ⇒ submits nothing, returns `{submitted:false, ready:false, readiness, standup_suggestion}` (standup is an approval-gated suggestion only) |
| Job monitoring via Watch API | ◽ | Implemented poll-based by design (documented in `controller.py`); acceptable — left as-is |

## §3.4 Results Analyzer

| Feature | Status | Evidence / Notes |
|---|---|---|
| Goodput computation (SLO-aware) | ✅ | `app/validation/analysis.py` `evaluate_slo` + goodput interpolation |
| A/B comparison of two runs | ✅ | `app/tools/compare.py` |
| DOE Pareto-optimal config identification | ✅ | `analysis.py` `pareto_analysis` |
| Report generation (BR-v0.2 JSON + human summary) | ✅ | `app/validation/report.py` `summarize_report` |
| Metric extraction: TTFT, TBT/TPOT, ITL, latency P50/P95/P99, throughput | ✅ | `report.py` / `analysis.py` |
| Metric extraction: **KV-cache hit rate, schedule delay, GPU utilization** | ✅ | `report.py` / `analysis.py` via `knowledge/standard_metrics.yaml` — **P25** done |

## §4 Distributed-Systems Concepts · §2.2 Harnesses

| Feature | Status | Evidence / Notes |
|---|---|---|
| Job scheduling / fault tolerance / stateless design | ✅ | See §3.3 (retry, dead-letter, reconstruct) |
| Resource management (quotas, affinity, no starvation) | ✅ | P23: optional `Scheduling` — node affinity/GPU selection/anti-starvation placement on benchmark Jobs |
| Observability — Prometheus/Grafana, live metrics during runs | ✅ | Phase 7: `/metrics`, `observe_run_metrics`, Grafana dashboard |
| Observability — real-time benchmark-pod log streaming | ✅ | P21: `stream_logs(follow=True)` wired into the orchestrator run loop → live `output` events during a run |
| Harness catalog: inference-perf, guidellm, vLLM, InferenceMAX, nop | ✅ | Runtime-discovered from the repo; any catalog harness invocable (no hardcoded whitelist) |
| guidellm multimodal (image/video/audio) guidance | ◽ | Low-value stretch; documented as out-of-scope below |

## §5 / §7 / §8 — Deliverables, Stack, Grading

| Feature | Status | Evidence / Notes |
|---|---|---|
| MVP end-to-end (interview → run → parse → compare) on kind/CPU-sim | ✅ | First milestone; flow-validation harness |
| Multi-harness in one session | ✅ | Phase 10 `compare_harness_runs` |
| Historical result storage + trends | ✅ | Phase 5 `result_history` + UI |
| Capacity Planner pre-flight (Configuration Explorer) | ✅ | Phase 6 `check_capacity` |
| Helm chart / Kustomize single-command deploy + RBAC | ✅ | Phase 8 `deploy/` |
| Technical docs: architecture, API, deployment, user guide | ✅ | Phase 9 `docs/` |
| Ops/contrib docs: SECURITY, TROUBLESHOOTING, CONTRIBUTING, CHANGELOG | 🔄 | **v2 Phase 17** (in progress) |
| CI/CD pipeline (GitHub Actions) | 🔶 | Exists at repo root (`/.github/workflows/agent-flow-validation.yml`, hermetic flow + opt-in live eval) |
| CI: **linting (ruff) + type-checking (mypy) + coverage** | 🔄 | **v2 Phase 14** (in progress) |
| CI: **integration tests with llm-d-inference-sim** | ⬜→v3 | §5.3/§7 explicit; all current tests are hermetic fakes — **P26** |
| Upstream-PR-ready agent module | 🔶 | Structure aligns; strengthened by v2 docs + v3 features |

---

## v3 "proposal-completion" autopilot — the missing features

Eight phases on `feature/roadmap-v3` (off `feature/roadmap-v2`), each hermetically testable,
obeying thin-code/thick-agent + allowlist-as-data. Built by `roadmap-v3-autopilot.js`.

| # | Phase | Proposal ref | Delivers |
|---|---|---|---|
| **P19** ✅ | DOE experiment-file generator | §5.2 #1, §3.2 | A tool that authors a DOE experiment YAML — cross-products agent-chosen *factors × levels* into *treatments* (mechanism), validated structurally against the repo's experiment examples; **which** factors/levels live in `knowledge/`. Also adds explicit token-characteristics elicitation guidance. |
| **P20** ✅ | Well-lit-path advisor | §5.2 | `knowledge/welllit_path_advisor.yaml` mapping workload shape → llm-d scenario guide (prefix-heavy→precise-prefix-cache-routing, long-context→pd-disaggregation, throughput→optimized-baseline); referenced scenarios verified against the catalog; inlined into the system prompt + served via `read_knowledge`. |
| **P21** ✅ | Real-time log streaming | §3.3, §4 | Wired `stream_logs(follow=True)` into the orchestrator run loop so benchmark-pod logs surface as live `output` events during a run (not just end-of-run); `orchestrate_benchmark_run` forwards each line via `ctx.emit("output", …)` — best-effort, a failing tail never breaks the run. |
| **P22** ✅ | DOE checkpoint/resume | §3.3, §4 | Persist completed/in-flight treatment IDs + outcome to a per-sweep ConfigMap (cluster source of truth, consistent with the stateless design); `run_sweep(sweep_id)` resumes a sweep skipping completed treatments (idempotent), `reconstruct_sweep` rebuilds from the ConfigMap, and omitting `sweep_id` keeps the original stateless behavior. |
| **P23** | Resource management | §4 | Extend `JobSpec`/`build_job_manifest` with optional `nodeSelector`/`affinity`/`tolerations` + GPU resource and anti-affinity so benchmark Jobs don't starve the measured stack; GPU/placement supplied at plan time via knowledge. |
| **P24** | Endpoint health-check + optional auto-standup | §3.3 | Before submitting a benchmark Job, gate on inference-endpoint readiness; optionally trigger `standup` (approval-gated) when no healthy stack is present. |
| **P25** | Analyzer metric completeness | §3.4 | Extract + surface KV-cache hit rate, schedule delay, and GPU utilization from BR-v0.2 / harness-native output (gracefully `None` when absent); include in summaries + Pareto objectives where sensible. |
| **P26** | llm-d-inference-sim integration tests | §5.3, §7 | An **opt-in** integration layer (env-gated, skipped by default to keep the suite hermetic) that stands up `llm-d-inference-sim` and runs analyze/compare against a real mock report; plus a non-gating CI job. |

**Waves** (conflict- & dependency-ordered): `[[19,20,25],[21,23],[22,24],[26]]` — disjoint
authoring/analyzer phases first; orchestrator run-loop phases sequenced (P21 before P22, both
touch the run loop); integration tests last so they can exercise the new features.

## Out of scope (documented, not built)

- **Watch-API monitoring** — the orchestrator is intentionally poll-based (`controller.py`
  documents why: simpler/robust for sub-10-min jobs); left as a design choice.
- **guidellm multimodal** (image/video/audio) — low-value stretch; the harness supports it and
  any catalog workload is already invocable, so this is a knowledge note at most.
- **Real GPU lab-cluster demo** — requires hardware (proposal weeks 10+); P23 makes the
  manifests GPU-ready and P26 exercises the stack via the CPU mock, but a live GPU run is
  environment-dependent, not code.
