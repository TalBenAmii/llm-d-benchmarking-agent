# PROGRESS LOG

Reverse-chronological log of the autonomous roadmap effort. One entry per work session /
phase milestone. See [`ROADMAP.md`](ROADMAP.md) for the plan and phase status.

Branch: `feature/roadmap` (integration; never merged to `main` during this effort).
Test baseline at start (primary checkout `main` @ `04c06fe`): **111 passed / 5 skipped**.

---

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
