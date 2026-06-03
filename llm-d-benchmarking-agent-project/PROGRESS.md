# PROGRESS LOG

Reverse-chronological log of the autonomous roadmap effort. One entry per work session /
phase milestone. See [`ROADMAP.md`](ROADMAP.md) for the plan and phase status.

Branch: `feature/roadmap` (integration; never merged to `main` during this effort).
Test baseline at start (primary checkout `main` @ `04c06fe`): **111 passed / 5 skipped**.
Latest completed phase: **Phase 26** (suite **591 passed / 9 skipped**). Each completed phase below
is collapsed to a one-liner (date · phase · what shipped · branch · final suite count); see git
history for the full per-phase narrative. ROADMAP_V4.md (Phases 27-58) is the forward-looking plan.

---

## Completed phases (newest first)

- 2026-06-03 — Phase 62 (ROADMAP_V4): Gated-model access pre-flight before standup. The already-allowlisted read-only
  capacity bridge `scripts/capacity_check.py` (driven by `app/capacity/planner.py`) now also calls the benchmark repo's
  OWN `llmdbenchmark.utilities.huggingface.check_model_access` / `GatedStatus` (never reimplemented) and returns a
  token-free `{gated, authorized, reason}` block alongside the sizing verdict. `CapacityVerdict` gained
  `gated`/`authorized`/`gated_reason` fields (defaulted → non-gated/legacy paths unchanged), wired via a pure-field-copy
  `merge_gated_access` (no `if/elif`). Per-status judgment (PUBLIC/authorized → proceed; gated+unauthorized → provision
  the secret via Phase 30) lives in `knowledge/capacity.md`, not Python. `HF_TOKEN` is read from the scrubbed child env
  only and never echoed into the result, events, or logs. No allowlist change. Hermetic tests
  (`tests/test_capacity_gated.py`) drive a fixture `ModelAccessResult` per `GatedStatus` and assert the verdict + token
  non-leak. Merged into `feature/roadmap-v4`; suite **756 passed / 20 skipped**; ruff + mypy clean.
- 2026-06-03 — Phase 61 (ROADMAP_V4): Right-size the harness launcher CPU for small/Kind clusters. Added a read-only
  `node_capacity` probe (per-node allocatable/capacity CPU + min-allocatable across nodes via `kubectl get nodes -o
  json`) to `probe_environment` (`app/tools/probe.py`), and a backend-only `harness_cpu_nr` flag plumbed as the
  `LLMDBENCH_HARNESS_CPU_NR` env var through `execute.py` → `context.run_command(env=)` → `runner._build_env` (merged
  last so it wins; never an allowlist flag, never reaches the browser); the lower-it-or-not / to-what (inference-perf
  multi-process vs vllm-benchmark single-process) judgment lives in `knowledge/harness_sizing.md`, not Python. Turns a
  silent `FailedScheduling`/`Pending` launcher pod into a scheduled run on the MVP Kind path. Merge into
  `feature/roadmap-v4` reconciled the two newly-added probes against Phase 27/59 (probe-emit/exec parity count 7→8:
  `prometheus_crds` + `node_capacity`). Branch `feature/roadmap-v4-p61-harness-cpu-size`. Suite **735 passed / 20
  skipped / 0 failed**; ruff + mypy clean. — done
- 2026-06-03 — Phase 59 (ROADMAP_V4): Model-load serving-readiness gate (`/v1/models` vs `/health` + stuck-pod
  diagnostics). Extended the endpoint-readiness path (`app/orchestrator/readiness.py`, `app/tools/readiness.py`,
  `app/tools/registry.py`) to classify a `Running`-but-`NotReady` model server as "still loading weights (keep
  waiting)" vs "wedged/broken (stop waiting)" from pod readiness conditions / `restartCount` / age (8000 prefill,
  8200 decode) plus a GET-only `curl` probe pinned by `security/allowlist.yaml` to the enum `{/v1/models, /health}`
  on in-namespace `*.svc` URLs. The loading-vs-broken JUDGMENT lives in the new `knowledge/readiness_probes.md`
  (no Python `if/elif`). Hermetic fixtures only (canned `kubectl`/`curl` bodies). Suite: 723 passed, 20 skipped,
  0 failed (ruff + mypy clean).

- 2026-06-03 — Phase 27 (ROADMAP_V4): Default-enable benchmark `--monitoring` + surface `results.observability`
  (THE headline observability gap — closed). Added a subcommand-aware `monitoring` flag to `ExecuteInput.flags` +
  `build_argv`: `--monitoring` for standup/run/experiment/plan, `--no-monitoring` only for standup (matching upstream
  argparse store_true vs both-flags); allowlisted those flags per subcommand (DATA-only `security/allowlist.yaml`);
  added a read-only `_probe_prometheus_crds` probe (`app/tools/probe.py`, key `prometheus_crds`) that reports
  PodMonitor/ServiceMonitor CRD presence so the on/off + CRD opt-out JUDGMENT lives in `knowledge/observability.md`
  + `knowledge/results_interpretation.md`, not Python. Phase 35 (standup PodMonitor/ServiceMonitor + EPP verbosity)
  folded in as a sub-deliverable. Unblocks Phase 49 (trend-store consumer). Merged into `feature/roadmap-v4`
  (`feature/roadmap-v4-p27-monitoring-activate`). Suite **692 passed / 20 skipped** (+26 from the 666 baseline;
  new `tests/test_monitoring_activate.py`); ruff + mypy clean. — done
- 2026-06-02 — Phase 26: llm-d-inference-sim integration tests (opt-in). Proposal §5.3/§7 integration
  layer (`tests/integration/`) drives a sim-shaped BR v0.2 fixture through real `analyze_results`/
  `compare_reports`; a live sim test is opt-in (`LLMD_SIM_INTEGRATION=1`) and skips cleanly otherwise;
  non-gating CI job + `test_quality_gates` opt-in assertion; `knowledge/sim_integration.md`,
  `docs/VALIDATION.md`. Branch `feature/roadmap-v3-p26-sim-integration`. Suite **591/9 skipped**. — done
- 2026-06-02 — Phase 25: Analyzer metric completeness. `summarize_report`/`analysis.py` parse + surface
  §3.4 standard serving metrics (KV-cache hit rate, schedule delay, GPU util) from BR v0.2 or harness
  output; candidate field names are DATA in `knowledge/standard_metrics.yaml`; absent ⇒ `None` (never
  fabricated); INFORMATIONAL Pareto objectives kept out of dominance. Branch
  `feature/roadmap-v3-p25-analyzer-metrics`. Suite **509/7 skipped**. — done
- 2026-06-02 — Phase 24: Endpoint health-check before submit (+ optional auto-standup). New read-only
  `check_endpoint_readiness` tool over `kubectl get endpoints` (+ `run --list-endpoints` corroboration);
  `orchestrate_benchmark_run` gates on it by default, unready ⇒ no mutation + approval-gated standup
  suggestion. Branch `feature/roadmap-v3-p24-health-check`. Suite **584/7 skipped**. — done
- 2026-06-02 — Phase 23: Resource management (node affinity / GPU selection / anti-starvation). Optional
  `Scheduling` value object on `JobSpec`/`build_job_manifest` (node_selector, tolerations, affinity, pod
  anti-affinity from `avoid_labels`); unset ⇒ byte-for-byte baseline manifest; WHICH/WHERE judgment is
  DATA in `knowledge/resource_management.md`. Branch `feature/roadmap-v3-p23-resource-mgmt`. Suite
  **556/7 skipped**. — done
- 2026-06-02 — Phase 22: DOE checkpoint/resume for long sweeps. Cluster-backed checkpoint/resume for
  `run_sweep` (proposal §3.3/§4) via per-sweep ConfigMap (`app/orchestrator/checkpoint.py`); re-invoking
  with the same `sweep_id` resumes idempotently, skipping COMPLETED treatments; no `sweep_id` ⇒ original
  stateless behavior. Branch `feature/roadmap-v3-p22-checkpoint`. Suite **568/7 skipped**. — done
- 2026-06-02 — Phase 21: Real-time benchmark-pod log streaming. `kube.stream_logs(follow=True)` wired into
  the orchestrator run loop via optional `on_log_line` sink; `orchestrate_benchmark_run` surfaces each line
  as an `output` event (live progress); best-effort (a failing tail never breaks the run). Branch
  `feature/roadmap-v3-p21-log-stream`. Suite **519/7 skipped**. — done
- 2026-06-02 — Phase 20: Well-lit-path advisor. `knowledge/welllit_path_advisor.yaml` maps workload shape →
  llm-d well-lit-path scenario (prefix-heavy→precise-prefix-cache-routing, long-context RAG→pd-disaggregation,
  high-throughput→optimized-baseline, agentic→agentic-tests, default→cicd/kind); wired into CORE_KNOWLEDGE +
  `read_knowledge`; `deploy_path_playbook.md` references it. Branch `feature/roadmap-v3-p20-welllit-advisor`.
  Suite **491/7 skipped**. — done
- 2026-06-02 — Phase 19: DOE experiment-file generator + token-characteristics elicitation. `generate_doe_experiment`
  tool (`app/tools/doe.py`) over pure mechanism in `app/validation/doe.py`: cross-products factors × levels into
  a treatments matrix, emits + structurally validates an experiment YAML (validated live against the benchmark
  example format). WHICH factors live in `knowledge/sweep_playbook.md`. Branch `feature/roadmap-v3-p19-doe-gen`.
  Suite **477/7 skipped**. — done
- 2026-06-02 — Phase 18: Workspace lifecycle — retention/GC + startup self-check. `app/storage/retention.py`
  config-driven GC over scratch areas (policy as DATA in `config.py`, unlimited by default); lifespan runs a
  one-shot startup GC that never prunes a live session; structured `self_check(settings)` surfaced via new
  `/readyz` (200/503); liveness stays on `/healthz`. Branch `feature/roadmap-v2-p18-workspace`. Suite
  **404/6 skipped**. — done
- 2026-06-02 — Phase 17: Operability docs + alert rules. Added `docs/SECURITY.md`, `docs/TROUBLESHOOTING.md`,
  `docs/CONTRIBUTING.md`, `docs/CHANGELOG.md` (linked from `docs/README.md`) + `deploy/observability/alerts.rules.yaml`
  (5 alert rules over exported metrics); `tests/test_ops_docs.py` verifies sections + that every referenced metric
  is actually exported. Docs + data only. Branch `feature/roadmap-v2-p17-ops-docs`. Suite **424/7 skipped**. — done
- 2026-06-02 — Phase 16: Run lifecycle & readiness. `app/agent/lifecycle.py` (`RunRegistry`) + `cancel_run` tool
  that cancels a DIFFERENT session's run (frees the concurrency-cap slot, reaps the child process group, no orphaned
  Job); SIGTERM graceful-shutdown cancels every in-flight run; `cancelled` event + `runner_ok` on `/readyz`; WHEN-to-
  cancel judgment in `knowledge/run_lifecycle.md`. Branch `feature/roadmap-v2-p16-run-lifecycle`. Suite
  **415/6 skipped**. — done
- 2026-06-02 — Phase 15: WebSocket protocol hardening + live event buffer. `/ws` validates every inbound frame
  against a Pydantic tagged union (`ws_schemas.py`, `extra="forbid"`); malformed frame ⇒ structured `protocol_error`
  with the socket kept alive; bounded per-turn live ring buffer replays missed live stream on mid-turn reconnect
  (handshake frames excluded). Branch `feature/roadmap-v2-p15-ws-protocol`. Suite **384/6 skipped**. — done
- 2026-06-02 — Phase 14: Quality gates — ruff + mypy + coverage. Enforced `ruff check`, `mypy app` (strict), and a
  coverage-gated suite (`--cov-fail-under=85`) via Makefile + pyproject + CI; cleaned types/lint to green (no behavior
  change); `tests/test_quality_gates.py` locks config/thresholds/CI. Coverage **88.90%**. Branch
  `feature/roadmap-v2-p14-quality-gates`. Suite **432/7 skipped**. — done
- 2026-06-02 — Phase 13: Allowlist governance — per-command timeouts + quotas. Execution limits moved out of Python
  into `security/allowlist.yaml` as data (optional `timeout_s` + `quota {per_session, per_day}`, schema-validated at
  startup, ridden on `Decision`); runner sources its deadline from `Decision.timeout_s` (removed parallel `_TIMEOUTS`);
  new `app/security/quota.py` refuses over-quota commands before execution; judgment in `knowledge/governance.md`.
  Branch `feature/roadmap-v2-p13-allowlist-gov`. Suite **378/6 skipped**. — done
- 2026-06-02 — Phase 12: API trust — auth + rate-limit + CORS. Stdlib-only, all defaulting OFF/open — optional Bearer
  auth (`secrets.compare_digest`, guards HTTP routes + `/ws` handshake → 401/WS 1008) + `TokenBucket`/`RateLimiter`
  (injectable clock) throttling `/api/*` (`/healthz`+`/metrics` exempt); `CORSMiddleware` only when `CORS_ALLOW_ORIGINS`
  set; fails loud if `AUTH_ENABLED` with empty `AUTH_TOKEN`; judgment in `knowledge/api_trust.md`. Branch
  `feature/roadmap-v2-p12-authz`. Suite **351/6 skipped**. — done
- 2026-06-02 — Phase 11: Structured logging + correlation IDs. Stdlib-only JSON logging (`app/observability/logging.py`
  + `logctx.py` contextvars carrier for `corr_id`/`session_id`/`run_id`/`tool`); a fresh `corr_id` minted at the WS
  handshake propagates into the loop, every tool dispatch, and the runner — trace one turn by `corr_id`, one chat by
  `session_id`; secrets never logged (exec record carries argv[0] only); `LOG_LEVEL`/`LOG_FORMAT` added; judgment in
  `knowledge/logging.md`. Branch `feature/roadmap-v2-p11-logging`. Suite **339/6 skipped**. — done
- 2026-06-01 — Phase 10: Multi-harness orchestration in one session. New read-only `compare_harness_runs` tool
  (`app/tools/multiharness.py`) over pure `compare_across_harnesses()`: groups runs by harness, reports which metrics
  ≥2 harnesses both measured vs only one, side-by-side per-harness values with NO cross-harness winner, refuses unless
  ≥2 distinct harnesses; `summarize_report` surfaces producing harness + load point; judgment in
  `knowledge/multi_harness.md`. (Registry was 17 tools at this point.) Branch `feature/roadmap-p10-multiharness`
  (`60546f4`). Suite **329/6 skipped**. — done
- 2026-06-01 — Phase 9: Documentation suite + upstream-PR readiness. Added the `docs/` suite (`ARCHITECTURE.md`,
  `API.md`, `DEPLOYMENT.md`, `USER_GUIDE.md`, `docs/README.md` index) + refreshed root `README.md`/`CLAUDE.md`/`plan.md`.
  Docs-only. Branch `feature/roadmap-p9-docs`. Suite **329/6 skipped**. — done
- 2026-06-01 — Phase 8: Packaging — container image + Helm/Kustomize one-command deploy. Hardened non-root,
  read-only-rootfs multi-stage `Dockerfile` (+`.dockerignore`, pinned kubectl); Helm chart + Kustomize base/overlay
  rendering Deployment/Service/ServiceAccount + namespaced least-privilege Role/RoleBinding (the exact kubectl verbs
  RealKubeClient uses — resolves the Phase-3 RBAC deferral); `orchestrate_benchmark_run` threads
  `orchestrator_service_account`; judgment in `knowledge/packaging.md`. Branch `feature/roadmap-p8-packaging`
  (`742e20d`). Suite **315/6 skipped**. — done
- 2026-06-01 — Phase 7: Observability — Prometheus /metrics + instrumentation + live run metrics. Dependency-free
  metrics registry (`app/observability/metrics.py`, Prometheus text format) + instrumentation hooks wired through
  `ToolContext` and the orchestrator; `GET /metrics` scrape endpoint; new read-only `observe_run_metrics` tool reads
  LIVE cluster CPU/mem via `kubectl top` (distinct from the agent's own `/metrics`); ops assets under
  `deploy/observability/`; judgment in `knowledge/observability.md`. Branch `feature/roadmap-p7-observability`. Suite
  **269/6 skipped**. — done
- 2026-06-01 — Phase 6: Configuration Explorer / Capacity Planner pre-flight. Read-only `check_capacity` tool
  (`app/tools/capacity.py`) at the plan gate ("will this fit?" before a ~10-min standup OOMs) — renders the scenario
  over repo defaults + conversation overrides and runs the BENCHMARK REPO's own capacity planner via a vetted
  `scripts/capacity_check.py` bridge (weights + activation + KV-cache vs accelerator memory + HF config lookup);
  feasibility math in `app/capacity/planner.py`, verdict judgment in `knowledge/capacity.md`; `enforce=True` ⇒ ERRORs
  else WARNINGs. Branch `feature/roadmap-p6-capacity`. Suite **245/6 skipped**. — done
- 2026-06-01 — Phase 5: Historical result storage + trends UI. Cross-session history store (`app/storage/history.py`)
  + single `result_history` tool (store/list/get/trend/delete) persisting a validated report's summary, browsing
  newest-first, and reading a time-series for one metric across results; new `knowledge/history.md`; UI results-browser/
  trends view. (Landed last of the stalled-run branches, out of numeric order.) Branch `feature/roadmap-p5-storage`
  (`60d356d`). Suite **297/6 skipped**. — done
- 2026-06-01 — Phase 4: Results Analyzer (goodput, SLO filtering, Pareto/DoE). Read-only `analyze_results` tool
  (`app/tools/analyze.py`) + pure math in `app/validation/analysis.py` (`SLOTargets`, `evaluate_slo`, `pareto_analysis`):
  schema-validates each BR v0.2 report (never scrapes logs), per-run SLO verdict over the full percentile ladder + an
  honest goodput estimate, and for a sweep the Pareto-optimal + SLO-feasible frontier; `SessionPlan` gained optional
  `slo` targets; judgment in new `knowledge/analysis.md`. Branch `feature/roadmap-p4-analyzer`. Suite
  **219/6 skipped**. — done
- 2026-06-01 — Phase 3: Kubernetes-native Benchmark Orchestrator (the 40% centerpiece). `app/orchestrator/`
  (kube.py/job.py/controller.py/faults.py) + `orchestrate_benchmark_run`: RealKubeClient over ToolContext + Fake for
  tests (allowlisted kubectl apply/logs/delete-job with value constraints, workspace-confined `-f`), Job manifest
  (backoffLimit:0, deadline, labels), submit/watch/logs/reconstruct, fault classification (6 kinds) + retry/dead-letter,
  concurrency-capped parallel sweep + cleanup; judgment in `knowledge/orchestrator.md`. 3-agent adversarial review
  fixed real bugs (watch() busy-loop, sweep gather swallowing a raising treatment, classify mapping, retry collapse) +
  hardening (manifest_path `..` ban, DNS-1123 Job name, pod securityContext). In-cluster image + RBAC deferred to
  Phase 8; tested hermetically (FakeKubeClient + CaptureRunner). Branch `feature/roadmap-p3-orchestrator`. Suite
  **190/6 skipped**. — done
- 2026-06-01 — Phase 2: Parallel sessions & parallel benchmark runs. Concurrency cap (`config.max_concurrent_runs`,
  default 2) as a shared `asyncio.Semaphore` wrapping only MUTATING executions (read-only probes uncapped);
  background-safe runs (an in-flight turn detaches on disconnect and finishes server-side, replayed via Phase-1 history;
  a `connected` gate auto-rejects post-disconnect approvals; a 2nd connection's concurrent turn is rejected). Adversarial
  review fixed a real runner hang (`proc.wait()` after the stdout-pump timeout) via `wait_for(gather(pump, wait))` +
  SIGKILL of the process group. Deferred to Phase 3: abandoned runs hold a slot until timeout; reconnecting clients see
  only the end result. Branch `feature/roadmap-p2-parallel`. Suite **127/6 skipped**. — done
- 2026-06-01 — Phase 1: Command transparency, debug mode, UI polish. A `command` event for every executed command
  (centralized in `ToolContext._emit_command`): read-only probes now announce themselves, mutating commands announce
  only after approval; bounded (500) `Session.commands` trail persisted + replayed on resume; UI inline `$ cmd` lines +
  global "Executed commands" log (read-only/mutating + auto/approved badges) + a Debug toggle. Slider audit: no sliders
  exist; deliberately did NOT invent parameter sliders (would embed judgment in the UI). 3-agent adversarial review
  fixed FOUC + added aria-live, flow-level command parity asserts, harness command recording. Branch
  `feature/roadmap-p1-transparency`. Suite **119/6 skipped**. — done
- 2026-06-01 — Phase 0: Autonomous scaffolding. Created integration worktree on `feature/roadmap` off `main`
  (`04c06fe`); fresh `.venv` (uv, py3.11, `-e ".[dev]"`); `.env` carried over with `REPOS_DIR` so app + tests see the
  real sibling repos. conftest portability fix: `BENCH_REPO` resolves via `get_settings().bench_repo` (honors
  `REPOS_DIR`/`.env`) instead of a hardcoded path — converts ~12 sibling-repo tests FAIL→PASS in a worktree, backward-
  compatible. Wrote `ROADMAP.md` (10 phases) + this `PROGRESS.md`. The one extra skip vs the primary checkout is
  `test_snapshot_matches_live` (catalog-drift guard, skips off the canonical sibling path — expected). Suite
  **110/6 skipped**. — done
