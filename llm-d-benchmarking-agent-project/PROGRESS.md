# PROGRESS LOG

Reverse-chronological log of the autonomous roadmap effort. One entry per work session /
phase milestone. See [`ROADMAP.md`](ROADMAP.md) for the plan and phase status.

Branch: `feature/roadmap` (integration; never merged to `main` during this effort).
Test baseline at start (primary checkout `main` @ `04c06fe`): **111 passed / 5 skipped**.

---

## 2026-06-02 — Phase 16: Run lifecycle & readiness — DONE
Branch `feature/roadmap-v2-p16-run-lifecycle` → merged into `feature/roadmap-v2` (`--no-ff`).
- **Shipped:** `app/agent/lifecycle.py` (a `RunRegistry` tracking each session's in-flight turn
  task) + a `cancel_run` tool (`app/tools/cancel.py`) that cancels a DIFFERENT session's run from
  outside itself — `asyncio` unwinds the concurrency-cap semaphore (freeing the slot) and the
  runner reaps the child process group on `CancelledError` (no orphaned K8s Job / subprocess). The
  `lifespan` installs a SIGTERM graceful-shutdown cancelling every in-flight run, composed into one
  coherent startup/shutdown with Phase 18's self-check + retention GC. A `cancelled` event + cancel
  control message + a `runner_ok` component on `/readyz` round out the surface; `knowledge/run_lifecycle.md`
  carries the JUDGMENT of *when* to cancel (thick agent / thin code).
- **Merge note:** the v2 hot files (`app/main.py` lifespan, `session.py`, `ws_schemas.py`,
  `runner.py`, `retention.py`, `registry.py`/`schemas.py`) auto-merged cleanly under `ort`; verified
  the result is a deliberate union (lifespan runs BOTH Phase 16 shutdown AND Phase 18 GC; `cancel_run`
  registered alongside all prior tools) rather than a blind concatenation. No conflict markers.
- **Tests:** worktree full suite **415 passed / 6 skipped / 0 failed** (15.6s, no hang;
  +`tests/test_run_lifecycle.py`, 384 lines; prior baseline 404 passed / 6 skipped).

## 2026-06-02 — Phase 18: Workspace lifecycle — retention/GC + startup self-check — DONE
Branch `feature/roadmap-v2-p18-workspace` → merged into `feature/roadmap-v2` (`--no-ff`).
- **Shipped:** `app/storage/retention.py` (446 lines) — a config-driven retention/GC mechanism
  over the workspace scratch areas (sessions, history, orchestrator `workspace/jobs/*.yaml`), with
  the policy as DATA in `config.py` (`retention_max_age_days` / `retention_max_items` /
  `retention_max_bytes`, all unlimited by default) and the walk/counter as the mechanism. The
  FastAPI `lifespan` runs a one-shot GC at startup (toggle `retention_gc_on_startup`, defaults ON,
  never blocks startup) that is fed `_active_session_ids(app)` so a live/running session is never
  pruned. Also added a structured startup `self_check(settings)` (workspace writable, provider
  coherent, repos resolvable, auth coherent) surfaced through a new `/readyz` readiness probe
  (200 ready / 503 not, with the structured reasons); liveness stays on `/healthz`.
- **Merge composition:** the structural-wiring file `app/main.py` was reconciled into ONE coherent
  `lifespan` running BOTH the prior wiring (logging/auth/rate-limit/allowlist/runner/sessions/
  provider) and the new self-check + GC; `session.py` gained `active_ids()`; `config.py` got the
  Phase 18 settings. No entries dropped. Other files were clean additions.
- **Tests:** full integration suite **404 passed / 6 skipped / 0 failed** (+`tests/test_retention.py`
  + `tests/test_readyz.py`, 20 new hermetic tests; prior baseline 384 passed / 6 skipped).
  Authoritative run from the integration worktree (worktree app on `PYTHONPATH`, real venv + .env,
  `REPOS_DIR` set, 600s timeout — exit 0, no hang).

## 2026-06-02 — Phase 15: WebSocket protocol hardening + live event buffer — DONE
Branch `feature/roadmap-v2-p15-ws-protocol` → merged into `feature/roadmap-v2` (`--no-ff`). The
phase branched off the current tip, so the merge applied with no code conflicts.
- **Shipped:** the `/ws` boundary now validates every *inbound* frame against an explicit Pydantic
  tagged union (`app/agent/ws_schemas.py` — `user_message`/`approval`/`ping`, `extra="forbid"`);
  a malformed/non-dict frame is rejected with a structured `error` event of `kind="protocol_error"`
  and the socket is KEPT ALIVE (no silent no-op / handler crash). The `Channel` gained a BOUNDED
  per-turn live ring buffer (`deque(maxlen=...)`): each emitted turn event is fanned out to the live
  socket AND appended, so a client reconnecting mid-turn replays the missed live stream and then
  continues live. Connection-lifecycle frames (`ready`/`history`/`pong`) are excluded from the
  buffer so a second reconnect doesn't replay stale handshake frames. Outbound serialization unified
  through `outbound()`; `ping` is answered with a `pong` event.
- **Tests:** full integration suite **384 passed / 6 skipped / 0 failed** (+`tests/test_ws.py`,
  280 lines; prior baseline 378 passed / 6 skipped). Authoritative run from the integration worktree
  with `PYTHONPATH=$PWD`, `REPOS_DIR` set, `timeout 600` — no hang (12s, exit 0).

## 2026-06-02 — Phase 13: Allowlist governance — per-command timeouts + quotas — DONE
Branch `feature/roadmap-v2-p13-allowlist-gov` → merged into `feature/roadmap-v2` (`--no-ff`). The
phase branched off the Phase-11 tip; its files (`allowlist.py`, `quota.py`, `context.py`,
`execute.py`, `loop.py`, `allowlist.yaml`) are disjoint from Phase 12's (`auth.py`, `main.py`,
`config.py`), so the merge applied with no code conflicts.
- **Shipped:** execution limits moved out of Python into `security/allowlist.yaml` as data —
  optional `timeout_s` and `quota {per_session, per_day}` on an executable and/or subcommand
  (subcommand overrides executable). `allowlist.py` schema-validates both AT STARTUP (malformed
  allowlist → clear load-time error) and rides the resolved limits on the `Decision`. The runner
  sources its per-command deadline from `Decision.timeout_s`, REMOVING the parallel
  `execute.py::_TIMEOUTS` table (one mechanism, not two; a sane global default remains). New
  `app/security/quota.py` is a pure per-session/per-day usage counter; `ToolContext` refuses an
  over-quota command with a structured `QuotaError` BEFORE execution/approval and the loop relays
  it. Judgment in `knowledge/governance.md`.
- **Tests:** full integration suite **378 passed / 6 skipped / 0 failed** (+27 hermetic governance
  tests; prior baseline 351 passed / 6 skipped). Authoritative run from the integration worktree
  with `PYTHONPATH=$PWD`, `REPOS_DIR` set, `timeout 600` — no hang (12s, exit 0).

## 2026-06-02 — Phase 12: API trust — auth + rate-limit + CORS — DONE
Branch `feature/roadmap-v2-p12-authz` → merged into `feature/roadmap-v2` (`--no-ff`). The phase
branched cleanly off the current v2 tip (single commit), so the merge applied with no conflicts in
the additive-registration or structural-wiring files (`app/main.py`, `app/config.py` took the
phase changes outright over an unchanged base).
- **Shipped:** stdlib-only API-trust controls (NO new dependency), all defaulting OFF/open so
  existing flows and tests are unchanged — `app/security/auth.py`: optional Bearer auth
  (constant-time `secrets.compare_digest`, app-level dependency over a typed `HTTPConnection` that
  guards every HTTP route, with the `/ws` handshake guarded in-handler via `Authorization` header
  or `?token=` → 401 / WS close 1008) and a `TokenBucket`/`RateLimiter` with an injectable
  monotonic clock (deterministic, sleepless tests) throttling the `/api/*` intake (empty bucket →
  429; `/healthz` + `/metrics` never throttled). `app/main.py` wires `CORSMiddleware` only when
  `CORS_ALLOW_ORIGINS` is set and the lifespan builds the shared limiter, failing loud if
  `AUTH_ENABLED` is set with an empty `AUTH_TOKEN`. `app/config.py` adds the settings +
  `cors_origins_list`; `.env.example` documents them. Judgment in `knowledge/api_trust.md`.
- **Tests:** full integration suite **351 passed / 6 skipped / 0 failed** (+12 hermetic api-trust
  tests; prior baseline 339 passed / 6 skipped). Authoritative run from the integration worktree
  with `PYTHONPATH=$PWD`, `REPOS_DIR` set, `timeout 600` — no hang.

## 2026-06-02 — Phase 11: Structured logging + correlation IDs — DONE
Branch `feature/roadmap-v2-p11-logging` → merged into `feature/roadmap-v2` (`--no-ff`). The phase
branched cleanly off the current v2 tip (single commit), so the merge applied with no conflicts in
the additive-registration or structural-wiring files.
- **Shipped:** stdlib-only structured logging — `app/observability/logging.py` (JSON formatter, one
  JSON object per line; a `logging.Filter` that injects the per-turn correlation context) +
  `app/observability/logctx.py` (contextvars carrier for `corr_id`/`session_id`/`run_id`/`tool`). A
  fresh `corr_id` is minted at the WebSocket handshake (one per connection/turn) and bound before
  `create_task`, so it propagates via `contextvars` into the agent loop (`turn.start/end`,
  `tool.call.start/result`), every tool dispatch (`tool=<name>`), and the command runner
  (`command.exec`, `runner.exec.start/timeout/launch_failed`). Trace one turn end-to-end by grepping
  its `corr_id`; one chat by `session_id`. `LOG_LEVEL` (default INFO) + `LOG_FORMAT` (json default /
  text dev) added to `config`; `setup_logging()` wired once in the lifespan. Secrets never logged —
  the exec record carries `exe` (argv[0]) only, never full argv/env. No new runtime dependency;
  judgment in `knowledge/logging.md`.
- **Tests:** worktree suite **339 passed / 6 skipped / 0 failed** (+7 hermetic tests in
  `test_logging.py`: JSON formatter keys + valid JSON, one `corr_id` bound at the WS boundary appears
  on records from the loop + a tool + the runner within one turn via a real read-only `git status -s`,
  the text path, idempotent setup). Run with `REPOS_DIR=/home/tal/kind-quickstart-guide` against the
  worktree venv + .env, 600s timeout — no hang (exit 0). Prior baseline 332 passed / 6 skipped.

## 2026-06-01 — Phase 9: Documentation suite + upstream-PR readiness — DONE
Branch `feature/roadmap-p9-docs` → merged into `feature/roadmap` (`--no-ff`). Docs-only phase: no
conflicts in the shared registration files (no code touched), so the merge applied clean.
- **Shipped:** a `docs/` suite — `ARCHITECTURE.md`, `API.md`, `DEPLOYMENT.md`, `USER_GUIDE.md`, and a
  `docs/README.md` index — plus refreshed root `README.md`, `CLAUDE.md`, and `plan.md` to polish
  toward the upstream `llm-d-benchmark` PR path.
- **Tests:** worktree suite **329 passed / 6 skipped / 0 failed** (against the worktree venv + .env,
  420s timeout — no hang, exit 0). Prior baseline was 315 passed / 6 skipped; docs-only change so the
  delta vs baseline reflects tests already integrated from later phases, not new tests here.

## 2026-06-01 — Phase 10: Multi-harness orchestration in one session — DONE
Branch `feature/roadmap-p10-multiharness` → merged into `feature/roadmap` (`--no-ff`, `60546f4`).
Conflicts in the shared registration files were resolved by keeping BOTH sides' additions:
`app/tools/registry.py` (`history` + `multiharness` imports/registrations) and `tests/test_schemas.py`
(expected tool set kept both `result_history` and `compare_harness_runs`); `app/tools/schemas.py`
auto-merged.
- **Shipped:** a cross-harness comparison path so the agent can recommend + run BOTH inference-perf
  (SLO/latency validation) and guidellm (throughput sweep) against the same stack in one session,
  then contrast them. New read-only `compare_harness_runs` tool (`app/tools/multiharness.py`, schema
  `CompareHarnessRunsInput`) backed by a pure `compare_across_harnesses()` in `validation/report.py`:
  groups runs by detected harness, reports which metrics ≥2 harnesses both measured (cross-validate)
  vs only one did, shows per-harness values side by side with NO cross-harness winner, flags
  multi-model contrasts as not meaningful, and refuses unless ≥2 distinct harnesses are present.
  `summarize_report` now surfaces the producing harness + load point from the report's own
  `scenario.load.standardized.tool`. Judgment in `knowledge/multi_harness.md` (cross-linked from
  `sweep_playbook.md`); registry now exposes 17 tools.
- **Tests:** worktree suite **329 passed / 6 skipped / 0 failed** (+`test_multiharness.py`, 14
  hermetic tests on real BR v0.2 reports on disk + pure math; +1 assert in `test_schemas.py`). Run
  with `REPOS_DIR=/home/tal/kind-quickstart-guide` against the worktree venv + .env, 420s timeout —
  no hang (exit 0). Prior baseline was 315 passed / 6 skipped.

## 2026-06-01 — Phase 8: Packaging — container image + Helm/Kustomize one-command deploy — DONE
Branch `feature/roadmap-p8-packaging` → merged into `feature/roadmap` (`--no-ff`, `742e20d`).
Only conflict was `pyproject.toml`'s `[tool.setuptools] packages` list — resolved by keeping BOTH
sides' additions (`app.storage` from Phase 5 + `app.packaging`). `app/tools/schemas.py` auto-merged.
- **Shipped:** hardened non-root, read-only-rootfs, multi-stage `Dockerfile` (+`.dockerignore`,
  pinned kubectl, no baked-in secrets); a Helm chart (`deploy/helm/llm-d-benchmarking-agent`) and a
  Kustomize base/overlay (`deploy/kustomize`) that each render Deployment + Service + ServiceAccount
  + namespaced least-privilege Role/RoleBinding granting EXACTLY the kubectl verbs RealKubeClient
  uses — resolving the Phase-3 RBAC deferral. `app/packaging` holds the port/path/RBAC contract;
  judgment lives in `knowledge/packaging.md`. `orchestrate_benchmark_run` now threads
  `orchestrator_service_account` so submitted Jobs run under the deploy's SA. LLM/HF keys via K8s
  Secret; `/healthz` probe + `/metrics` scrape annotations.
- **Tests:** worktree suite **315 passed / 6 skipped / 0 failed** (+`test_packaging.py`, 327 lines;
  +SA-wiring test in `test_orchestrator_tool.py`). Run with `REPOS_DIR=/home/tal/kind-quickstart-guide`
  against the worktree venv + .env, 420s timeout — no hang (exit 0). Prior baseline was 297 passed / 6 skipped.

## 2026-06-01 — Phase 5: Historical result storage + trends UI — DONE
Branch `feature/roadmap-p5-storage` → merged into `feature/roadmap` (`--no-ff`, `60d356d`).
Clean merge — no conflicts in the shared registration files (the branch was based on a recent
`feature/roadmap`, so registry/schemas/prompt/allowlist additions merged cleanly).
- **Shipped:** a cross-session history store (`app/storage/history.py`) and a single
  `result_history` tool (`app/tools/history.py`, schema `ResultHistoryInput`) with actions
  store/list/get/trend/delete — persist a validated Benchmark Report's summary, browse stored
  results newest-first, fetch one record, and read a time-series for one metric across results.
  Wired into `main.py`; new `knowledge/history.md`. UI gained a results-browser / trends view
  (`ui/app.js`, `ui/index.html`, `ui/styles.css`).
- **Tests:** worktree suite **297 passed / 6 skipped / 0 failed** (+`test_history.py`, 361 lines;
  +1 assert in `test_schemas.py`). Run with `REPOS_DIR=/home/tal/kind-quickstart-guide` against
  the worktree venv + .env, 420s timeout — no hang (exit 0). Prior baseline was 269 passed / 6 skipped.
- Note: phases landed out of numeric order; Phase 5 was the last of the stalled-run branches to integrate.

## 2026-06-01 — Phase 7: Observability — Prometheus /metrics, instrumentation, live run metrics — DONE
Branch `feature/roadmap-p7-observability` → merged into `feature/roadmap` (`--no-ff`). Conflicts
in the shared registration files (`registry.py`, `schemas.py`, `tests/test_schemas.py`,
`pyproject.toml`) were resolved by keeping BOTH sides' additions — Phase 6's `check_capacity` /
Phase 4's `analyze_results` and Phase 7's `observe_run_metrics` now coexist in the imports,
REGISTRY, schema models, expected tool-name set, and `[tool.setuptools].packages` (which now
lists `app.observability` alongside `app.orchestrator`/`app.capacity`). `allowlist.yaml`
auto-merged cleanly.
- **Shipped:** a hand-rolled, dependency-free metrics registry (`app/observability/metrics.py`)
  with Prometheus text-format exposition; metric definitions + instrumentation hooks in
  `app/observability/instrument.py`, wired through `ToolContext` and the orchestrator
  `controller.py` so tool calls, commands, and orchestrated runs are counted/timed. `app/main.py`
  exposes a `GET /metrics` scrape endpoint. A new read-only `observe_run_metrics` tool
  (`app/tools/observe.py`) reads LIVE cluster CPU/memory via `kubectl top` (pods in a namespace,
  optionally narrowed to one run by run_id or per-container; or nodes) — distinct from `/metrics`
  (the agent's own counters). Ops assets under `deploy/observability/` (Grafana dashboard +
  Prometheus scrape config); interpretation guidance in `knowledge/observability.md`.
- **Allowlist:** `kubectl top` (read-only -> auto-runs, no approval) added for `observe_run_metrics`.
- **Tests:** worktree suite **269 passed / 6 skipped / 0 failed** (+`test_metrics.py`,
  +`test_observability.py`, +updated `test_schemas.py`; authoritative run in the integration
  worktree with the real venv + .env). Prior baseline was 245 passed / 6 skipped.
- Next: Phase 5 (historical result storage + trends UI) / Phase 8 (packaging).

## 2026-06-01 — Phase 6: Configuration Explorer / Capacity Planner pre-flight — DONE
Branch `feature/roadmap-p6-capacity` → merged into `feature/roadmap` (`--no-ff`). Conflicts
in the shared registration files (`registry.py`, `schemas.py`, `tests/test_schemas.py`) were
resolved by keeping BOTH sides' additions — Phase 4's `analyze_results` and Phase 6's
`check_capacity` now coexist in the imports, REGISTRY, schema models, and the expected
tool-name set. `prompt.py` + `allowlist.yaml` auto-merged cleanly.
- **Shipped:** read-only `check_capacity` tool (`app/tools/capacity.py`) called at the plan
  gate (after a SessionPlan is approved, before standup) to answer "will this fit?" before a
  ~10-minute standup fails opaquely with OOM/won't-load. It renders the spec's scenario
  (model/accelerator/parallelism) merged over repo defaults, applies conversation-derived
  `overrides` (bigger model, longer context, a real GPU), and runs the BENCHMARK REPO's OWN
  capacity planner via a vetted `scripts/capacity_check.py` bridge (the planner package lives
  only in that repo's venv) — weights + activation + KV-cache vs accelerator memory arithmetic
  plus a HuggingFace model-config lookup. Pure feasibility math + diagnostic classification
  live in `app/capacity/planner.py`; the verdict-interpretation judgment lives in
  `knowledge/capacity.md` (thin-code/thick-agent). `enforce=True` tags shortfalls as
  deployment-halting ERRORs (the strict read), else advisory WARNINGs.
- **Allowlist:** the `capacity_check.py` bridge is a vetted project script run through the
  allowlisted runner with a workspace-confined JSON request file (read-only -> auto-runs, no
  approval prompt); `runner.py` extended accordingly. It never touches the cluster.
- **Tests:** worktree suite **245 passed / 6 skipped / 0 failed** (+`test_capacity.py`, +
  updated `test_schemas.py`; authoritative run in the integration worktree with the real
  venv + .env). Prior baseline was 219 passed / 6 skipped.
- Next: Phase 5 (historical result storage + trends UI) / Phase 7 (observability).

## 2026-06-01 — Phase 4: Results Analyzer (goodput, SLO filtering, Pareto/DoE) — DONE
Branch `feature/roadmap-p4-analyzer` → merged into `feature/roadmap` (`--no-ff`, no conflicts).
- **Shipped:** read-only `analyze_results` tool (`app/tools/analyze.py`) + pure math in
  `app/validation/analysis.py` (`SLOTargets`, `evaluate_slo`, `pareto_analysis`). Given user SLO
  targets and one-or-more Benchmark Reports (single run, A/B pair, or a whole DoE sweep dir), it
  schema-validates each report (BR v0.2, never scrapes logs), computes a per-run SLO verdict over
  the full percentile ladder + an honest goodput *estimate* (the proposal's key differentiator),
  and for a sweep identifies the Pareto-optimal configs and the SLO-feasible frontier.
- **SessionPlan** now captures optional `slo` targets (max TTFT/TPOT/ITL/request-latency ms,
  min throughput floor tokens/s, success-rate). Registry/schemas/prompt updated; analysis
  judgment lives in the new `knowledge/analysis.md` (thin-code/thick-agent), with a
  `knowledge/sweep_playbook.md` cross-link. Goodput correctness fix carries the full ladder.
- **Tests:** worktree suite **219 passed / 6 skipped / 0 failed** (+386-line `test_analyze.py`;
  authoritative run in the integration worktree with the real venv + .env).
- Next: Phase 5 (historical result storage + trends UI).

## 2026-06-01 — Phase 3: Kubernetes-native Benchmark Orchestrator — DONE  (the 40% centerpiece)
Branch `feature/roadmap-p3-orchestrator` → merged into `feature/roadmap`. Built in
`app/orchestrator/` (kube.py, job.py, controller.py, faults.py) + tool `orchestrate_benchmark_run`.
- **Research first** (2-agent workflow): `llmdbenchmark run` submits the harness as a bare K8s
  Pod via `kubectl apply` and shells out to kubectl for everything (no Python client in the run
  path). Verdict: the orchestrator shells allowlisted kubectl too (security-model-consistent).
- **3a** (`0d1106c`): KubeClient (Real over ToolContext + Fake for tests). Allowlist: kubectl
  apply/logs/delete-job + value constraints; delete is job-by-name-only; `-f` is .yaml-only,
  no `..`, workspace-confined. **3b** (`8ab5697`): Job manifest (backoffLimit:0, deadline,
  labels on pod template), submit/watch/logs/reconstruct. **3c+3d** (`a77b165`): fault
  classification (6 kinds, priority-ordered) + retry/dead-letter (distinct `-aN` Jobs;
  transient retries, deterministic dead-letters). **3e** (`9cc48a3`): parallel sweep
  (concurrency-capped, per-treatment dead-letter) + cleanup; tool wiring (`a68cde5`) +
  `knowledge/orchestrator.md` (the remediation judgment, kept out of faults.py).
- **3-agent adversarial review** (`ed731f1`) found + fixed real bugs: watch() busy-looped with
  poll_interval=0 (now wall-clock bounded + floored sleep); run_sweep's bare gather let one
  raising treatment sink the sweep (now per-treatment try/except); classify mapped a
  failed-count-no-condition Job to PENDING (now FAILED); run_with_retries collapsed ABSENT/
  timeout into a bogus Failure(NONE). Hardening: manifest_path forbids `..`; Job name DNS-1123
  validation; non-breaking pod securityContext. +9 review-driven tests.
- **Tested hermetically** (FakeKubeClient + CaptureRunner) — no GPU, no live cluster runs.
- **Deferred to Phase 8 (packaging):** the in-cluster benchmark image + least-privilege
  ServiceAccount/RBAC + image pinning (so an orchestrated Job runs live). Until then the tool
  refuses without an image; `execute_llmdbenchmark` remains the live local path.
- **Tests:** worktree suite **190 passed / 6 skipped / 0 failed**.
- Next: Phase 4 (Results Analyzer — goodput, SLO filtering, Pareto/DoE analysis).

## 2026-06-01 — Phase 2: Parallel sessions & parallel benchmark runs — DONE
Branch `feature/roadmap-p2-parallel` → merged into `feature/roadmap`.
- **Concurrency cap** (`config.max_concurrent_runs`, default 2): a shared `asyncio.Semaphore`
  wraps only MUTATING executions in `ToolContext.run_command` (read-only probes uncapped);
  `SessionManager` passes the shared cap + isolated workspaces into every session's ctx.
- **Background-safe runs:** `main.py` no longer cancels an in-flight turn on disconnect — it
  detaches to `app.state.background_tasks` and finishes server-side (result replayed via Phase-1
  history on reconnect). A `connected` gate auto-rejects approvals requested after disconnect
  (so a detached turn can't hang holding a slot); a per-session `running` registry rejects a 2nd
  connection's concurrent turn; `ready.running` shows a UI note on reconnect.
- **Adversarial review (2 agents)** found a real latent bug: the runner's `proc.wait()` ran
  AFTER the stdout-pump `wait_for` timeout, so a child that closes stdout without exiting could
  hang forever and pin a concurrency slot. Fixed: `wait_for(gather(pump, wait), deadline)` +
  SIGKILL the process group (`start_new_session=True`) on timeout. Also: `get_running_loop()`.
  Added the two safety tests the review flagged as missing (post-disconnect auto-reject; 2nd-
  connection double-run guard) and tightened the survival test to assert detached-not-cancelled.
- **Deferred to Phase 3:** abandoned long runs hold a cap slot until timeout (need cancel/reattach
  or operator visibility); reconnecting clients see only the end result, not the live stream
  (need per-session pub/sub event buffer). Both are orchestrator-reconstruction concerns.
- **Tests:** worktree suite **127 passed / 6 skipped / 0 failed** (stable across repeated runs).
- Next: Phase 3 (Kubernetes-native Benchmark Orchestrator — the 40% centerpiece).

## 2026-06-01 — Phase 1: Command transparency, debug mode, UI polish — DONE
Branch `feature/roadmap-p1-transparency` → merged into `feature/roadmap`.
- **Backend** (`4f200ab`): a `command` event for every executed command, centralized in
  `ToolContext._emit_command` (the only two `runner.execute` call sites: `run_readonly` +
  `run_command`). Read-only probes — previously invisible — now announce themselves; mutating
  commands announce only after approval (so the trail = what truly ran). `Session.commands`
  (bounded 500) persists the trail; `main.py` records on the `command` event and resume replays it.
- **UI + tests** (`daf5486`): inline `$ cmd` console lines + a global "Executed commands" log
  (read-only/mutating badges, auto/approved tag) + a Debug toggle that shows only the command
  trail (persisted, applied pre-paint to avoid FOUC, aria-live for SR). Resume replays the trail.
- **Slider audit (#4):** no sliders exist (tree + git history). Deliberately did NOT invent
  parameter sliders (would embed judgment in the UI, violating thin-code/thick-agent); added a
  styled range-input foundation; real sliders deferred to where they fit (Phase 2/4). Documented.
- **Adversarial review:** 3-agent workflow (backend-security / UI-correctness / test-coverage).
  Findings fixed: pre-paint `data-debug` (FOUC), `aria-live` on cmdlog; added flow-level
  command-event/exec + session.commands parity asserts (all 12 flows), `probe_environment`
  6-probe visibility test, full-deploy command-surfacing test, harness now records commands
  (exercises emit→record→persist→replay), strengthened the read-only run_command test.
- **Tests:** worktree suite **119 passed / 6 skipped / 0 failed**.
- Next: Phase 2 (parallel sessions & parallel benchmark runs).

## 2026-06-01 — Phase 0: Autonomous scaffolding — DONE
- Created integration worktree `/home/tal/kind-quickstart-guide-roadmap` on `feature/roadmap`
  off `main` (`04c06fe`); fresh `.venv` (uv, py3.11) with `-e ".[dev]"`; `.env` carried over
  with `REPOS_DIR=/home/tal/kind-quickstart-guide` so the app + tests see the real sibling repos.
- **conftest portability fix:** `tests/conftest.py` resolves `BENCH_REPO` via `get_settings().bench_repo`
  (honors `REPOS_DIR`/`.env`) instead of a hardcoded `PROJECT_ROOT.parent` path. This converts the
  ~12 sibling-repo-dependent tests from FAIL → PASS in a worktree, with no change in the primary
  checkout. Backward-compatible (empty `REPOS_DIR` still falls back to the sibling layout).
- Wrote `ROADMAP.md` (10 phases, proposal-grounded ordering) and this `PROGRESS.md`.
- **Tests (worktree):** `110 passed, 6 skipped, 0 failed`. The single extra skip vs the primary
  checkout is `test_snapshot_matches_live` (catalog-drift guard) which deliberately skips when the
  bench repo isn't at the canonical sibling path — expected, benign.
- Next: Phase 1 (command transparency, debug mode, UI/slider polish).

## Phase 17 — Operability docs + alert rules — DONE
Branch `feature/roadmap-v2-p17-ops-docs` → merged into `feature/roadmap-v2` (`--no-ff`).
- Added `docs/SECURITY.md`, `docs/TROUBLESHOOTING.md`, `docs/CONTRIBUTING.md`, `docs/CHANGELOG.md`
  (linked from `docs/README.md`) and `deploy/observability/alerts.rules.yaml` (5 alert rules over
  the already-exported metrics). Docs + data only — no app behavior change.
- New hermetic `tests/test_ops_docs.py` checks the docs/sections exist, the alert YAML is valid
  Prometheus, and every metric the rules reference is actually exported (derived live from
  `app.observability.instrument`, so it can't drift); an optional `promtool` lint skips when the
  binary is absent.
- **Tests:** full suite **424 passed / 7 skipped / 0 failed** (prior baseline 415 passed / 6 skipped).
