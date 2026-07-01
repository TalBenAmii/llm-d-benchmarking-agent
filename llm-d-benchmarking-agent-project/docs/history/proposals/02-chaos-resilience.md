# SPEC: Chaos / Fault-Injection + Orchestrator-Restart Durability + Resilience Report

> **Status: REMOVED 2026-07-02** (was implemented & merged as of 2026-06, then retired: the
> drill was hermetic-only — it ran against an in-process fake cluster behind the double
> `CHAOS_ENABLED` gate — and nothing else depended on it). The `run_resilience_drill` tool,
> `app/orchestrator/{chaos,resilience,restart}.py`, the `chaos_enabled` gate, the resilience
> card/knowledge, and its tests were all deleted; the ordinary fault-classification /
> retry / dead-letter path (`faults.py`, `controller.py`) it exercised remains in production.
> This spec is kept as the historical design record only — not pending work, not a current feature.

## 0. Investigation summary (what exists today)

- **Fault classification** is pure and stable: `app/orchestrator/faults.py:14-22` defines the kinds (`timeout`, `oom`, `unschedulable`, `evicted`, `image_error`, `run_error`, `unknown`, `none`); `classify_failure()` (`faults.py:99-119`) scans a `JobStatus` + the Job's pod JSON in priority order (timeout → oom → unschedulable → evicted → image → run_error). Facts only.
- **Retry / dead-letter** lives in `BenchmarkOrchestrator.run_with_retries` (`controller.py:280-348`): transient kinds (`DEFAULT_RETRYABLE = {EVICTED, UNKNOWN}`, `controller.py:53`) resubmit as a *fresh distinct Job* `<run_id>-a<N>` (`controller.py:308`); deterministic kinds dead-letter immediately (`controller.py:332-336`). Each attempt is recorded as an `AttemptResult` (`controller.py:66-70`) inside a `RunOutcome` (`controller.py:73-80`).
- **Watch** is poll-based and stateless: `watch()` (`controller.py:148-177`) re-reads `kubectl get jobs -l run-id=<id>` every poll; `terminal` = succeeded/failed; a Job that vanishes after being seen is `absent` (terminal); `max_wait` is a client wall-clock bound that returns the non-terminal status.
- **Restart durability already exists at the state-rehydration layer** but is NOT proven end-to-end:
  - `reconstruct()` (`controller.py:255-268`) rebuilds in-flight run state purely from cluster labels (`managed-by` + optional session/sweep).
  - `reconstruct_sweep()` (`controller.py:270-278`) + `checkpoint.py` persist sweep progress to a **ConfigMap** (`llmd-bench-sweep-<sweep_id>`), the single source of truth; completed treatments are skipped on resume (`controller.py:451-460`), and `record_in_flight` never downgrades a completed record (`checkpoint.py:92-97`).
  - "Orchestrator restart" here means: **a fresh `BenchmarkOrchestrator` object (or process) holding NO local state, pointed at the same cluster, calls `reconstruct*` and resumes** (`controller.py:5-9`).
- **The tool**: `orchestrate_benchmark_run` (`app/tools/orchestrate.py:71-174`) builds a `JobSpec`, gates on endpoint readiness (`orchestrate.py:119-133`), then calls `run_with_retries`; `cancel_run` (`app/tools/cancel.py`) cancels an in-flight turn. There is **no sweep tool and no list/reconstruct tool** exposed to the agent (`registry.py:449-479`).
- **Test harness**: `tests/orchestrator_fakes.py` is a programmable in-memory `FakeKubeClient` — `program(run_id, phases=[...], pods=[...])`; `make_job`/`make_pod` build the exact JSON shapes `classify_failure` consumes (e.g. `make_pod(..., terminated="OOMKilled", exit_code=137)` at `orchestrator_fakes.py:50-71`). The checkpoint tests (`test_orchestrator_checkpoint.py:110-179`) already demonstrate "interrupt → fresh store → resume" with two separate orchestrator instances against one fake cluster.
- **Metrics**: `app/observability/instrument.py:115-123` records run outcome + fault kind. **No existing chaos/resilience code anywhere** (grep confirmed).
- **Card surfacing**: deterministic cards are built by `app/agent/results_card.py::build_results_card` and emitted by `loop.py:191-193` as a `results_card` event; `ui/app.js:1276` renders them. Today only `analyze_results` yields a card.

**Key architectural finding:** the entire Job lifecycle flows through the `KubeClient` Protocol (`kube.py:50-64`). Real runs use `RealKubeClient`; tests use `FakeKubeClient`. **Fault injection is naturally a `KubeClient` decorator** — it never has to touch `controller.py`, `faults.py`, or `checkpoint.py`. That is the cleanest opt-in seam.

---

## 1. Goal & user-facing behavior

**Goal:** Add the *injection* side of resilience (classification/retry/recovery already exist) and produce a demonstrable, structured **resilience report** proving the orchestrator (a) correctly classifies and recovers from injected faults, and (b) survives its own restart mid-run and resumes from cluster/checkpoint state.

**How a user opts in (chat):**
> "Run a chaos drill on the cicd/kind benchmark — inject an eviction and an OOM and show me the resilience report."

The agent calls a new tool `run_resilience_drill`. Chaos is **never** reachable from the normal `orchestrate_benchmark_run` path unless the dedicated drill tool is invoked AND a backend flag permits it (`CHAOS_ENABLED`, default `false`). Two layers of opt-in.

**The resilience report (sketch — data model returned by the tool, rendered as a `results_card`):**

```
Resilience drill — cicd/kind (run rd-3f2a)   SLO: complete within 600s  ✓ MET (412s)

 Injected faults                        Classified as   Recovery action      Recovered
  ─────────────────────────────────────────────────────────────────────────────────
  evicted   @ attempt 1, before-watch    evicted        retry → fresh Job -a2   ✓
  oom       @ attempt 1                   oom            dead-letter (no retry)  ✓ (by-design)
  timeout   @ attempt 1                   timeout        dead-letter (no retry)  ✓ (by-design)

 Orchestrator restart drill
  • killed mid-run after attempt -a1 submitted (Job in cluster, no local state)
  • fresh orchestrator reconstruct()  → recovered 1 in-flight run from cluster labels
  • sweep checkpoint  → 2/5 treatments already COMPLETED, resumed remaining 3, 0 re-run
  • RESULT: resumed and completed; no duplicate Jobs; SLO met

 Verdict: 3/3 faults classified correctly; 3/3 recoveries as designed; restart survived.
```

The agent then *narrates* the report (judgment) using a new `knowledge/resilience.md`. The card carries only facts (mechanism); the verdict prose is the LLM's.

---

## 2. Architecture

### 2.1 Decision: chaos is a HARNESS/DRILL concern injected via a `KubeClient` decorator — NOT a change to the production lifecycle path

**Justification.** The point of the #1 grading criterion is proving the *existing* lifecycle (classify → retry → dead-letter → checkpoint/reconstruct) is correct under adverse conditions. We must drive faults through the **real, unmodified** `controller.py`/`faults.py`/`checkpoint.py` so the proof is genuine. The cleanest seam is a `KubeClient` decorator that wraps any underlying client and *deterministically rewrites cluster reads* (the Job/pod JSON returned by `list_jobs`/`list_pods`) to present a fault at a controlled point/probability.

- Satisfies "default OFF / opt-in": a normal run constructs `RealKubeClient(ctx)` directly (`orchestrate.py:152`) — the decorator is never instantiated.
- Satisfies "reuse, don't duplicate": injected faults are just realistic pod JSON (`make_pod`-shaped), so they flow through the *unchanged* `diagnose()` → `classify_failure()` → `run_with_retries` retry/dead-letter logic. Zero new classification/retry code.
- Testable hermetically: in tests the decorator wraps `FakeKubeClient`. **Recommendation: drill-against-fake only** for hermeticity and to never deliberately break a real cluster.

**Where the seam lives:** a new module `app/orchestrator/chaos.py` exporting `ChaosKubeClient` (implements the `KubeClient` Protocol by delegation) + a pure `ChaosPlan`/`FaultInjection` data model + a `FaultLedger` recording what was injected and when.

### 2.2 How an injected fault flows through the existing path (reuse, no duplication)

1. `run_with_retries` submits attempt `-a1` and calls `watch()` (`controller.py:314`).
2. `watch()` polls `status()` → `list_jobs` on the `ChaosKubeClient`. The decorator, per its `ChaosPlan`, rewrites the returned Job snapshot to a `failed` phase at the programmed point (and records the injection in the `FaultLedger`).
3. `run_with_retries` sees `FAILED`, calls `diagnose()` → `list_pods`, which returns a fault-shaped pod (e.g. OOMKilled). `classify_failure` (unchanged) returns `OOM`.
4. The unchanged retry/dead-letter rule decides: evicted → retry `-a2`; oom/timeout → dead-letter. We assert exactly that happened.
5. The `FaultLedger` holds `[(kind, attempt, injection_point)]`; the resilience report cross-references it against the `RunOutcome.attempts` (which carry the *classified* failure + recovery taken).

The report = **inject ledger ⋈ RunOutcome attempts** — a pure join in Python.

### 2.3 How the restart-durability proof works (against the real checkpoint/reconstruct)

A restart is modelled as **discarding the orchestrator object and building a new one against the same (fake) cluster state**, then calling `reconstruct()` / `reconstruct_sweep()` / `run_sweep(sweep_id=...)` and asserting resume. Two proof modes, both reusing existing code:

- **Single-run reconstruct proof** (`reconstruct()` at `controller.py:255`): submit a run, drop the orchestrator while the Job is still `active`, build a fresh `BenchmarkOrchestrator(same_kube, ...)`, call `reconstruct(namespace, session_id)`, assert it recovers the in-flight Job purely from labels — then `watch()` to completion on the new object.
- **Sweep checkpoint proof** (`run_sweep(sweep_id=...)` at `controller.py:350`): already demonstrated by `test_orchestrator_checkpoint.py:130-179` (run k of N → fresh orchestrator → resume remaining, 0 re-runs). The drill **reuses that exact pattern** and records the facts.

The new `app/orchestrator/restart.py` is a thin **mechanism** wrapper (`prove_restart_recovery`) that performs the kill-and-rehydrate and returns a `RestartProof` dataclass (facts). It calls only existing controller methods.

---

## 3. Judgment vs mechanism (explicit)

| Concern | Layer | Where |
|---|---|---|
| Injection mechanics (when/which fault, probability, rewrite pod JSON) | **Mechanism** | `app/orchestrator/chaos.py` (`ChaosPlan` is *data the agent supplies*, not policy) |
| Recording what was injected + when | **Mechanism** | `FaultLedger` in `chaos.py` |
| Cross-join ledger ⋈ outcome → report data | **Mechanism** | `app/orchestrator/resilience.py` (`build_resilience_report`) |
| Kill-and-rehydrate restart proof | **Mechanism** | `app/orchestrator/restart.py` |
| Deterministic card render model | **Mechanism** | extend `app/agent/results_card.py` |
| *What each fault classification means / whether recovery was correct* | **Judgment** | `knowledge/resilience.md` |
| *Whether the run is "resilient enough"; what to fix* | **Judgment** | `knowledge/resilience.md` (cross-links `knowledge/orchestrator.md`) |
| *Which faults to inject for a given scenario / SLO* | **Judgment** | `knowledge/resilience.md` — the agent chooses the `ChaosPlan`; Python only validates the shape |

The `ChaosPlan` is the same pattern as `Scheduling` (`job.py:47-149`): a pure, type-validated, policy-free data object the agent fills in; Python rejects on *shape*, never on *wisdom*.

---

## 4. Exact new/changed files

### New files

**`app/orchestrator/chaos.py`** (mechanism — the injection seam)
- `FaultInjection` dataclass: `kind: str` (one of `faults.py`'s kinds), `at_attempt: int = 1`, `point: str` ("before-watch" | "mid-watch" after N polls | "on-diagnose"), `probability: float = 1.0`, optional `exit_code`, `message`.
- `ChaosPlan` dataclass + `from_dict()` (mirrors `Scheduling.from_dict`, `job.py:92-149`): list of `FaultInjection`, `seed: int | None` (deterministic RNG). Rejects unknown keys / bad kinds on *shape*.
- `FaultLedger` dataclass: append-only record of `(run_id, kind, attempt, point, realized: bool)`.
- `ChaosKubeClient` implementing the `KubeClient` Protocol (`kube.py:50-64`) by delegation: wraps an inner `KubeClient`, intercepts `list_jobs`/`list_pods` to rewrite snapshots into fault-shaped JSON at the planned point (same shapes as `make_job`/`make_pod`), appends to the `FaultLedger`. All other methods pass through verbatim. Deterministic via seeded RNG. Constructed only when `CHAOS_ENABLED`.

**`app/orchestrator/resilience.py`** (mechanism — report data model)
- `ResilienceReport` dataclass: `run_id`, `injected`, `recoveries` (joined to `RunOutcome.attempts` — each carries injected kind, classified kind, recovery action `retry|dead-letter|completed`, `classified_correctly: bool`, `recovered_as_designed: bool`), `restart: RestartProof | None`, `slo: {budget_s, elapsed_s, met: bool}`, `verdict_counts`.
- `build_resilience_report(outcome, ledger, restart, slo_budget_s, elapsed_s) -> ResilienceReport` — pure join. `to_dict()` for the tool result.

**`app/orchestrator/restart.py`** (mechanism — durability proof)
- `RestartProof` dataclass (facts).
- `async def prove_restart_recovery(kube, workspace, *, namespace, session_id=None, sweep_id=None, specs=None) -> RestartProof`: builds a *fresh* `BenchmarkOrchestrator`, calls `reconstruct()` / `reconstruct_sweep()` / `run_sweep(sweep_id=...)`, asserts no duplicate applies, returns the facts. Pure orchestration of existing methods.

**`app/tools/resilience.py`** (mechanism — the agent tool)
- `async def run_resilience_drill(ctx, *, namespace, spec=None, harness=None, workload=None, image=None, chaos_plan=None, prove_restart=True, slo_budget_s=600.0, ...)`:
  - Refuse with `ToolError` if `not ctx.settings.chaos_enabled` (production guard).
  - Parse `chaos_plan` via `ChaosPlan.from_dict` (ToolError on bad shape → agent self-corrects).
  - **Recommended first cut:** drive the drill against a deterministic in-process driver / fake even when enabled, so it never touches a real cluster. Drive `run_with_retries` (single) or `run_sweep` (with restart proof), then `prove_restart_recovery`, then `build_resilience_report`. Return `report.to_dict()`.
- Follows the `tools/CLAUDE.md` pattern: flat JSON dict, raises only `ToolError`.

**`knowledge/resilience.md`** (judgment) — on-demand (NOT CORE). How to read the resilience report; what correct classification of each fault means; which faults *should* retry (`evicted`/`unknown`) vs dead-letter (`oom`/`unschedulable`/`image`/`timeout`) — from `controller.py:50-53` + `knowledge/orchestrator.md:16-34`; how to interpret the restart proof; how to choose a `ChaosPlan`; the verdict framing. Cross-links `read_knowledge('orchestrator')`.

### Changed files

**`app/config.py`** — add after `orchestrator_service_account` (`config.py:128`):
```python
chaos_enabled: bool = False  # gate the chaos/resilience drill tool; OFF in production
```

**`app/tools/schemas.py`** — add `RunResilienceDrillInput(BaseModel)` (after `OrchestrateBenchmarkInput`, `schemas.py:364-420`): `namespace`, `spec/harness/workload/image`, `chaos_plan: dict | None` (described: list of `{kind, at_attempt, point, probability}`, point to `read_knowledge('resilience')`), `prove_restart: bool = True`, `slo_budget_s: float`.

**`app/tools/registry.py`** — add `_DESCRIPTIONS["run_resilience_drill"]` and a `ToolSpec(...)` in `build_registry()`; `from app.tools import resilience`.

**`app/agent/results_card.py`** — extend `build_results_card` (`results_card.py:33-45`): `if tool_name == "run_resilience_drill": return _card_from_resilience(result)`. Add `_card_from_resilience` (flat render model). Mechanism only.

**`ui/app.js`** — extend `renderResultsCard` (`ui/app.js:1276`) to render `card.kind === "resilience"` (injected-faults table + restart panel + verdict), reusing existing table/`slo-pass`/`slo-fail` CSS (`ui/app.js:1336`). Rides the existing `results_card` event.

**`app/observability/instrument.py`** (optional) — add `faults_injected_total` counter + `record_fault_injected(kind)` mirroring `record_run_outcome` (`:115-123`); decorator calls it via `_safe_metric` (swallow-all, like `controller.py:60-63`).

**Allowlist:** **No change.** A drill reuses the existing `kubectl apply/get/delete` surface; the chaos decorator only rewrites *read* responses in-process (issues no new commands).

### NOT changed (important)
`controller.py`, `faults.py`, `checkpoint.py`, `job.py`, `kube.py` — **untouched**. Whole feature is additive via the `KubeClient` seam + new modules + a new tool.

---

## 5. Hermetic test plan (reuse fakes; no cluster)

**`tests/test_chaos_injection.py`** — wrap `FakeKubeClient` in `ChaosKubeClient`, program a run, drive `run_with_retries`, assert:
- `evicted` @ a1 → classified `evicted` → retried `-a2` → succeeds.
- `oom`/`unschedulable`/`image_error`/`timeout` → classified correctly → dead-lettered, exactly 1 attempt.
- `FaultLedger` records what fired; `probability=0` injects nothing; seeded RNG reproducible.
- Determinism: chaos OFF (empty `ChaosPlan`) ⇒ byte-identical to a plain `FakeKubeClient` run.

**`tests/test_orchestrator_restart.py`** — durability proof:
- Single-run: `active` Job, drop orchestrator, fresh one on same `FakeKubeClient`, `reconstruct(session_id=...)` recovers it, `watch()` to succeeded.
- Sweep: reuse the `test_orchestrator_checkpoint.py:130-179` pattern through `prove_restart_recovery`; assert `RestartProof` reports k completed before, N-k after, **0 duplicate applies** (`_applied_run_ids(kube).count(t)==1`), checkpoint final = all N.
- Restart *during* an injected-fault run (combined).

**`tests/test_resilience_report.py`** — `build_resilience_report` joins ledger ⋈ outcome: `classified_correctly` true iff injected==classified; `recovered_as_designed` true iff recovery matches the rule; SLO `met` = elapsed ≤ budget; verdict counts correct.

**`tests/test_resilience_tool.py`** — end-to-end via `dispatch` + `RunResilienceDrillInput` + a `CaptureRunner`-backed `ToolContext` (copy `_ctx` from `test_orchestrator_tool.py:33-51`):
- `chaos_enabled=False` ⇒ `ToolError`, no cluster touched.
- `chaos_enabled=True` ⇒ report dict with the right keys (assert key presence per `tools/CLAUDE.md`).
- A bad `chaos_plan` shape returns `{"error": ...}` via dispatch.

**`tests/test_ui_frontend.py`** (extend) — assert the resilience card render branch exists.
**`tests/test_new_tools.py` / `test_schemas.py`** — register the new tool's schema.

Baseline to keep green: ~1650 passed.

---

## 6. Acceptance criteria, effort, risks

### Acceptance criteria
1. Chaos is unreachable from `orchestrate_benchmark_run` and OFF unless **both** `CHAOS_ENABLED=true` **and** `run_resilience_drill` invoked.
2. Each of `evicted/oom/unschedulable/image_error/timeout/run_error` can be injected and is classified correctly by the **unmodified** `classify_failure`.
3. `evicted`/`unknown` → retry to a fresh Job; deterministic kinds → dead-letter — via the **unmodified** `run_with_retries`.
4. A fresh orchestrator recovers an in-flight run via `reconstruct()` (labels only) and a partial sweep via the ConfigMap checkpoint with **0 duplicate Jobs**.
5. The resilience report (data + card) correctly cross-references injected ⋈ classified ⋈ recovery and states SLO met/missed.
6. `controller.py`/`faults.py`/`checkpoint.py`/`job.py`/`kube.py` unchanged; allowlist unchanged.
7. Full suite green; new tests hermetic.

### Effort: **M** (decorator + join + thin restart wrapper over existing methods; no new lifecycle logic).

### Risks & open questions
- **"Genuine vs theatre" for restart durability.** Honest model = object-discard + rehydrate against the same cluster state, exercising the real `reconstruct*` (already proven for sweeps). State this plainly in the report + `knowledge/resilience.md`. Optionally add an opt-in `tests/integration/` (gated like `LLMD_SIM_INTEGRATION`) that truly restarts the process against a real kind cluster — out of the hermetic default.
- **Drill against real vs fake.** First cut drives chaos against a fake/in-process driver even when enabled. A real-cluster chaos mode (wrapping `RealKubeClient`) is deferred; if added, injected faults must be approval-gated + clearly labeled.
- **Decorator rewriting reads vs cluster truth.** Keep the `FaultLedger` authoritative for "what we injected"; make rewrites deterministic (seeded).
- **Open:** SLO budget wall-clock vs attempt-count? (Recommend wall-clock `slo_budget_s`, consistent with `activeDeadlineSeconds`.)
- **Open:** expose `run_sweep`/`reconstruct` as standalone agent tools too? (Keep the drill self-contained for now.)

### Cleanest opt-in lever
The `KubeClient` Protocol seam (`kube.py:50-64`): production constructs `RealKubeClient` directly; chaos exists only as a decorator instantiated solely inside `run_resilience_drill`, double-gated by `CHAOS_ENABLED` + the named tool. No `if chaos:` ever enters `controller.py`.

### Critical files
- `app/orchestrator/kube.py` (the Protocol seam)
- `app/orchestrator/controller.py` (unchanged target: `run_with_retries`, `reconstruct`, `run_sweep`)
- `tests/orchestrator_fakes.py` (FakeKubeClient + make_job/make_pod)
- `app/tools/orchestrate.py` (pattern the new tool mirrors)
- `app/agent/results_card.py` (resilience card render model)
