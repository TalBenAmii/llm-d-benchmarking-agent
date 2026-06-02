# Changelog

All notable changes to the **llm-d Benchmarking Agent** are documented here. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Versions correspond to the
phased build-out tracked in [`ROADMAP.md`](../ROADMAP.md) and [`PROGRESS.md`](../PROGRESS.md).

## [Unreleased] — v2: production operability, trust & quality (Phases 11-18)

Developed on the `feature/roadmap-v2` integration branch (never `main`); each phase lands after
its full hermetic-suite gate is green. This work hardens the v1 agent for operation without
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
[0.1.0]: https://github.com/TalBenAmii/llm-d-benchmarking-agent
