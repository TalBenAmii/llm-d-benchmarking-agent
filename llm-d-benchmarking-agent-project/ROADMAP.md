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
