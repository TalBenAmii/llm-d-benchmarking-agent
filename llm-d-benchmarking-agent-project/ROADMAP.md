# ROADMAP — llm-d Benchmarking Agent

> **Living document.** Updated at the end of every phase. Tracks the autonomous build-out
> that takes the project from "MVP chat agent that drives the `llmdbenchmark` CLI" toward
> the full vision in [`llm-d-benchmarking-agent-proposal.md`](llm-d-benchmarking-agent-proposal.md):
> a Kubernetes-native **conversational agent + benchmark orchestrator + results analyzer**.
>
> **Approved 2026-06-01.** Worked autonomously on branch `feature/roadmap` (integration
> branch; **never merged to `main`** during this effort). Each phase lands as one or more
> commits / merges into `feature/roadmap`.

## Why this order

The proposal is a Technion Parallel & Distributed Systems lab project with an explicit
grading rubric. Priorities below are driven by it plus the user's `todo` file:

| Grade dimension | Weight | Status at start |
|---|---|---|
| Kubernetes job orchestration (lifecycle, OOM/timeout/eviction, restart-resilience) | **40%** | mostly missing — CLI runs as a blocking subprocess |
| System design & API quality | 25% | strong (clean tool/allowlist/validation boundaries) |
| End-to-end functionality | 20% | partial (single quickstart works; goodput/Pareto missing) |
| Code quality & documentation | 15% | partial (good tests; no Helm chart / deploy docs) |

Quick user-requested wins come first (they improve the demo and the Observability story and
are an on-ramp to orchestration), then the 40%-weighted K8s orchestrator, then the analyzer
differentiators, then stretch goals and packaging/docs deliverables.

## Status legend
`TODO` · `IN-PROGRESS` · `DONE` · `DEFERRED`

---

## Phase 0 — Autonomous scaffolding — **DONE**
*Setup for the autonomous effort.*
- [x] Integration worktree `../kind-quickstart-guide-roadmap` on `feature/roadmap` (off `main`).
- [x] Worktree-portable tests: `tests/conftest.py` now resolves the read-only sibling repo via
      the app's own `get_settings()` (honors `REPOS_DIR`/`.env`), so the full suite runs in any
      worktree instead of failing on the empty sibling gitlinks. Backward-compatible with the
      primary checkout. Worktree suite: **110 passed / 6 skipped / 0 failed** (the 1 extra skip
      vs the primary checkout is the catalog-drift guard, which deliberately skips when the repo
      isn't at the canonical sibling path).
- [x] `ROADMAP.md` + `PROGRESS.md` living docs; status headers refreshed.

## Phase 1 — Command transparency, debug mode, UI polish — **DONE**
*User todos #2/#3/#4 · proposal "Observability".*
- [x] `command` event emitted for **every** executed command (centralized in ToolContext, so
      auto-run read-only probes are no longer invisible); for mutating commands it fires only
      after approval (records what truly ran). Persisted on the session + replayed on resume.
- [x] UI: inline `$ cmd` lines in each tool console + a global "Executed commands" log with
      read-only/mutating badges; a **Debug-mode** toggle showing only the executed-command
      trail (persisted, pre-paint, aria-live). (#2, #3)
- [x] Slider audit (#4): no slider elements exist in the UI (confirmed in tree + git history).
      Rather than invent parameter sliders that embed judgment in the UI (against thin-code),
      added a reusable styled range-input foundation; real sliders land where they genuinely fit
      (Phase 2 concurrency cap / Phase 4 SLO targets the agent proposes and the user fine-tunes).
- [x] Tests: 3-agent adversarial review; command-event/exec parity asserted across all 12 flows,
      probe-visibility (6 read-only probes), full-deploy command surfacing, persist/replay path.
      Suite: **119 passed / 6 skipped**.

## Phase 2 — Parallel sessions & parallel benchmark runs — **DONE**
*User todo #1 · proposal "parallel treatments w/ configurable concurrency", Distributed Coordination.*
- [x] Configurable cross-session concurrency cap (`settings.max_concurrent_runs`, default 2):
      a shared `asyncio.Semaphore` wraps only MUTATING executions; read-only probes never capped.
      `SessionManager` wires the shared cap + isolated per-session workspaces into every session.
- [x] Background-safe runs: an approved in-flight turn is no longer cancelled on WS disconnect —
      it finishes server-side; result replayed from history on reconnect. A `connected` gate
      auto-rejects post-disconnect approvals (no hang); a per-session running-turn registry blocks
      a 2nd connection's concurrent turn (prevents transcript corruption); `ready.running` drives
      a UI note.
- [x] Review fix (real latent bug): the runner now bounds the **whole** process lifecycle under
      the deadline (was: `proc.wait()` ran after the stdout-pump timeout → a stdout-closing daemon
      could hang forever and pin a slot); SIGKILLs the process group on timeout.
- [x] Tests: 8 hermetic concurrency tests. Suite: **127 passed / 6 skipped**.
- **Deferred to Phase 3** (orchestrator territory): (a) an abandoned long run (e.g. a 4h
  `experiment` the user navigated away from) holds a cap slot until its timeout — needs a
  cancel/reattach path or operator visibility; (b) a reconnecting client only sees the result
  replayed at the end, not the live stream — needs a per-session pub/sub event buffer.

## Phase 3 — Kubernetes-native Benchmark Orchestrator — **DONE**  *(the 40% centerpiece)*
*Proposal §3.3 · grade dimension 1. Built in `app/orchestrator/` + the `orchestrate_benchmark_run` tool.*
- [x] 3a. **KubeClient** abstraction over the allowlisted `kubectl` runner (NOT the Python
      client — keeps the deny-by-default + approval + env-scrub model). Allowlist gained
      `kubectl apply/logs/delete job` (tight: delete is job-by-name-only, `-f` is `.yaml` +
      no `..` + workspace-confined). Research confirmed `llmdbenchmark run` submits the harness
      as a bare Pod via `kubectl apply` and shells out for everything.
- [x] 3b. Job model + controller: submit (write manifest → apply), **poll-based watch** to
      terminal (wall-clock bounded), pod-log streaming, cluster-only **reconstruction** from
      labels.
- [x] 3c. **Fault classification**: OOM / timeout / eviction / unschedulable / image / run
      error, priority-ordered, facts-only (remediation lives in `knowledge/orchestrator.md`).
- [x] 3d. **Retry + dead-letter**: transient faults retry as fresh distinct Jobs (`-aN`);
      deterministic faults dead-letter immediately. Stateless reconstruction = source of truth.
- [x] 3e. **Parallel sweep** (concurrency-capped, per-treatment dead-letter) + **cleanup**
      (terminal-only by default; preserves the results PVC). Wired as the agent tool
      `orchestrate_benchmark_run` (+ `knowledge/orchestrator.md`).
- [x] 3-agent adversarial review; fixed a watch busy-loop, sweep exception-isolation, a
      classify gap, + security hardening. Tested hermetically (FakeKubeClient + CaptureRunner),
      no GPU/live runs. Suite: **190 passed / 6 skipped**.
- **Deferred to Phase 8 (packaging):** the in-cluster benchmark image + a least-privilege
  ServiceAccount/RBAC (so an orchestrated Job actually runs live); image pinning.

## Phase 4 — Results Analyzer: goodput, SLO filtering, Pareto/DoE analysis — **DONE**
*Proposal §3.4 · grade dimension 3.*
- **Result:** `analyze_results` tool + `app/validation/analysis.py` (SLOTargets captured in
  the SessionPlan; per-run SLO verdict over the full percentile ladder; honest goodput estimate;
  Pareto-optimal config selection + SLO-feasible frontier across a DoE sweep), grounded in the
  new `knowledge/analysis.md`. Read-only, schema-validated (BR v0.2), never scrapes logs.
- Capture SLO targets (TTFT / TBT / P99 / throughput floor) in the SessionPlan.
- Compute **goodput** (proposal's "key differentiator").
- Identify **Pareto-optimal** configs across a sweep matrix; richer comparison + plain-language
  explanation tied to `knowledge/analysis.md`.

## Phase 5 — Historical result storage + trends UI — **DONE**
*Proposal stretch "historical storage + trend visualization".*
- Persist validated reports across sessions; results-browser / trends view in the UI.
- **Result:** validated reports persisted to a cross-session history store via a single
  `result_history` tool (store/list/get/trend/delete) plus a UI results-browser/trends view;
  merged into `feature/roadmap`, full suite **297 passed / 6 skipped / 0 failed**.

## Phase 6 — Configuration Explorer / Capacity Planner pre-flight — **DONE**
*Proposal §2.2 stretch.*
- Use the repo's capacity planner to pre-validate feasibility before a run (surface
  "will this fit?" at the plan gate); reduces OOM failures.
- **Result:** `check_capacity` tool runs a memory/feasibility pre-flight (KV + weights +
  activation vs accelerator memory) over the rendered spec at the plan gate, advisory or
  enforced; merged into `feature/roadmap`, full suite **245 passed / 6 skipped / 0 failed**.

## Phase 7 — Observability: Prometheus/Grafana — **DONE**
*Proposal stretch + Observability dimension.*
- Export agent/orchestrator metrics; optional Grafana dashboard; live system metrics during runs.
- **Result:** Prometheus `/metrics` endpoint + instrumentation across the agent/orchestrator,
  a new read-only `observe_run_metrics` tool (live `kubectl top` pod/node usage during a run),
  Grafana dashboard + scrape config under `deploy/observability/`; merged into `feature/roadmap`,
  full suite **269 passed / 6 skipped / 0 failed**.

## Phase 8 — Packaging: container image + Helm/Kustomize single-command deploy — **DONE**
*Proposal §5.3 deliverable.*
- Production image for the agent service; Helm chart / Kustomize for one-command K8s deploy.
- **Result:** hardened non-root Dockerfile + Helm chart and Kustomize base/overlay rendering
  Deployment/Service/ServiceAccount + namespaced least-privilege Role/RoleBinding (exactly the
  kubectl verbs RealKubeClient uses), resolving the Phase-3 RBAC deferral; orchestrated Jobs now
  run under the deploy's SA. Suite 315 passed / 6 skipped / 0 failed.

## Phase 9 — Documentation suite + upstream-PR readiness — **DONE**
*Grade dimension 4 · §10.*
- Architecture doc, API reference, deployment & user guides; polish toward the upstream
  `llm-d-benchmark` PR path.
- **Result:** added `docs/` suite (ARCHITECTURE, API, DEPLOYMENT, USER_GUIDE, README index) plus
  refreshed root README/CLAUDE.md; docs-only, no code changes — suite stays green at 329 passed / 6 skipped.

## Phase 10 — Multi-harness orchestration in one session — **DONE**
*Proposal stretch.*
- Agent recommends + runs both inference-perf (SLO validation) and guidellm (throughput sweep)
  in one session, then compares.
- **Result:** new read-only `compare_harness_runs` tool + pure `compare_across_harnesses()` group
  runs by detected harness, cross-validate shared metrics (no cross-harness winner), backed by
  `knowledge/multi_harness.md`; `summarize_report` now surfaces producing harness + load point.

---

## Autonomous execution rules (self-imposed)
- **Branching:** `feature/roadmap` is the integration branch and "home" worktree. Each phase is
  developed on a `feature/roadmap-pN-<slug>` branch and merged into `feature/roadmap`. When a
  phase decomposes into independent parts, subagents work in **separate worktrees partitioned by
  file/module** (no collisions) and their branches merge back into the phase branch.
  **`main` is never touched.**
- **Tests:** pytest only. No long/real benchmark runs, no GPU. The orchestrator is validated with
  a fake kube client and the hermetic CaptureRunner harness.
- **Commits:** one after every legitimate feature; clear scoped messages; `Co-Authored-By` trailer.
- **Docs:** `ROADMAP.md` + `PROGRESS.md` (and affected `.md`/knowledge files) updated each phase.
- **Context hygiene:** after each phase commit, if context > 150k tokens, document first, then
  compact (if context still needed) or clear (if not).
- **Thin code, thick agent** stays the law: mechanism in Python, judgment in `knowledge/`.

---

## Roadmap v2 — production operability, trust & quality (Phases 11-18)

Phases 11-18 are developed on the integration branch `feature/roadmap-v2` (never `main`); each phase merges in after its full-suite gate is green.

## Phase 11 — Structured logging + correlation IDs — DONE
- Shipped stdlib-only structured logging (`app/observability/logging.py` JSON formatter — one
  JSON object per line — + `logctx.py` contextvars carrier). A fresh `corr_id` is minted at the
  WebSocket boundary and rides via `contextvars` (snapshotted by `create_task`) into the agent
  loop (`turn.start/end`, `tool.call.start/result`), every tool dispatch (`tool=<name>`), and the
  command runner (`command.exec`, `runner.exec.*`) — so one turn is traceable end-to-end by
  grepping its `corr_id`. `LOG_LEVEL`/`LOG_FORMAT` (json default, text for dev) added to config;
  `setup_logging()` wired once in the lifespan. Secrets never logged (exe = argv[0] only). No new
  runtime dependency. Judgment in `knowledge/logging.md`.
- Merged into `feature/roadmap-v2` (`--no-ff`); full suite **339 passed / 6 skipped / 0 failed**
  (+7 hermetic logging tests; prior baseline 332 passed / 6 skipped).

## Phase 12 — API trust: auth + rate-limit + CORS — DONE
- Shipped stdlib-only, default-off API-trust controls (NO new dependency) so the FastAPI surface is
  safe to expose while staying frictionless locally: optional Bearer auth (constant-time
  `secrets.compare_digest`; an app-level dependency guards every HTTP route and the `/ws` handshake
  is guarded in-handler — bad/missing token → 401 / WS close 1008) and a `TokenBucket`/`RateLimiter`
  with an injectable monotonic clock guarding `/api/*` intake (empty bucket → 429; `/healthz` +
  `/metrics` never throttled), plus `CORSMiddleware` wired only when `CORS_ALLOW_ORIGINS` is set.
  `app/config.py` adds `AUTH_ENABLED`/`AUTH_TOKEN`/`RATE_LIMIT_*`/`CORS_ALLOW_ORIGINS`; lifespan
  fails loud if `AUTH_ENABLED` with empty `AUTH_TOKEN`. Judgment in `knowledge/api_trust.md`.
- Merged into `feature/roadmap-v2` (`--no-ff`); full suite **351 passed / 6 skipped / 0 failed**
  (+12 hermetic api-trust tests; prior baseline 339 passed / 6 skipped).

## Phase 13 — Allowlist governance: per-command timeouts + quotas — DONE
- Moved execution limits out of Python and into `security/allowlist.yaml` as data: optional
  `timeout_s` and `quota {per_session, per_day}` on an executable and/or subcommand (subcommand
  overrides executable). `allowlist.py` schema-validates both fields at startup (malformed
  allowlist → clear load-time error) and rides the resolved limits on the `Decision`. The runner
  now sources its per-command deadline from `Decision.timeout_s`, REMOVING the parallel
  `app/tools/execute.py::_TIMEOUTS` table (one mechanism, not two; a sane global default remains).
  New `app/security/quota.py` is a pure per-session/per-day usage counter; `ToolContext` refuses an
  over-quota command with a structured `QuotaError` BEFORE execution/approval and the loop relays it.
  Judgment in `knowledge/governance.md`.
- Merged into `feature/roadmap-v2` (`--no-ff`); full suite **378 passed / 6 skipped / 0 failed**
  (+27 hermetic governance tests; prior baseline 351 passed / 6 skipped).

## Phase 15 — WebSocket protocol hardening + live event buffer — DONE
- The `/ws` boundary now validates every inbound frame against an explicit Pydantic tagged union
  (`app/agent/ws_schemas.py`: `user_message`/`approval`/`ping`, `extra="forbid"`); a malformed or
  non-dict frame is rejected with a structured `error` event of `kind="protocol_error"` and the
  socket is KEPT alive instead of silently no-op'ing or crashing. The `Channel` gained a bounded
  per-turn live ring buffer (`deque(maxlen=...)`): every emitted turn event is fanned out live AND
  appended, so a client reconnecting mid-turn replays the missed live stream and continues live;
  lifecycle frames (`ready`/`history`/`pong`) are excluded so reconnects don't replay stale
  handshakes. Outbound envelope unified via `outbound()`; `ping` answered with `pong`.
- Merged into `feature/roadmap-v2` (`--no-ff`); full suite **384 passed / 6 skipped / 0 failed**
  (+`tests/test_ws.py`, 280 lines; prior baseline 378 passed / 6 skipped).

## Phase 18 — Workspace lifecycle: retention/GC + startup self-check — DONE
- Added `app/storage/retention.py`: a config-driven retention/GC over the workspace scratch areas
  (sessions, history, orchestrator `workspace/jobs/*.yaml`) — policy as DATA in `config.py`
  (`retention_max_age_days`/`max_items`/`max_bytes`, unlimited by default), walk/counter as the
  mechanism. The FastAPI `lifespan` runs a one-shot GC at startup (`retention_gc_on_startup`, ON by
  default, never blocks startup) that excludes any live/running session. Also a structured startup
  `self_check` (workspace writable, provider coherent, repos resolvable, auth coherent) surfaced via
  a new `/readyz` readiness probe (200/503 + structured reasons); liveness stays on `/healthz`.
- Merged into `feature/roadmap-v2` (`--no-ff`); full suite **404 passed / 6 skipped / 0 failed**
  (+20 hermetic tests in `test_retention.py` + `test_readyz.py`; prior baseline 384 passed / 6 skipped).

## Phase 16 — Run lifecycle & readiness — DONE
- Closes Phase 2's two deferrals with pure mechanism (`app/agent/lifecycle.py`: a `RunRegistry`
  of in-flight turn tasks). A new `cancel_run` tool (`app/tools/cancel.py`) cancels another
  session's running turn from OUTSIDE itself — `asyncio` unwinds the concurrency-cap semaphore so
  the slot is freed, and the runner reaps the child process group on `CancelledError` so no K8s
  Job / subprocess is orphaned. The `lifespan` now installs a SIGTERM graceful-shutdown that
  cancels every in-flight run (composed with Phase 18's startup self-check + retention GC); a
  `cancelled` event + a cancel control message round out the surface, and `/readyz` gains a
  `runner_ok` component. `knowledge/run_lifecycle.md` holds the JUDGMENT of *when* to cancel.
- Merged into `feature/roadmap-v2` (`--no-ff`); full suite **415 passed / 6 skipped / 0 failed**
  (+`tests/test_run_lifecycle.py`, 384 lines; prior baseline 404 passed / 6 skipped).

## Phase 17 — Operability docs + alert rules — DONE
- Shipped the four operability docs under `docs/` (SECURITY: threat model, trust boundaries,
  allowlist/approval model, secret scrubbing, network-exposure; TROUBLESHOOTING: symptom->fix
  keyed to the structured logs/`corr_id` + `/healthz`//`readyz`//`metrics`; CONTRIBUTING:
  thin-code + allowlist-as-data laws + the hermetic-test rule; a Keep-a-Changelog) linked from
  the docs index, plus `deploy/observability/alerts.rules.yaml` (5 Prometheus rules over the
  EXISTING exported metrics — slow commands, elevated run-failure/fault rates, stuck in-flight
  runs, target down). Docs + data only; no app behavior change.
- Merged into `feature/roadmap-v2` (`--no-ff`); full suite **424 passed / 7 skipped / 0 failed**
  (+`tests/test_ops_docs.py`, 224 lines — asserts docs/sections exist, the rule YAML is valid
  Prometheus, and every referenced metric is one the app actually exports, derived live so it
  can't drift; the +1 skip is an optional `promtool` check skipped when the binary is absent.
  Prior baseline 415 passed / 6 skipped).


## Phase 14 — Quality gates: ruff + mypy + coverage — DONE
- Wired three CI-enforced quality gates into `pyproject.toml` / `Makefile` and a new
  `.github/workflows/agent-flow-validation.yml`: **ruff** (lint, clean), **mypy** (strict typecheck
  over `app`, no issues across 61 source files), and a **coverage-gated** pytest run
  (`--cov=app --cov-fail-under=85`). Tightened types/lint across the tree (tools, validation,
  observability, security, storage) so the gates pass with no behavior change; added
  `tests/test_quality_gates.py` (146 lines) asserting the config/threshold/CI wiring stay in place.
- Merged into `feature/roadmap-v2` (`--no-ff`); full suite **432 passed / 7 skipped / 0 failed**
  (ruff clean, mypy clean, coverage **88.90%** >= 85% gate; prior baseline 424 passed / 7 skipped).

## Roadmap v3 — proposal-completion features (Phases 19-26)
Integration branch `feature/roadmap-v3` off `main` (the chosen base; main is never touched directly) for the missing proposal-coverage features.

## Phase 19 — DOE experiment-file generator + token-characteristics elicitation — DONE
- Shipped a `generate_doe_experiment` tool (`app/tools/doe.py`) backed by pure mechanism in
  `app/validation/doe.py`: the agent supplies factors (name, dotted override key, levels) for the
  optional `setup` and required `run` phases; the tool cross-products factors × levels into the full
  treatments matrix, emits a valid experiment YAML into the session workspace (never the read-only
  repos), and validates it STRUCTURALLY against the llm-d-benchmark experiment example format read
  LIVE from disk (no vendored copy). WHICH factors/levels to sweep is agent judgment grounded in an
  expanded `knowledge/sweep_playbook.md`, which now adds explicit token-characteristics / prefix-reuse
  elicitation guidance. Thin code / thick agent: no factor-selection logic in Python.
- Merged into `feature/roadmap-v3` (`--no-ff`, clean `ort` merge — additive registry/schemas/knowledge,
  no entries dropped); full suite **477 passed / 7 skipped / 0 failed**, ruff clean, mypy clean
  (+`tests/test_doe.py`, 429 lines).

## Phase 20 — Well-lit-path advisor — DONE
- Shipped `knowledge/welllit_path_advisor.yaml` (the Well-lit-path advisor): ADVISORY DATA mapping a
  workload SHAPE → the llm-d well-lit-path scenario worth benchmarking, with the SIGNALS that select it
  (prefix-reuse, context length, concurrency, SLO emphasis), a plain-language rationale, candidate
  `benchmark_workloads`, and a `deploy_path` reach flag (kind-local vs gpu-only). The judgment lives in
  the data, not in any Python `if/elif`: the file is inlined into the system prompt (added to
  `CORE_KNOWLEDGE` in `app/agent/prompt.py`) and served via `read_knowledge`. `deploy_path_playbook.md`
  now points at the advisor so the agent consults it once a GPU deploy is in scope, while still
  benchmarking `cicd/kind` for a local sanity pass. Thin code / thick agent — code only reads the file.
- Merged into `feature/roadmap-v3` (`--no-ff`, clean `ort` merge — additive prompt/knowledge, no entries
  dropped); full suite **491 passed / 7 skipped / 0 failed**, ruff clean, mypy clean (63 files)
  (+`tests/test_welllit_advisor.py`, 231 lines; hermetic — validates every archetype's fields/signals and
  that each `scenario`/`also_consider`/`benchmark_workloads` id resolves against the catalog snapshot).

## Phase 25 — Analyzer metric completeness: KV-cache hit rate, schedule delay, GPU utilization — DONE
- Extended `summarize_report`/`analysis` to PARSE and SURFACE the §3.4 standard serving metrics that
  were previously ignored — KV-cache hit rate, schedule delay (queue-depth proxy), and GPU utilization —
  mechanically extracting the first present candidate from EITHER the BR v0.2 standardized
  `observability.components[].aggregate` shape OR a harness-native per-metric entry, with field-name
  discovery as DATA in `knowledge/standard_metrics.yaml` (thin code / thick agent); gracefully `None`
  when a harness doesn't emit them — never fabricated. Surfaced in the human summary
  (`summary.standard_metrics`), per-run in `analyze_results`, and as INFORMATIONAL Pareto objectives kept
  deliberately OUT of dominance — goodput/SLO/Pareto behavior unchanged.
- Merged into `feature/roadmap-v3` (`--no-ff`, clean `ort` merge — additive analyzer/knowledge, no
  entries dropped); full suite **509 passed / 7 skipped / 0 failed**, ruff clean, mypy clean (63 files)
  (+`tests/test_standard_metrics.py`, 293 lines; hermetic — standardized + native extraction, catalog
  preference order, graceful degradation on absent/garbage reports, real BR v0.2 surfacing, and the
  informational-only Pareto behavior).
