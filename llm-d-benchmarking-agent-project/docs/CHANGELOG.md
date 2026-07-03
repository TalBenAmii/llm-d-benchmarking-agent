# Changelog

All notable changes to the **llm-d Benchmarking Agent** are documented here. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Versions correspond to the
phased build-out, now summarized in [`FEATURES.md`](../FEATURES.md) (the live feature
inventory), whose DEFERRED phases track the remaining work.

## [Unreleased] — v4: benchmark/deploy-coverage gaps + UX (Roadmap v4)

Post-v3 work tracked live in [`FEATURES.md`](../FEATURES.md);
the only outstanding roadmap items are the 7 DEFERRED phases. The agent tool surface has grown from
22 to **36 tools** (`app/tools/registry.py` is authoritative). Landed since v3 (representative, not
exhaustive — see `FEATURES.md` for the evidence-backed inventory): interactive spec + workload
co-creation, proactive metrics-server pre-flight + pre-run install offer, one-command publish of a
chat to a public link, a single-GPU-cluster runbook, and assorted UI/observability refinements.

### Added
- **Harness launcher MEMORY sizing flag** — `harness_mem` on `execute_llmdbenchmark` sets the
  backend-only env var `LLMDBENCH_HARNESS_CPU_MEM` (default `32Gi`), a Kubernetes-quantity value
  validated at the tool boundary so a typo is a clean error, not a late pod-apply failure
  (`app/tools/execute.py`). It rides alongside the existing CPU knob; the WHEN/how-much judgment is
  data in `knowledge/harness_sizing.md` (raise on launcher OOM, lower on a tiny node).
- **`read_knowledge` section addressing + truncation UX** — a `section=` arg returns one named
  markdown section verbatim, and a whole-guide read that overflows the tool-result feed-back budget
  is annotated with the `dropped_sections` (the headings past the clamp's cut) + a re-fetch note, so
  a large guide's later sections never vanish silently (`app/tools/knowledge_access.py`,
  `app/tools/schemas/docs.py`).

### Changed
- **Knowledge audit + splits** — `observability.md` and `sweep_playbook.md` slimmed to hub files
  that point at focused on-demand guides (9 new ones, incl. `observability_monitoring.md`,
  `router_features.md`, and the `sweep_*` set), with ~25 fact fixes across the corpus so each
  judgment lives in exactly one canonical place.

### Fixed
- **DoE list-indexed override keys are now rejected with the WHY** — a numeric dotted segment (e.g.
  `load.stages.0.rate`) indexes a LIST element, which upstream `apply_overrides` cannot apply (it
  walks dicts only, so the override never lands and no-ops at runtime); the rejection now names that
  cause so a caller doesn't hand-edit around a guard that is protecting them (`app/validation/doe.py`).

## [0.3.0] — v3: proposal-completion features + token-tracking (Phases 19-26)

Developed on the `feature/roadmap-v3` integration branch and merged into `main`. This wave closes
the remaining proposal-coverage gaps (DOE generation, the well-lit-path advisor, live log
streaming, checkpoint/resume, resource management, endpoint health-check, analyzer metric
completeness, and an opt-in inference-sim integration layer), then adds token accounting + prompt
caching. The agent tool surface grows from 18 to **22 tools**.

### Added
- **DOE experiment-file generator + token-characteristics elicitation** (Phase 19). A
  `generate_doe_experiment` tool (`app/tools/doe.py`) cross-products agent-chosen *factors ×
  levels* into the full treatments matrix, emits a valid experiment YAML into the session
  workspace, and validates it structurally against the repo's experiment-example format (read
  live). *Which* factors/levels to sweep is judgment in an expanded `knowledge/sweep_playbook.md`,
  which now also elicits token characteristics / prefix-reuse ratio.
- **Well-lit-path advisor** (Phase 20). `knowledge/welllit_path_advisor.yaml` maps a workload
  *shape* → the llm-d well-lit-path scenario worth benchmarking (prefix-heavy chat →
  precise-prefix-cache-routing, long-context RAG → pd-disaggregation, high-throughput →
  optimized-baseline, …) with the selecting signals + a `deploy_path` reach flag; inlined into the
  system prompt and served via `read_knowledge`. Judgment is data, not code.
- **Real-time benchmark-pod log streaming** (Phase 21). `kube.stream_logs(follow=True)` is wired
  into the orchestrator run loop so a running benchmark Job's log lines surface live as `output`
  events (same transport as streamed command output) — progress during the run, not just at the
  end. Best-effort: a failing tail never breaks the run.
- **DOE checkpoint/resume for long sweeps** (Phase 22). `run_sweep(sweep_id, namespace)` persists
  each treatment's completed/in-flight state + outcome to a per-sweep **ConfigMap** (the cluster
  source of truth, consistent with the stateless design — `app/orchestrator/checkpoint.py`). On
  resume, completed treatments are skipped (their outcome reconstructed so the merged result still
  covers all N) and the sweep continues idempotently; no `sweep_id` ⇒ original stateless behavior.
- **Resource management: node affinity / GPU selection / anti-starvation** (Phase 23). An optional
  `Scheduling` value object on `JobSpec`/`build_job_manifest` lets a benchmark Job request hardware
  (GPU resource/count, GPU-type node label) and be placed so it doesn't starve the measured stack
  (`node_selector`/`tolerations`/raw `affinity` + pod anti-affinity from `avoid_labels`). Unset ⇒
  byte-for-byte the cpu/memory baseline. The WHICH/WHERE judgment is data in
  `knowledge/resource_management.md`.
- **Endpoint health-check before submit** (Phase 24). `app/orchestrator/readiness.py` reads
  `kubectl get endpoints` (corroborated by `run --list-endpoints`) for a ready backing endpoint;
  the read-only `check_endpoint_readiness` tool exposes it, and `orchestrate_benchmark_run` gates on
  it by default (`require_ready_endpoint=true`) — an unready stack submits nothing and returns an
  approval-gated standup *suggestion* (never auto-run).
- **Analyzer metric completeness** (Phase 25). `summarize_report`/`analysis` now parse and surface
  the §3.4 standard serving metrics that were previously ignored — KV-cache hit rate, schedule
  delay, GPU utilization — from either the BR v0.2 standardized shape or harness-native output,
  with field-name discovery as data in `knowledge/standard_metrics.yaml`; absent metrics degrade to
  `None` (never fabricated) and stay informational (kept out of Pareto dominance).
- **llm-d-inference-sim integration tests (opt-in)** (Phase 26). A `tests/integration/` layer that
  exercises the analyze/compare path against the CPU-only mock server without making the default
  suite non-hermetic: an always-running check drives a sim-shaped BR v0.2 fixture through the real
  tools, and a live test stands up a real sim **only** when `LLMD_SIM_INTEGRATION=1` and the sim is
  locatable (else skips cleanly, never hangs). Plus a non-gating CI job.
- **Token-usage counter + provider-agnostic prompt caching** (token-tracking merge). A real
  provider token counter surfaced in the chat UI (header `Σ N tokens` chip + per-turn
  `↑up ↓down · N this turn (X calls · Y cached)`), provider-agnostic prompt caching wired through
  `app/llm/*`, and a system-prompt shrink (~20.4K → ~12.3K fixed overhead; schema `title`s
  stripped).

### Security
- No new runtime dependency in any v3 phase. The new orchestrator surfaces (`cm/configmaps`,
  `ep/endpoints`) ride the existing read-only allowlisted `kubectl` enum; the inference-sim live
  test is env-gated and off by default.

## [0.2.0] — v2: production operability, trust & quality (Phases 11-18)

Developed on the `feature/roadmap-v2` integration branch and merged into `main`; each phase lands
after its full hermetic-suite gate is green. This work hardens the v1 agent for operation without
changing its core behavior.

### Added
- **Structured logging + correlation IDs** (Phase 11). Stdlib-only JSON logging (one object per
  line) with a per-turn `corr_id` minted at the WebSocket boundary and propagated via
  `contextvars` into the agent loop, every tool dispatch, and the command runner — so one turn
  is traceable end-to-end by grepping its `corr_id`, one chat by `session_id`, one run by
  `run_id`. `LOG_LEVEL` / `LOG_FORMAT` (json default, text for dev) added to config. Secrets are
  never logged (`exe` = `argv[0]` only). Judgment in `knowledge/logging.md`.
- **API trust: auth + rate-limit + CORS** (Phase 12). Stdlib-only, **default-off/open** controls
  so the FastAPI surface is safe to expose while staying frictionless locally: optional Bearer
  auth (constant-time compare; guards every HTTP route and the `/ws` handshake — 401 / WS close
  1008), an in-memory token-bucket rate limiter on `/api/*` (empty bucket → 429; `/healthz` +
  `/metrics` never throttled), and `CORSMiddleware` wired only when `CORS_ALLOW_ORIGINS` is set.
  Judgment in `knowledge/api_trust.md`.
- **Allowlist governance: per-command timeouts + quotas** (Phase 13). Execution limits moved
  into `security/allowlist.yaml` as DATA — optional `timeout_s` and `quota {per_session,
  per_day}` per executable/subcommand, schema-validated at startup. An over-quota command is
  refused *before* execution. Judgment in `knowledge/governance.md`.
- **WebSocket protocol hardening + live event buffer** (Phase 15). Every inbound `/ws` frame is
  validated against an explicit Pydantic tagged union (`user_message`/`approval`/`ping`,
  `extra="forbid"`); a malformed frame yields a structured `protocol_error` and the socket stays
  alive. A bounded per-turn live ring buffer lets a client reconnecting mid-turn replay the
  missed stream and continue live.
- **Operability docs + Prometheus alert rules** (Phase 17). `docs/SECURITY.md` (threat model:
  trust boundaries, the allowlist/approval model, secret scrubbing, network-exposure guidance),
  `docs/TROUBLESHOOTING.md` (symptom → what to check, debug mode, which logs to read),
  `docs/CONTRIBUTING.md` (how to add a tool/flow/phase; the two laws; the hermetic-test rule),
  and this changelog. Ships `deploy/observability/alerts.rules.yaml` — Prometheus alert rules
  over the existing metrics (slow commands, elevated run-failure / fault rates, stuck in-flight
  runs, target down).
- **Workspace lifecycle: retention/GC + startup self-check** (Phase 18). A config-driven
  retention/GC over the workspace scratch areas (policy as DATA in `config.py`, unlimited by
  default; walk/counter as mechanism), run once at startup and never pruning a live session. A
  structured startup `self_check` (workspace writable, provider coherent, repos resolvable, auth
  coherent) surfaced via a new `/readyz` readiness probe (200/503 + reasons); liveness stays on
  `/healthz`.

### Changed
- The command runner sources its per-command deadline from the allowlist `Decision.timeout_s`,
  removing the parallel `_TIMEOUTS` table in `app/tools/execute.py` (one mechanism, not two).

### Security
- No new runtime dependency in any v2 phase; all controls are stdlib + FastAPI/Starlette.
- All new trust controls (auth/rate-limit/CORS) default OFF/open so existing local flows and the
  test suite are unchanged; turn them on when binding beyond `localhost`.

## [0.1.0] — v1: the conversational benchmarking agent (Phases 0-10)

The first complete vertical: a local chat-based assistant that drives `llm-d-benchmark` for
non-experts, plus a Kubernetes-native orchestrator and analysis suite. Obeys thin-code /
thick-agent and a deny-by-default security model throughout.

### Added
- **MVP quickstart vertical** (Phase 0-1). Drive the `llm-d-benchmark` quickstart end-to-end on
  a local kind cluster (probe → ensure repo → `install.sh` → `standup --spec cicd/kind` →
  smoketest → `run` → parse the Benchmark Report v0.2 → summarize → offer teardown). FastAPI
  backend + static chat UI; the deny-by-default allowlist + per-action approval; a `command`
  event for every executed command, a persisted command trail, and a UI Debug mode.
- **Parallel sessions & parallel benchmark runs** (Phase 2). A shared concurrency cap on
  mutating executions (`max_concurrent_runs`); background-safe runs that survive a disconnect and
  replay on reconnect.
- **Kubernetes-native Benchmark Orchestrator** (Phase 3) — the centerpiece. Job lifecycle over
  allowlisted `kubectl` (`KubeClient` real + `FakeKubeClient` for tests), fault classification
  (6 kinds), retry / dead-letter, and concurrency-capped parallel sweeps.
- **Results Analyzer** (Phase 4). Read-only `analyze_results`: SLO evaluation over the full
  percentile ladder, an honest goodput estimate, and Pareto / SLO-feasible-frontier analysis
  over a DoE sweep. `SessionPlan` captures optional SLO targets.
- **Historical result storage + trends UI** (Phase 5). A cross-session history store and a
  `result_history` tool (store/list/get/trend/delete) with a results-browser / trends view.
- **Capacity Planner pre-flight** (Phase 6). Read-only `check_capacity` runs the benchmark
  repo's own planner (weights + activation + KV-cache vs accelerator memory) before a ~10-minute
  standup fails opaquely.
- **Observability** (Phase 7). A dependency-free Prometheus `/metrics` endpoint and
  instrumentation (commands, command durations, orchestrator run lifecycle/faults/in-flight),
  a `observe_run_metrics` tool reading live `kubectl top`, and ops assets under
  `deploy/observability/` (Grafana dashboard + scrape config).
- **Packaging** (Phase 8). A hardened non-root, read-only-rootfs, multi-stage `Dockerfile` and a
  Helm chart / Kustomize base with **least-privilege namespaced RBAC** (exactly the kubectl verbs
  the orchestrator uses — no `*`, no secrets/exec, no cluster scope).
- **Documentation suite** (Phase 9). `docs/` — `ARCHITECTURE.md`, `API.md`, `DEPLOYMENT.md`,
  `USER_GUIDE.md`, `VALIDATION.md` — toward upstream-PR readiness.
- **Multi-harness orchestration in one session** (Phase 10). A read-only `compare_harness_runs`
  tool that contrasts inference-perf (SLO/latency) and guidellm (throughput) against the same
  stack, cross-validating only metrics ≥2 harnesses both measured.

### Security
- All commands run as argv lists with `shell=False`; read-only probes auto-run, mutating
  commands require explicit UI approval; the policy is data (`security/allowlist.yaml`).
- Secrets (LLM keys, HF token) stay in the backend env; the browser never receives them and the
  subprocess environment is scrubbed of them.
- The two sibling repos (`llm-d/`, `llm-d-benchmark/`) are read-only — read live, never vendored
  or modified.

[Unreleased]: https://github.com/TalBenAmii/llm-d-benchmarking-agent
[0.3.0]: https://github.com/TalBenAmii/llm-d-benchmarking-agent
[0.2.0]: https://github.com/TalBenAmii/llm-d-benchmarking-agent
[0.1.0]: https://github.com/TalBenAmii/llm-d-benchmarking-agent
