# PROGRESS LOG

Reverse-chronological log of the autonomous roadmap effort. One entry per work session /
phase milestone. See [`ROADMAP.md`](ROADMAP.md) for the plan and phase status.

Branch: `feature/roadmap` (integration; never merged to `main` during this effort).
Test baseline at start (primary checkout `main` @ `04c06fe`): **111 passed / 5 skipped**.

---

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
