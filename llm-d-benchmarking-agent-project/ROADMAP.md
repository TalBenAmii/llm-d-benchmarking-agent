# ROADMAP — llm-d Benchmarking Agent

> **Tracking record (all phases below are DONE).** Tracks the autonomous build-out that took the
> project from "MVP chat agent that drives the `llmdbenchmark` CLI" to the full vision in
> [`llm-d-benchmarking-agent-proposal.md`](llm-d-benchmarking-agent-proposal.md): a
> Kubernetes-native **conversational agent + benchmark orchestrator + results analyzer**.
> See [`PROGRESS.md`](PROGRESS.md) for the per-session work log and
> [`docs/CHANGELOG.md`](docs/CHANGELOG.md) for the released changelog. Forward-looking gap work
> now lives in [`ROADMAP_V4.md`](ROADMAP_V4.md) (Phases 27+, none done).
>
> **Approved 2026-06-01.** v1 (Phases 0–10) and v2 (Phases 11–18) were developed on integration
> branches `feature/roadmap` / `feature/roadmap-v2`; v3 (Phases 19–26) on `feature/roadmap-v3`.
> v1, v2 and v3 were ultimately merged into `main`. Latest DONE: **Phase 26**.

## Why this order

The proposal is a Technion Parallel & Distributed Systems lab project with an explicit grading
rubric, which (plus the user's `todo` file) drove the ordering: quick user-requested wins first
(better demo + Observability story + on-ramp to orchestration), then the 40%-weighted K8s
orchestrator, then analyzer differentiators, then stretch goals and packaging/docs.

| Grade dimension | Weight |
|---|---|
| Kubernetes job orchestration (lifecycle, OOM/timeout/eviction, restart-resilience) | **40%** |
| System design & API quality | 25% |
| End-to-end functionality | 20% |
| Code quality & documentation | 15% |

## Autonomous execution rules (still in force)
- **Branching:** each wave has an integration branch (`feature/roadmap[-v2/-v3]`); each phase is a
  `feature/roadmap*-pN-<slug>` branch merged in after its full-suite gate is green. Independent
  parts use **separate worktrees partitioned by file/module**. `main` is touched only by the final
  wave merge.
- **Tests:** pytest only — no long/real benchmark runs, no GPU. The orchestrator is validated with a
  fake kube client and the hermetic CaptureRunner harness.
- **Thin code, thick agent** is the law: mechanism in Python, judgment in `knowledge/`.
- **Docs:** `ROADMAP.md` + `PROGRESS.md` (and affected `.md`/knowledge files) updated each phase.

---

## v1 — MVP → orchestrator + analyzer (Phases 0–10) — branch `feature/roadmap` → main

- **Phase 0 — Autonomous scaffolding** — done. Worktree-portable tests (conftest resolves the sibling repo via `get_settings()`); `ROADMAP.md`/`PROGRESS.md` living docs.
- **Phase 1 — Command transparency, debug mode, UI polish** — done. `command` event for every executed command (read-only + post-approval mutating), persisted/replayed; inline `$ cmd` consoles + global executed-command log + Debug-mode toggle; reusable range-input foundation (no invented sliders).
- **Phase 2 — Parallel sessions & parallel benchmark runs** — done. Configurable cross-session concurrency cap (`max_concurrent_runs`, mutating-only semaphore); background-safe runs survive WS disconnect + replay on reconnect; runner bounds the whole process lifecycle under the deadline. (Its two deferrals — abandoned-run slot hold + live stream on reconnect — were closed by Phases 16 and 15.)
- **Phase 3 — Kubernetes-native Benchmark Orchestrator** — done *(the 40% centerpiece)*. `app/orchestrator/` + `orchestrate_benchmark_run`: KubeClient over allowlisted `kubectl`, poll-based job watch, fault classification (OOM/timeout/eviction/unschedulable/image/run-error), retry + dead-letter, parallel sweep + cleanup, stateless cluster reconstruction. Judgment in `knowledge/orchestrator.md`. (Its RBAC/in-cluster-image deferral was closed by Phase 8.)
- **Phase 4 — Results Analyzer: goodput, SLO filtering, Pareto/DoE** — done. `analyze_results` + `app/validation/analysis.py`: SLOTargets in the SessionPlan, per-run SLO verdict over the percentile ladder, honest goodput, Pareto-optimal selection + SLO-feasible frontier; grounded in `knowledge/analysis.md`.
- **Phase 5 — Historical result storage + trends UI** — done. Cross-session history store via one `result_history` tool (store/list/get/trend/delete) + UI results-browser/trends view.
- **Phase 6 — Configuration Explorer / Capacity Planner pre-flight** — done. `check_capacity` tool runs a memory/feasibility pre-flight (KV + weights + activation vs accelerator memory) at the plan gate, advisory or enforced.
- **Phase 7 — Observability: Prometheus/Grafana** — done. `/metrics` endpoint + agent/orchestrator instrumentation, `observe_run_metrics` tool (live `kubectl top` during a run), Grafana dashboard + scrape config under `deploy/observability/`.
- **Phase 8 — Packaging: container image + Helm/Kustomize** — done. Hardened non-root Dockerfile + Helm chart and Kustomize base/overlay (Deployment/Service/SA + namespaced least-privilege Role/RoleBinding matching the kubectl verbs used); resolves the Phase-3 RBAC deferral.
- **Phase 9 — Documentation suite + upstream-PR readiness** — done. `docs/` suite (ARCHITECTURE, API, DEPLOYMENT, USER_GUIDE, README index) + refreshed root README/CLAUDE.md; docs-only.
- **Phase 10 — Multi-harness orchestration in one session** — done. `compare_harness_runs` tool + `compare_across_harnesses()` (group by detected harness, cross-validate shared metrics, no cross-harness winner), backed by `knowledge/multi_harness.md`.

---

## v2 — production operability, trust & quality (Phases 11–18) — branch `feature/roadmap-v2` → main

- **Phase 11 — Structured logging + correlation IDs** — done. Stdlib-only JSON logging (`app/observability/logging.py`) + `logctx` contextvars; `corr_id` minted at the WS boundary rides into the agent loop / every tool / the command runner so one turn is grep-traceable. `LOG_LEVEL`/`LOG_FORMAT` config; secrets never logged. Judgment in `knowledge/logging.md`.
- **Phase 12 — API trust: auth + rate-limit + CORS** — done. Stdlib-only, default-off controls: optional Bearer auth (constant-time, guards HTTP + `/ws`), `TokenBucket`/`RateLimiter` on `/api/*` (429 on empty; `/healthz`+`/metrics` exempt), `CORSMiddleware` when `CORS_ALLOW_ORIGINS` set. Config `AUTH_ENABLED`/`AUTH_TOKEN`/`RATE_LIMIT_*`/`CORS_ALLOW_ORIGINS`. Judgment in `knowledge/api_trust.md`.
- **Phase 13 — Allowlist governance: per-command timeouts + quotas** — done. `timeout_s` + `quota{per_session,per_day}` as DATA in `security/allowlist.yaml` (validated at startup), removed the parallel `_TIMEOUTS` table; `app/security/quota.py` refuses over-quota before execution with `QuotaError`. Judgment in `knowledge/governance.md`.
- **Phase 14 — Quality gates: ruff + mypy + coverage** — done. CI-enforced gates in `pyproject.toml`/`Makefile`/`.github/workflows/agent-flow-validation.yml`: ruff (clean), mypy strict over `app`, coverage `--cov-fail-under=85` (achieved 88.90%). `tests/test_quality_gates.py` pins the wiring.
- **Phase 15 — WebSocket protocol hardening + live event buffer** — done. `/ws` validates inbound frames against a Pydantic tagged union (`app/agent/ws_schemas.py`, `extra="forbid"`); malformed → `protocol_error`, socket kept alive. Bounded per-turn live ring buffer so a mid-turn reconnect replays missed events; unified outbound envelope; `ping`→`pong`. (Closes a Phase-2 deferral.)
- **Phase 16 — Run lifecycle & readiness** — done. `app/agent/lifecycle.py` `RunRegistry` + `cancel_run` tool cancels another session's running turn from outside (frees the cap slot, reaps the child process group, no orphaned Job); SIGTERM graceful-shutdown cancels in-flight runs; `/readyz` gains `runner_ok`. *When* to cancel is judgment in `knowledge/run_lifecycle.md`. (Closes Phase-2 deferrals.)
- **Phase 17 — Operability docs + alert rules** — done. `docs/SECURITY`, `docs/TROUBLESHOOTING`, `docs/CONTRIBUTING`, a Keep-a-Changelog, all linked from the docs index; `deploy/observability/alerts.rules.yaml` (5 Prometheus rules over existing metrics). Docs+data only.
- **Phase 18 — Workspace lifecycle: retention/GC + startup self-check** — done. `app/storage/retention.py` config-driven GC over scratch areas (policy as DATA, excludes live sessions), one-shot at startup; structured `self_check` surfaced via a new `/readyz` probe (liveness stays on `/healthz`).

---

## v3 — proposal-completion features (Phases 19–26) — branch `feature/roadmap-v3` → main

- **Phase 19 — DOE experiment-file generator + token-characteristics elicitation** — done. `generate_doe_experiment` tool + `app/validation/doe.py`: cross-products agent-supplied factors×levels into a treatments matrix, emits experiment YAML into the session workspace, validates structurally against the live llm-d-benchmark example (no vendored copy). Factor/level selection is judgment in `knowledge/sweep_playbook.md`.
- **Phase 20 — Well-lit-path advisor** — done. `knowledge/welllit_path_advisor.yaml`: ADVISORY DATA mapping workload SHAPE → llm-d well-lit-path scenario with selecting signals, rationale, candidate workloads, `deploy_path` reach flag; inlined via `CORE_KNOWLEDGE` and served by `read_knowledge`. `deploy_path_playbook.md` points at it. (`tests/test_welllit_advisor.py` asserts every archetype resolves.)
- **Phase 21 — Real-time benchmark-pod log streaming** — done. Wired `kube.stream_logs(follow=True)` into the run loop via an optional `on_log_line` sink; `orchestrate_benchmark_run` builds it from `ctx.emit` so each line surfaces as an `output` event. Best-effort: a failing tail never breaks the run; no emitter ⇒ disabled.
- **Phase 22 — DOE checkpoint/resume for long sweeps** — done. `app/orchestrator/checkpoint.py` persists each treatment's state to a per-sweep **ConfigMap** (cluster source of truth, over allowlisted `kubectl`); `run_sweep(sweep_id, namespace)` skips COMPLETED treatments and resumes idempotently; omitting `sweep_id` ⇒ original stateless behavior byte-for-byte. Judgment in `knowledge/orchestrator.md`.
- **Phase 23 — Resource management: node affinity / GPU selection / anti-starvation** — done. Optional `Scheduling` on `JobSpec`/`build_job_manifest` (GPU resource+count, GPU-type node label, `node_selector`, `tolerations`, raw `affinity`, anti-affinity from `avoid_labels`); unset ⇒ manifest byte-for-byte the cpu/memory baseline. WHICH-GPU/WHERE judgment is DATA in `knowledge/resource_management.md`. No allowlist change.
- **Phase 24 — Endpoint health-check before submit (+ optional auto-standup)** — done. `app/orchestrator/readiness.py` reads `kubectl get endpoints` (corroborated by `run --list-endpoints`) for a `ready` verdict; `check_endpoint_readiness` tool; `orchestrate_benchmark_run` gates on it by default (`require_ready_endpoint=true`) — submits nothing if unready, returns an approval-gated standup *suggestion*. Judgment in `knowledge/orchestrator.md` + `knowledge/preconditions.md`.
- **Phase 25 — Analyzer metric completeness: KV-cache hit rate, schedule delay, GPU utilization** — done. `summarize_report`/`analysis` parse and surface the §3.4 metrics from either the BR v0.2 `observability.components[].aggregate` shape or a harness-native entry (field discovery as DATA in `knowledge/standard_metrics.yaml`), `None` when absent — never fabricated; informational-only Pareto objectives (out of dominance).
- **Phase 26 — llm-d-inference-sim integration tests (opt-in)** — done. `tests/integration/`: an always-running hermetic check driving a sim-shaped BR v0.2 fixture (read live) through `analyze_results`/`compare_reports`, plus a live test that is SKIPPED by default (runs only when `LLMD_SIM_INTEGRATION=1` and the sim is locatable, never hangs). Non-gating CI job; guidance in `knowledge/sim_integration.md`; docs in `docs/VALIDATION.md` + `docs/CONTRIBUTING.md`.
