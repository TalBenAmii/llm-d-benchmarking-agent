# Proposal Coverage Roadmap — llm-d Benchmarking Agent

> Maps every feature in [`llm-d-benchmarking-agent-proposal.md`](llm-d-benchmarking-agent-proposal.md)
> to its implementation status (evidence-backed audit, 2026-06-02). The gaps at the bottom were
> implemented by the **v3 "proposal-completion" autopilot** (`.claude/workflows/roadmap-v3-autopilot.js`)
> and **merged into `main`** — Phases 19-26 are all done. Completed rows are collapsed to one-liners;
> the only non-done items are partial (🔶) or out-of-scope (◽), kept in full.

**Legend:** ✅ done · 🔶 partial · ⬜ missing · ◽ out of scope (documented below)

---

## §3.2 Conversational Agent (Workload Advisor & Profile Builder)

| Feature | Status | Evidence / Notes |
|---|---|---|
| Use-case → `<scenario, harness, workload>` triplet mapping | ✅ | `knowledge/usecase_to_profile.yaml` + `app/validation/session_plan.py` — done |
| Knowledge-as-config (not hardcoded logic) | ✅ | judgment in `knowledge/*.md\|yaml`, loaded by `app/agent/prompt.py` — done |
| Concrete `run` invocation output (selected triplet) | ✅ | `app/tools/execute.py` (argv + dry-run preview) — done |
| Structured interview: scale, QoS/SLO, infra | ✅ | `SessionPlan` (TTFT/TBT/P99/throughput, spec, harness, workload) — done |
| Structured interview: **token characteristics / prefix-reuse ratio** | ✅ | P19: elicitation guidance in `knowledge/sweep_playbook.md` — done |
| Harness **recommendation** (guidellm sweep vs inference-perf SLO) | 🔶 | Knowledge present (`multi_harness.md`); reasoned by the LLM, no dedicated surfacing — strengthened by P19/P20 |
| **DOE experiment-FILE generation** (factors × levels → treatments) | ✅ | P19: `generate_doe_experiment` (`app/tools/doe.py`) — done |
| **Well-lit-path advisor** (workload shape → which scenario) | ✅ | P20: `knowledge/welllit_path_advisor.yaml` (shape→scenario + signals + deploy_path), inlined into prompt — done |

## §3.3 Benchmark Orchestrator (K8s Job Lifecycle)

| Feature | Status | Evidence / Notes |
|---|---|---|
| Manifest generation (Job per run / per DOE treatment) | ✅ | `app/orchestrator/job.py` `build_job_manifest`; `controller.run_sweep` — done |
| OOM / timeout / eviction detection + fault classification | ✅ | `app/orchestrator/faults.py` (6 kinds) — done |
| Retry policy + dead-letter for failing treatments | ✅ | `controller.run_with_retries` — done |
| Parallel DOE sweeps w/ concurrency limit | ✅ | `controller.run_sweep` (`asyncio.Semaphore`) — done |
| **Stateless design — reconstruct state from cluster** | ✅ | `controller.reconstruct` from Job/pod labels — done (the 40% centerpiece) |
| Cleanup of terminal Jobs / ConfigMaps; preserve artifacts | ✅ | `controller.cleanup` (terminal-only; results PVC preserved) — done |
| **Real-time log streaming** from pods → UI | ✅ | P21: orchestrator tails the pod's live logs into `output` events (`kube.stream_logs`), best-effort — done |
| **Checkpoint/resume for long DOE experiments** | ✅ | P22: per-sweep ConfigMap (`app/orchestrator/checkpoint.py`); `run_sweep(sweep_id)` skips completed treatments, `reconstruct_sweep` rebuilds — done |
| **Resource management** (node affinity, GPU-type, anti-starvation) | ✅ | P23: optional `Scheduling` on `JobSpec`/`build_job_manifest`; knowledge in `resource_management.md` — done |
| Dependency mgmt: **endpoint health-check before submit** + optional auto-standup | ✅ | P24: `app/orchestrator/readiness.py` + `check_endpoint_readiness`; `orchestrate_benchmark_run` gates on `require_ready_endpoint` (standup = approval-gated suggestion) — done |
| Job monitoring via Watch API | ◽ | Implemented poll-based by design (documented in `controller.py`); acceptable — left as-is |

## §3.4 Results Analyzer

| Feature | Status | Evidence / Notes |
|---|---|---|
| Goodput computation (SLO-aware) | ✅ | `app/validation/analysis.py` `evaluate_slo` + goodput interpolation — done |
| A/B comparison of two runs | ✅ | `app/tools/compare.py` — done |
| DOE Pareto-optimal config identification | ✅ | `analysis.py` `pareto_analysis` — done |
| Report generation (BR-v0.2 JSON + human summary) | ✅ | `app/validation/report.py` `summarize_report` — done |
| Metric extraction: TTFT, TBT/TPOT, ITL, latency P50/P95/P99, throughput | ✅ | `report.py` / `analysis.py` — done |
| Metric extraction: **KV-cache hit rate, schedule delay, GPU utilization** | ✅ | P25: via `knowledge/standard_metrics.yaml` — done |

## §4 Distributed-Systems Concepts · §2.2 Harnesses

| Feature | Status | Evidence / Notes |
|---|---|---|
| Job scheduling / fault tolerance / stateless design | ✅ | See §3.3 (retry, dead-letter, reconstruct) — done |
| Resource management (quotas, affinity, no starvation) | ✅ | P23: optional `Scheduling` on benchmark Jobs — done |
| Observability — Prometheus/Grafana, live metrics during runs | ✅ | Phase 7: `/metrics`, `observe_run_metrics`, Grafana dashboard — done |
| Observability — real-time benchmark-pod log streaming | ✅ | P21: `stream_logs(follow=True)` → live `output` events — done |
| Harness catalog: inference-perf, guidellm, vLLM, InferenceMAX, nop | ✅ | Runtime-discovered from the repo (no hardcoded whitelist) — done |
| guidellm multimodal (image/video/audio) guidance | ◽ | Low-value stretch; documented as out-of-scope below |

## §5 / §7 / §8 — Deliverables, Stack, Grading

| Feature | Status | Evidence / Notes |
|---|---|---|
| MVP end-to-end (interview → run → parse → compare) on kind/CPU-sim | ✅ | First milestone; flow-validation harness — done |
| Multi-harness in one session | ✅ | Phase 10 `compare_harness_runs` — done |
| Historical result storage + trends | ✅ | Phase 5 `result_history` + UI — done |
| Capacity Planner pre-flight (Configuration Explorer) | ✅ | Phase 6 `check_capacity` — done |
| Helm chart / Kustomize single-command deploy + RBAC | ✅ | Phase 8 `deploy/` — done |
| Technical docs: architecture, API, deployment, user guide | ✅ | Phase 9 `docs/` — done |
| Ops/contrib docs: SECURITY, TROUBLESHOOTING, CONTRIBUTING, CHANGELOG | ✅ | v2 Phase 17: `docs/{SECURITY,TROUBLESHOOTING,CONTRIBUTING,CHANGELOG}.md` + alert rules — done |
| CI/CD pipeline (GitHub Actions) | 🔶 | Exists at repo root (`/.github/workflows/agent-flow-validation.yml`, hermetic flow + opt-in live eval) |
| CI: **linting (ruff) + type-checking (mypy) + coverage** | ✅ | v2 Phase 14: ruff + mypy (strict) + coverage gate (≥85%) — done |
| CI: **integration tests with llm-d-inference-sim** | ✅ | v3 Phase 26: opt-in env-gated `tests/integration/` + non-gating CI job — done |
| Upstream-PR-ready agent module | 🔶 | Structure aligns; strengthened by v2 docs + v3 features |

---

## v3 "proposal-completion" autopilot — the missing features

Eight phases on `feature/roadmap-v3` (off `feature/roadmap-v2`), each hermetically testable, obeying
thin-code/thick-agent + allowlist-as-data. Built by `roadmap-v3-autopilot.js`, **all merged into `main`** — all done.

| # | Phase | Proposal ref | Delivers |
|---|---|---|---|
| **P19** ✅ | DOE experiment-file generator | §5.2 #1, §3.2 | `generate_doe_experiment` cross-products factors × levels → treatments + structural YAML validation; factors/levels live in `knowledge/`; + token-characteristics elicitation — done |
| **P20** ✅ | Well-lit-path advisor | §5.2 | `knowledge/welllit_path_advisor.yaml` (workload shape → llm-d scenario), verified against catalog, inlined into prompt + via `read_knowledge` — done |
| **P21** ✅ | Real-time log streaming | §3.3, §4 | `stream_logs(follow=True)` in the orchestrator run loop → live `output` events; best-effort — done |
| **P22** ✅ | DOE checkpoint/resume | §3.3, §4 | per-sweep ConfigMap; `run_sweep(sweep_id)` resumes skipping completed treatments, `reconstruct_sweep` rebuilds; no `sweep_id` = stateless — done |
| **P23** ✅ | Resource management | §4 | optional `nodeSelector`/`affinity`/`tolerations` + GPU resource/anti-affinity on `JobSpec`/`build_job_manifest`; placement from knowledge — done |
| **P24** ✅ | Endpoint health-check + optional auto-standup | §3.3 | gate Job submit on endpoint readiness; approval-gated `standup` suggestion when no healthy stack — done |
| **P25** ✅ | Analyzer metric completeness | §3.4 | extract/surface KV-cache hit rate, schedule delay, GPU utilization (graceful `None`); in summaries + Pareto — done |
| **P26** ✅ | llm-d-inference-sim integration tests | §5.3, §7 | opt-in env-gated integration layer (skipped by default) + non-gating CI job — done |

**Waves** (conflict-/dependency-ordered): `[[19,20,25],[21,23],[22,24],[26]]` — disjoint authoring/analyzer
phases first; orchestrator run-loop phases sequenced (P21 before P22); integration tests last.

## Out of scope (documented, not built)

- **Watch-API monitoring** — the orchestrator is intentionally poll-based (`controller.py`
  documents why: simpler/robust for sub-10-min jobs); left as a design choice.
- **guidellm multimodal** (image/video/audio) — low-value stretch; the harness supports it and
  any catalog workload is already invocable, so this is a knowledge note at most.
- **Real GPU lab-cluster demo** — requires hardware (proposal weeks 10+); P23 makes the
  manifests GPU-ready and P26 exercises the stack via the CPU mock, but a live GPU run is
  environment-dependent, not code.
