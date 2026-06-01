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

## Phase 1 — Command transparency, debug mode, UI polish — **TODO**
*User todos #2/#3/#4 · proposal "Observability".*
- Emit a structured event for **every** runner invocation (including auto-run read-only probes,
  not just approval-gated mutating commands) so the UI can show the full executed-command log.
- **Debug mode** toggle: a view showing only the executed `argv` timeline (per the `todo`
  "look at cicd feature" note) — useful for demos and trust.
- Audit & fix slider styling; wire scale-parameter sliders (concurrency / QPS / token lengths)
  into the SessionPlan interview where appropriate.
- Tests: hermetic event/UI-contract tests. No live runs.

## Phase 2 — Parallel sessions & parallel benchmark runs — **TODO**
*User todo #1 · proposal "parallel treatments w/ configurable concurrency", Distributed Coordination.*
- Concurrency-safe runner; run multiple sessions and multiple DoE treatments concurrently under
  a configurable concurrency cap.
- Tests with fakes/CaptureRunner — no live runs.

## Phase 3 — Kubernetes-native Benchmark Orchestrator — **TODO**  *(the 40% centerpiece)*
*Proposal §3.3 · grade dimension 1. Split into sub-phases.*
- 3a. Submit benchmark runs as **K8s Jobs** (wrap CLI/harness), labeled/annotated for
      reconstruction. First: audit how `llmdbenchmark run` executes the harness today.
- 3b. **Watch-API** monitoring + real-time pod-log streaming to the UI.
- 3c. **Fault tolerance**: detect OOM / timeout / pod eviction; retry policy; dead-letter for
      persistently-failing sweep treatments.
- 3d. **Stateless reconstruction**: zero local state — rebuild session/run state from K8s
      labels/annotations/ConfigMaps after a restart; checkpoint/resume for long DoE sweeps.
- 3e. **Cleanup**: reap completed Jobs/ConfigMaps; preserve artifacts.
- Tested hermetically against a **fake kube client** + the existing CaptureRunner pattern —
  no GPU, no long real runs.

## Phase 4 — Results Analyzer: goodput, SLO filtering, Pareto/DoE analysis — **TODO**
*Proposal §3.4 · grade dimension 3.*
- Capture SLO targets (TTFT / TBT / P99 / throughput floor) in the SessionPlan.
- Compute **goodput** (proposal's "key differentiator").
- Identify **Pareto-optimal** configs across a sweep matrix; richer comparison + plain-language
  explanation tied to `knowledge/results_interpretation.md`.

## Phase 5 — Historical result storage + trends UI — **TODO**
*Proposal stretch "historical storage + trend visualization".*
- Persist validated reports across sessions; results-browser / trends view in the UI.

## Phase 6 — Configuration Explorer / Capacity Planner pre-flight — **TODO**
*Proposal §2.2 stretch.*
- Use the repo's capacity planner to pre-validate feasibility before a run (surface
  "will this fit?" at the plan gate); reduces OOM failures.

## Phase 7 — Observability: Prometheus/Grafana — **TODO**
*Proposal stretch + Observability dimension.*
- Export agent/orchestrator metrics; optional Grafana dashboard; live system metrics during runs.

## Phase 8 — Packaging: container image + Helm/Kustomize single-command deploy — **TODO**
*Proposal §5.3 deliverable.*
- Production image for the agent service; Helm chart / Kustomize for one-command K8s deploy.

## Phase 9 — Documentation suite + upstream-PR readiness — **TODO**
*Grade dimension 4 · §10.*
- Architecture doc, API reference, deployment & user guides; polish toward the upstream
  `llm-d-benchmark` PR path.

## Phase 10 — Multi-harness orchestration in one session — **TODO**
*Proposal stretch.*
- Agent recommends + runs both inference-perf (SLO validation) and guidellm (throughput sweep)
  in one session, then compares.

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
