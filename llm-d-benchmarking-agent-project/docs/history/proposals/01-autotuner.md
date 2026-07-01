# SPEC: Closed-loop Autotuner / Goal-Seeking Mode

**Status:** SHIPPED, then **REMOVED 2026-07-02** — goal-seeking now rides DoE sweeps + Pareto analysis (strategy in `knowledge/sweep_playbook.md`). Was implemented as the `autotune_search` tool (`app/tools/autotune.py` + `app/storage/autotune.py`), `knowledge/autotune_strategy.md`, the `AutotunePlan`/`AutotuneKnob` blocks in `app/validation/session_plan.py`, and `tests/test_autotune.py`. This spec is kept as a historical record (see `docs/history/proposals/README.md` and `FEATURES.md` for the live inventory).
**Invariants confirmed against:** root `CLAUDE.md`, `app/tools/CLAUDE.md`, `app/validation/CLAUDE.md`, `app/agent/CLAUDE.md`, `app/orchestrator/CLAUDE.md`, `knowledge/CLAUDE.md`

---

## 0. What exists today (grounding — every path verified)

The whole loop the feature needs already exists *as discrete pieces*; the only missing piece is the **result → next-config feedback step** and the **search-state tracker**. Concretely:

- **DoE generator** — `generate_doe_experiment` tool (`app/tools/doe.py:144`), backed by pure cross-product mechanism `build_doe_experiment` (`app/validation/doe.py:194`). The agent supplies `run_factors`/`setup_factors` (each `{name, key, levels}` — schema `DoEFactor` at `app/tools/schemas.py:683`); it cross-products them into a treatments matrix and writes a structurally-validated experiment YAML to the workspace. **This is a static, full-grid generator** — it expands *all* levels upfront. There is no "give me the next single point" entry.
- **Run execution** — two paths: `execute_llmdbenchmark` (`app/tools/execute.py:547`, returns `results_dir`) drives the local CLI (`run`/`experiment` subcommands); `orchestrate_benchmark_run` (`app/tools/orchestrate.py:71`) drives a single K8s Job. The orchestrator *also* has a fully-built **parallel sweep** path `BenchmarkOrchestrator.run_sweep` (`app/orchestrator/controller.py:350`) with checkpoint/resume — **but it is NOT exposed as an agent tool** (verified: no registry entry). Today sweeps go through the CLI `experiment`/`run --experiments` path.
- **Results analyzer** — `analyze_results` tool (`app/tools/analyze.py:88`) → pure math in `app/validation/analysis.py`: `evaluate_slo` (`:181`, per-run SLO verdict + bounded goodput estimate), `pareto_analysis` (`:378`, frontier + `slo_feasible` + `slo_frontier`). SLO contract is `SLOTargets` (`app/validation/analysis.py:69`) — exactly the goal-seeking target object, already embedded in `SessionPlan.slo` (`app/validation/session_plan.py:20`).
- **History/trend store** — `HistoryStore` (`app/storage/history.py:127`) persists validated summaries cross-session (content-addressed, idempotent), with `trend()` math. Tool: `result_history` (`app/tools/history.py`).
- **Tool registry** — `ToolSpec` + `build_registry()` + `dispatch()` (`app/tools/registry.py:76,449,508`). Adding a tool = handler + Pydantic schema + `ToolSpec` row + `_DESCRIPTIONS` entry.
- **Agent loop** — `app/agent/loop.py:34` (`run_turn`): LLM → validated dispatch → approval-gated exec → feed result back, `MAX_STEPS = 24` (`:23`). Plan approval wired at `:171` (`propose_session_plan` → `session.approved_plan`).
- **SessionPlan approval** — `propose_session_plan` (`app/tools/plan.py:12`) validates against live catalog then routes through `ctx.request_approval`.
- **Results card UI** — deterministic card built in `app/agent/results_card.py:33` from `analyze_results` output, emitted as `RESULTS_CARD` event (`app/agent/events.py:87`), rendered in `ui/app.js`.

**Tunable knobs available today** (the autotuner's search space — all already expressible as DoE factor `key`s, grounded in knowledge):
- **Workload/run knobs** (run-parameter sweep, one stack): `max-concurrency`, `rate`/QPS, `data.shared_prefix.num_groups`, `system_prompt_len`, prompt/output token lengths (`knowledge/sweep_playbook.md:88-110`).
- **Deployment knobs** (full-DoE, re-deploys): `decode.replicas`, `prefill.replicas`, `decode.parallelism.tensor`, model, prefill/decode split (`knowledge/sweep_playbook.md:68-81`).
- **vLLM/scenario knobs**: `vllmCommon.flags.*` (enforceEager, noPrefixCaching…), `vllmCommon.kvTransfer.*`, `schedulerName`, affinity (`knowledge/vllm_overrides.md:76-118`), authored via `write_and_validate_config(artifact_type="scenario")`.

---

## 1. Goal & user-facing behavior

The user states a goal in plain English; the agent runs a benchmark, reads the schema-validated result, **picks the next config itself**, reruns, and converges on the SLO-feasible Pareto point that best serves the stated objective — narrating each iteration. The only structural change vs today is that the **next config is chosen from the last result** instead of from a static grid.

### Sample transcript

> **User:** Benchmark my chat app — hit p95 TTFT under 300 ms at the highest output-token throughput you can. Spend at most 6 runs.
>
> **Agent:** I'll treat this as a goal-seeking search. Goal: maximize output-token throughput subject to **TTFT p95 ≤ 300 ms**. The knob I'll tune is request concurrency (one stack stood up once, re-benchmarked per step — cheapest on kind/CPU-sim). Budget: 6 trials. Here's the plan to approve — it covers the standup plus a bounded autotune of up to 6 runs.
> *[emits `propose_session_plan` with `slo={ttft_ms:300, percentile:"p95"}`, `autotune` block: strategy=coordinate-descent, knob=`max-concurrency`, budget=6]*
>
> **User:** *[approves]*
>
> **Agent:** Stack is up. **Trial 1/6** — concurrency=8 (midpoint start). *[runs → reads report]* TTFT p95 = 140 ms (✅ under 300), output 210 tok/s. Headroom on latency; the strategy says push load up. **Trial 2/6** — concurrency=16. *[runs → reads]* TTFT p95 = 250 ms (✅), output 340 tok/s. Still feasible and throughput climbed — keep going. **Trial 3/6** — concurrency=32. *[runs → reads]* TTFT p95 = 470 ms (❌ over 300), output 390 tok/s. Overshot the SLO; the feasible point is between 16 and 32. **Trial 4/6** — concurrency=24. *[runs → reads]* TTFT p95 = 305 ms (❌, barely). **Trial 5/6** — concurrency=20. *[runs → reads]* TTFT p95 = 285 ms (✅), output 365 tok/s.
> Improvement over the last feasible point (340→365 tok/s) is under my 5% convergence threshold and I've bracketed the SLO boundary — **converged**.
> **Result:** best SLO-feasible config = **concurrency=20** — output **365 tok/s** at TTFT p95 **285 ms**. The Pareto frontier across all 5 trials, with the feasible subset, is below. *[RESULTS_CARD: convergence view]* Want me to save this as your baseline, or push past the SLO to see the throughput ceiling?

**Key behaviors:** one upfront approval covers the bounded search; each trial reuses the existing run + analyze tools; the stop decision and next-config choice are the agent's reasoning over an explicit strategy spec; every number comes from a schema-validated BR v0.2 report (never logs).

---

## 2. Architecture — mechanism vs judgment

The non-negotiable: **search strategy is judgment, lives in `knowledge/`, driven by the LLM.** Python is *only* mechanism: track the trial history, validate that a proposed next-candidate is well-formed and inside declared bounds/budget, and surface a structured view. **No `if/elif` decides the next config or whether to stop.**

### The loop, structurally

The closed loop is **the existing agent loop** (`app/agent/loop.py`) — we do NOT build a second loop. Each "trial" is the model deciding, within one user turn (or across turns), to: pick a config → run it → analyze it → record it → reason about the next config. The new tools are *state + validation mechanism* the model leans on so it doesn't have to hold trial history in its context window or hand-roll the search bookkeeping.

```
[approved SessionPlan w/ slo + autotune budget]
        │
        ▼   (agent reasoning, grounded in knowledge/autotune_strategy.md)
  pick next config ──► run it (execute_llmdbenchmark OR orchestrate_benchmark_run, REUSED)
        ▲                       │
        │                       ▼
  record_trial  ◄── analyze_results (REUSED: SLO verdict + goodput + Pareto)
   (new: state)            │
        │                  ▼
  propose_next_config ──► (new: VALIDATES the agent's candidate; tracks budget;
   (new: mechanism)        returns frontier-so-far + budget-remaining FACTS)
        │
        ▼
  agent reads facts + knowledge/autotune_strategy.md → decides converge / continue
```

### Division of responsibility (explicit)

| Concern | Mechanism (Python) | Judgment (LLM + `knowledge/`) |
|---|---|---|
| Which knob(s) to tune, search space bounds | validate the candidate is within declared bounds; reject out-of-bounds | **picks** the knob(s) and bounds from the use case (`knowledge/autotune_strategy.md` + `knowledge/sweep_playbook.md`) |
| Which strategy (hill-climb / coordinate-descent / bisection / simple Bayesian-ish) | none — strategy is *named* in the plan and *described* in knowledge | **chooses + executes** the strategy by reasoning; the strategy spec is a knowledge doc, not code |
| Next config value | validate it's well-formed, not a duplicate of a prior trial, within budget | **computes** the next value (e.g. "bisect between last feasible and last infeasible") |
| SLO contract | `SLOTargets` model + `evaluate_slo` (EXISTING) | which targets, which percentile (`knowledge/analysis.md`) |
| "Converged / stop?" | expose FACTS: trials used vs budget, current SLO-feasible frontier, last-N improvement delta, whether the SLO boundary is bracketed | **decides** to stop using a convergence rubric in `knowledge/autotune_strategy.md` |
| Trial bookkeeping | append-only trial log keyed to the session; compute frontier-so-far by reusing `pareto_analysis` | interpret it |

### How the SLO contract is represented & checked

Reuse `SLOTargets` (`app/validation/analysis.py:69`) verbatim — it's already in `SessionPlan.slo`. Each trial's result is checked by the **existing** `evaluate_slo`/`pareto_analysis`; the autotuner never re-implements SLO math. The plan's `slo` is the single source of truth for the goal's constraint; the plan's new `autotune` block names the *objective* to maximize/minimize subject to that constraint (e.g. `objective: output_token_rate, direction: max`).

### How "converged / stop" is decided

The Python tracker returns **facts only**: `trials_used`, `budget_remaining`, `slo_feasible_frontier` (via reused `pareto_analysis`), `best_feasible` (the incumbent), and `recent_improvement_pct` (objective delta over the last K feasible trials). The **agent** applies the stop rubric from `knowledge/autotune_strategy.md` (e.g. "stop when: budget exhausted; OR improvement < threshold for 2 consecutive feasible trials; OR the SLO boundary is bracketed and the bisection gap is below the knob's resolution; OR no feasible point exists after exhausting the bounded range — report nearest miss"). This keeps the convergence *decision* out of Python entirely.

---

## 3. Exact new/changed files

### New tool: `app/tools/autotune.py` (mechanism — the search-state tracker)

One tool, action-dispatched (mirrors `result_history`'s single-tool/multi-action shape at `app/tools/history.py:100`). Auto-runs (read-only w.r.t. cluster/repos — it only reads/writes a JSON trial log under `ctx.workspace`, like the history store). **Contains no benchmarking decision logic.**

**Name:** `autotune_search` · **Schema:** `AutotuneSearchInput` in `app/tools/schemas.py`.

**Actions & I/O sketch:**

```
action="record_trial"
  in:  search_id: str            # the agent picks a stable id per goal-seeking session
       config: dict[str,Any]     # the knob values used this trial, e.g. {"max-concurrency": 16}
       report_source: str        # run dir / report file produced this trial (validated path)
  does: load+validate BR v0.2 (REUSE app.validation.report.load_report/validate_report);
        summarize_report; evaluate_slo against the plan's SLOTargets (REUSE); append a
        Trial{config, summary, slo_eval, objective_value, feasible} to <ws>/autotune/<search_id>.json
  out: {recorded: bool, trial_index, feasible: bool, objective_value, slo: <evaluate_slo out>,
        note: "now reason about the next config per knowledge/autotune_strategy.md"}
        # rejects an unvalidated report (determinism gate d) — never records a log-scraped number

action="propose_next_config"     # VALIDATION + budgeting only — does NOT compute the value
  in:  search_id: str
       candidate: dict[str,Any]  # the value the AGENT computed for the next trial
  does: load trial log; check candidate is well-formed, within declared bounds (from the plan's
        autotune block), not a duplicate of a prior trial, and budget not exhausted
  out: {ok: bool, reason?: str, budget_remaining: int, duplicate_of?: int,
        out_of_bounds?: [...], note: "if ok, run this config next; if not, revise it"}

action="status"                  # the convergence-fact surface (FACTS, no verdict)
  in:  search_id: str
  does: REUSE pareto_analysis over all recorded trials' summaries + the plan's SLOTargets
  out: {trials_used, budget_remaining, objective, direction,
        best_feasible: {config, objective_value, slo_eval} | null,
        slo_feasible_frontier: [...],        # from pareto_analysis (REUSED)
        frontier: [...],
        recent_improvement_pct: float | null, # objective delta over last K feasible trials
        slo_boundary_bracketed: bool,         # FACT: do we have ≥1 feasible AND ≥1 infeasible trial
        note: "decide converge/continue using knowledge/autotune_strategy.md — this is facts only"}
```

> **Thin-code tripwire (flagged):** it is tempting to put the next-config *computation* (bisection midpoint, gradient step) into `propose_next_config`. **Do not.** That is the search strategy = judgment. Python only *validates* the agent's candidate. `slo_boundary_bracketed` and `recent_improvement_pct` are facts, not a stop decision — Python must never return `converged: true`. If a reviewer sees an `if recent_improvement < threshold: converged` in this file, it's a violation; the threshold lives in `knowledge/autotune_strategy.md` and the decision is the LLM's.

**State storage:** `<workspace>/autotune/<search_id>.json` — an append-only trial log (same defensive best-effort I/O pattern as `HistoryStore`, `app/storage/history.py`). Consider a thin `app/storage/autotune.py` module for the pure trial-log read/append/frontier math (keeps `autotune.py` tool as wiring, mirroring the `history.py` tool ↔ `storage/history.py` split). **(S–M)**

### Schema: `app/tools/schemas.py`

- `AutotuneSearchInput(BaseModel)` — `action: Literal["record_trial","propose_next_config","status"]` plus the per-action fields above. Every `Field(description=...)` points the model at `read_knowledge('autotune_strategy')` for the judgment. **(S)**
- Extend `SessionPlan` (`app/validation/session_plan.py:17`) with an **optional** `autotune: AutotunePlan | None`:
  ```
  class AutotunePlan(BaseModel):
      strategy: str               # NAME only, e.g. "coordinate-descent" — described in knowledge, not enumerated-as-logic
      objective: str              # one of the analyzer objective names (output_token_rate, ttft, ...)
      direction: Literal["max","min"]
      knobs: list[AutotuneKnob]   # each: {name, key (dotted, reuses DoEFactor.key rules), min, max, resolution?}
      budget: int = Field(ge=1, le=…)   # max trials — the bounded search budget for ONE approval
  ```
  This rides the **existing** plan-approval path (`app/tools/plan.py`, loop `:171`) — so the bounded search gets **one upfront approval**, not per-trial. `knobs[].key` reuses the dotted-key validation already in `app/validation/doe.py:_KEY_RE`. **(S)**

### Registry wiring: `app/tools/registry.py`

- `_DESCRIPTIONS["autotune_search"]` — describes the three actions, says explicitly "this tool tracks search state and validates YOUR candidate; the strategy and the stop decision are yours, grounded in `read_knowledge('autotune_strategy')`."
- One `ToolSpec("autotune_search", …, AutotuneSearchInput, autotune.autotune_search)` row in `build_registry()`. **(S)**

### New knowledge: `knowledge/autotune_strategy.md` (judgment — the heart of the feature)

On-demand (NOT CORE — goal-seeking is mid/late-session; CORE inflates every prompt per `knowledge/CLAUDE.md`). Contents:
- **When to use goal-seeking** vs the static DoE grid (`generate_doe_experiment`): use it when the user states a *goal* ("hit X at best Y") rather than "compare these N configs."
- **The strategies, described as procedures** (so the LLM executes them): coordinate-descent (tune one knob to its knee, fix it, move to the next), hill-climb (step toward improvement, halve step on overshoot), bisection (when bracketing an SLO boundary on a monotone knob — the chat-app TTFT case), and a simple model-free "Bayesian-lite" (sample spread, then exploit the best region). Cross-reference `knowledge/sweep_playbook.md` for which knob is a run-param vs deployment factor, and `knowledge/vllm_overrides.md` for scenario knobs.
- **Picking the start point & step** from the use case (interactive → start low concurrency; batch → start mid and push).
- **The convergence rubric** (the stop conditions listed in §2) — written as judgment, e.g. "treat `recent_improvement_pct` < ~5% across 2 feasible trials as diminishing returns."
- **The honest-goodput caveat** (carry forward from `knowledge/analysis.md:33`): goodput is an estimate; never claim the converged point is exact-optimal — it's the best *found* under the budget.
- **Narration cadence** (one trial-summary line per step; cross-ref `knowledge/conversation_style.md`).

Cross-cue: add one line to `knowledge/sweep_playbook.md` and `knowledge/analysis.md` ("for *goal-seeking* (adaptive next-config), see `read_knowledge('autotune_strategy')`"). **(M — this doc is where the real design effort goes.)**

### Allowlist: `security/allowlist.yaml` — **NO CHANGES**

The autotuner reuses the existing run path (`execute_llmdbenchmark` / `orchestrate_benchmark_run`), whose mutating commands are already allowlisted and approval-gated. `autotune_search` only reads/writes the session workspace (no cluster, no repos) → auto-runs, no allowlist entry. **(none)**

### UI: `ui/app.js` (+ `ui/styles.css`) — convergence/iteration view

Extend the deterministic results-card path:
- `app/agent/results_card.py:build_results_card` (`:33`) gains a branch for `autotune_search` (action=`status`): build an **autotune convergence card** — a trial-by-trial table (trial #, config, objective value, SLO ✅/❌), the incumbent `best_feasible`, the `slo_feasible_frontier`, and budget used. Pure reshaping of already-computed facts (same contract as the existing analyze card — no fabricated numbers).
- Wire it into the loop's card emit (`app/agent/loop.py:191` already calls `build_results_card` after every tool result and emits `RESULTS_CARD`) — **no loop change needed**, just the card builder learns the new tool name.
- `ui/app.js`: a `renderAutotuneCard` renderer (mirror `renderReportSummary`/analysis card); a small convergence list (trials as rows, feasible-frontier highlighted). `ui/styles.css`: minor styles. **(M)**

### SessionPlan implications — **one upfront approval for the bounded budget**

The `autotune` block (`budget`, `knobs`, bounds) is part of the approved plan, so the user authorizes "up to N trials within these bounds" **once**. The per-trial *runs* still go through `execute_llmdbenchmark`/`orchestrate_benchmark_run`, which carry their own per-command approval gate — but the autotune narration should set expectations so the user can approve runs quickly. This matches the existing model: the SessionPlan is the contract, the budget bounds it. **No new approval channel.** **(S)**

### Loop: `app/agent/loop.py` — likely **NO structural change**

The existing loop already feeds tool results back and supports multi-step reasoning. One concern: `MAX_STEPS = 24` (`:23`) bounds steps *per user turn*; a 6-trial search × (run + analyze + record + propose ≈ 4 tool calls) ≈ 24 — right at the ceiling. **Mitigation (no code change preferred):** the agent narrates trial-by-trial and naturally yields the turn between trials (the user watches/approves), so the search spans multiple turns. Raising `MAX_STEPS` or making it autotune-budget-aware is a *mechanism* tweak — open question, not a v1 requirement. **(S, deferred)**

---

## 4. How it reuses (not reinvents) the existing pieces

- **DoE generator:** Reused conceptually for knob/key vocabulary and dotted-key validation (`_KEY_RE`, `DoEFactor` rules). The autotuner does **not** call `generate_doe_experiment` per trial (that's a full grid); each trial is a single config. **Optional reuse:** for a Bayesian-lite "sample then exploit" initial spread, the agent MAY call `generate_doe_experiment` for the initial sample batch and then switch to adaptive single trials.
- **Pareto/goodput analyzer:** Reused verbatim. `record_trial` calls `evaluate_slo`; `status` calls `pareto_analysis` over the accumulated trial summaries (`app/validation/analysis.py:378`). Zero new Pareto math — the autotuner's "best feasible point" IS the analyzer's.
- **Orchestrator sweep:** The single-run trials reuse `orchestrate_benchmark_run` / `execute_llmdbenchmark`. The orchestrator's parallel `run_sweep` (`controller.py:350`, currently unexposed) is the natural backend if a future strategy wants to evaluate a *batch* of candidate configs in parallel per iteration. **v1 keeps it serial.**
- **History store:** After convergence, the agent uses the existing `result_history` `store` action to persist the winning config's report. No new persistence for results; the autotune trial log is *ephemeral search state* in the workspace.

---

## 5. Hermetic test plan

All tests use existing fakes — no cluster, no GPU, no live LLM. Convergence is tested deterministically against a **synthetic result surface**: a pure function `config → BR v0.2 report` written to a temp dir, then driven through the real tools.

**New: `tests/test_autotune.py`**

1. **Synthetic result surface.** Reuse `tests/test_analyze.py:_write_report` pattern + `br_example` fixture (`conftest.py:44`) + `tool_ctx` (`conftest.py:54`). Define a monotone surface, e.g. `ttft_p95(c) = 60 + 13*c` ms, `out_rate(c) = 420*c/(c+20)` tok/s — with a known SLO crossing at `c≈18` for a 300 ms target.
2. **`record_trial` validates & records.** Assert it loaded+validated the report (reuses `validate_report`), computed `feasible` from real `evaluate_slo`, appended to the log. Negative: schema-invalid report → `recorded: false`, never stored (gate d).
3. **`propose_next_config` is pure validation.** Out-of-bounds → `ok:false, out_of_bounds`. Duplicate → `ok:false, duplicate_of`. Budget exhausted → `ok:false, budget_remaining:0`. Valid in-bounds non-dup → `ok:true`.
4. **`status` returns facts (no verdict).** After a feasible + an infeasible trial: `slo_boundary_bracketed:true`, `best_feasible` correct, `slo_feasible_frontier` matches a direct `pareto_analysis` call, and **assert NO `converged`/`stop` key** (guards the thin-code invariant).
5. **Deterministic convergence simulation (headline test).** A small driver simulates the *agent's* coordinate-descent over the synthetic surface (the test plays the LLM's strategy role): start `c=8`; double until bracketed; bisect; stop when gap ≤ resolution. Drive each step through `record_trial`/`propose_next_config`/`status`. Assert convergence to `c∈[16,20]` within ≤6 trials and final `best_feasible` is the highest-throughput feasible trial.
6. **Plan schema.** `SessionPlan` accepts `autotune`; `AutotunePlan` rejects budget<1, out-of-order bounds, empty knobs; validates against the live catalog unchanged.
7. **Results card.** `build_results_card("autotune_search", status_result)` returns a well-formed convergence card; `None` for non-status/empty log.
8. **Registry/dispatch.** Tool registered; schema validation errors **returned not raised** (per `app/tools/CLAUDE.md`); result dict keys asserted.

**No new fakes needed** — `tool_ctx` + `br_example` + the `_write_report` pattern cover everything.

---

## 6. Acceptance criteria, effort, risks, open questions

### Acceptance criteria
1. A user can state a goal in English; the agent proposes ONE plan (with `slo` + bounded `autotune` budget) approved once.
2. The agent runs an adaptive sequence where **each next config is chosen from the prior result** (verifiable in the transcript), not a pre-expanded grid.
3. Every per-trial number comes from a schema-validated BR v0.2 report via the reused analyzer; the converged "best feasible" point is the analyzer's `slo_frontier` pick.
4. The search respects budget and bounds (Python-enforced) and stops on a convergence condition the agent reasons about (knowledge-driven), reporting the incumbent or the nearest miss if infeasible.
5. A convergence/iteration card renders in the UI.
6. `pytest tests/` stays green (baseline ≈1650 passed) with new hermetic tests; no live cluster/LLM.
7. **Invariant audit:** no next-config computation or stop-decision in any `.py` file; the strategy + convergence rubric live entirely in `knowledge/autotune_strategy.md`.

### Effort: **M** (dominant cost = the knowledge doc + the UI card, not Python)

### Risks
- **Thin-code erosion (highest risk).** Pressure to put bisection/hill-climb math in `propose_next_config`. Mitigation: tool only validates + exposes facts; test #4 asserts no `converged` key; code review for step-arithmetic.
- **`MAX_STEPS=24` ceiling** for a long single-turn search. Mitigation: multi-turn narration in v1.
- **Non-monotone/noisy real surfaces** — naive hill-climb can thrash. Mitigation: knowledge prescribes step-halving + budget cap; claim "best found", not "global optimum".
- **Goodput is an estimate.** Narration must say so.
- **Per-trial approval friction.** Mitigation: one-approval-for-the-budget plan + clear narration.

### Open questions
1. Parallel batch trials via `run_sweep` — expose for Bayesian-lite spread, or keep v1 serial? (Recommend serial v1.)
2. Multi-knob coordinate-descent in v1, or single-knob first? (Recommend single-knob; schema supports multiple.)
3. Auto-feed the trial log to `HistoryStore` on convergence, or only on explicit save? (Recommend explicit.)

### Critical files
- `app/tools/autotune.py` *(new — search-state tracker)*
- `knowledge/autotune_strategy.md` *(new — ALL search/convergence judgment)*
- `app/validation/analysis.py` *(reused verbatim: `SLOTargets`, `evaluate_slo`, `pareto_analysis`)*
- `app/tools/schemas.py` *(new `AutotuneSearchInput`; `AutotunePlan`/`AutotuneKnob`)*
- `app/validation/session_plan.py` *(optional `autotune` block)*
