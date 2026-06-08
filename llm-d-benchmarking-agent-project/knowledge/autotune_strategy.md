# Autotuning / goal-seeking strategy (closed-loop search)

Use this when the user states a **goal** — "hit p95 TTFT under 300 ms at the best
output-token throughput you can", "find the most concurrency I can run while staying under
1 s end-to-end", "maximize throughput without breaking my latency SLO" — rather than asking
to **compare a fixed set of configs**. A goal means: *optimize one objective subject to an
SLO constraint, choosing each next config from the last result.* That is goal-seeking; the
mechanism is the `autotune_search` tool plus your reasoning over THIS doc.

**The division of labor is absolute.** `autotune_search` is pure mechanism: it tracks the
trial log, *validates* the candidate you computed (bounds / duplicate / budget), and returns
*facts* (incumbent, SLO-feasible frontier, budget remaining, recent improvement, whether the
SLO boundary is bracketed). **It never computes the next config and never tells you to stop.**
You pick the next config and you decide to stop, using the procedures and the rubric below.

## Goal-seeking vs the static DoE grid

- **Goal-seeking (this doc, `autotune_search`).** The user has a *target* and wants the best
  operating point — you don't know the answer up front, so you search adaptively: run → read
  the validated result → pick the next config from it → repeat → converge. One upfront plan
  approval covers the whole bounded search.
- **Static DoE sweep (`generate_doe_experiment` + `read_knowledge('sweep_playbook')`).** The
  user already knows the N configs they want compared ("compare tp=1,2,4"). Expand the full
  grid once and run it; no adaptation. Use this when the question is "which of these?", not
  "find the best".
- You may **combine** them: for a "Bayesian-lite" search, use `generate_doe_experiment` for a
  small *initial spread* (e.g. 3–4 well-separated points), record those as trials, then switch
  to adaptive single trials in the most promising region.

## Set up the search in the SessionPlan (one approval)

Goal-seeking rides the **existing** plan-approval path. Put it in the plan's `autotune` block
so the user authorizes the *bounded* search ONCE (not per trial):

- `slo` (the plan's normal SLO block) — the **constraint** every trial is judged against
  (e.g. `{ttft_ms: 300, percentile: "p95"}`). This is the source of truth for feasibility;
  `autotune_search` evaluates each trial with the SAME analyzer the rest of the agent uses.
- `autotune.objective` + `autotune.direction` — what to **optimize** subject to the SLO
  (e.g. `output_token_rate` / `max`, or `request_latency` / `min`).
- `autotune.strategy` — the NAME of the procedure you'll run (below). A label only.
- `autotune.knobs` — the knob(s) you'll tune, each with a dotted `key` and `[min, max]`
  bounds (and an optional `resolution`). WHICH knob is judgment — see below + `sweep_playbook`.
- `autotune.budget` — the max trials this one approval spends. Be honest in your narration so
  the user can approve the per-trial runs quickly.

Per-trial runs still go through `execute_llmdbenchmark` / `orchestrate_benchmark_run` and
their normal per-command approval — the autotuner runs nothing itself.

## Pick the knob and its bounds (judgment)

Reuse the sweep playbook's knob taxonomy (`read_knowledge('sweep_playbook')`):

- **Run/workload knobs — PREFER these (esp. on kind/CPU-sim): one stack, re-benchmarked per
  trial.** `max-concurrency`, `rate`/QPS, prompt/output token lengths,
  `data.shared_prefix.num_groups`. Cheapest closed loop — stand up once, run N times.
- **Deployment knobs — expensive (each trial re-deploys).** `decode.replicas`,
  `prefill.replicas`, `decode.parallelism.tensor`, model, prefill/decode split. Only when a
  run-knob can't express the goal; keep the budget small.
- **vLLM/scenario knobs** (`read_knowledge('vllm_overrides')`): `vllmCommon.flags.*`,
  `vllmCommon.kvTransfer.*`, `schedulerName`, affinity — authored via
  `write_and_validate_config(artifact_type="scenario")`.

Set bounds from the use case, not arbitrarily: a sane low end (e.g. concurrency 1–4), a high
end you believe will breach the SLO (so the search can *bracket* the boundary), and a
`resolution` equal to the smallest step worth distinguishing (e.g. `1` for integer
concurrency, so you stop bisecting once the gap is ≤ 1).

## The strategies (procedures you execute)

You run these by reasoning — there is no strategy code. For each step: compute the next
config, call `autotune_search(action="propose_next_config", candidate=...)` to validate it,
run it, then `autotune_search(action="record_trial", ...)`, then read
`autotune_search(action="status")` and apply the rubric.

### Bisection — the monotone single-knob SLO-boundary case (the chat-app TTFT classic)
When one knob moves the SLO metric monotonically (more concurrency → higher TTFT) and you
want the *most* load that still meets the SLO:
1. Start at a midpoint (or low). Run, record.
2. **Bracket:** push the knob up (e.g. double it) until a trial goes *infeasible*. Now you
   have a feasible low and an infeasible high — `status.slo_boundary_bracketed` becomes true.
3. **Bisect:** next config = the integer midpoint between the highest feasible and the lowest
   infeasible knob value. Run, record. The new result replaces one end of the bracket.
4. Repeat until the bracket gap ≤ the knob's `resolution` — then the highest feasible point IS
   the answer (it's the analyzer's SLO-feasible frontier pick).

### Coordinate-descent — multi-knob
Tune ONE knob to its knee (via bisection/hill-climb above), *fix it at the best feasible
value*, then move to the next knob and repeat. Cheap, interpretable; v1 default for >1 knob.

### Hill-climb — noisy / non-monotone surfaces
Step in the improving direction; on an *overshoot* (the objective got worse, or you crossed
the SLO), **halve the step** and reverse. Stop when the step is below `resolution` or you've
spent the budget. Robust to mild noise without a model.

### Bayesian-lite — wide, unknown space
Sample a small spread first (use `generate_doe_experiment` for 3–4 separated points, record
each), find the best *feasible region* from `status`, then exploit it with adaptive single
trials (hill-climb/bisection) in that region. Use when no knob is obviously monotone.

## Start point & step from the use case

- **Interactive / chat** → start at *low* concurrency (responsiveness matters); push up to
  find the latency ceiling.
- **Batch / offline / cost** → start at a *mid* point and push load up hard (throughput is the
  goal; latency is loose) until the SLO (if any) bites.
- First step ≈ a doubling when bracketing an unknown boundary; switch to bisection once
  bracketed; halve on any overshoot.

## The convergence rubric (YOUR decision, not the tool's)

`autotune_search(action="status")` gives you FACTS — `trials_used`, `budget_remaining`,
`best_feasible`, `slo_feasible_frontier`, `recent_improvement_pct`, `slo_boundary_bracketed`.
**Stop the search when any of these holds** (you decide; Python never returns a verdict):

- **Budget exhausted** — `budget_remaining == 0`. Report the incumbent.
- **Diminishing returns** — `recent_improvement_pct` is below ~5% across **2** consecutive
  feasible trials. The objective has flattened; more trials aren't worth it.
- **Boundary resolved** — `slo_boundary_bracketed` is true AND the bisection gap between the
  best feasible and the nearest infeasible knob value is ≤ the knob's `resolution`. You've
  pinned the SLO crossing; the highest feasible point is the answer.
- **No feasible point** — you exhausted the bounded range and *no* trial met the SLO. Report
  the **nearest miss** (the closest-to-feasible trial) and say plainly that the goal isn't
  reachable in these bounds; offer to relax the SLO or widen the bounds.

The 5% threshold and "2 trials" are guidance — adjust for a noisy surface (require a clearer
trend) and narrate why you're stopping.

## The honest-goodput caveat (carry this forward)

Goodput is an **estimate** bounded from the reported percentiles, not an exact per-request
fraction (`read_knowledge('analysis')`). So is the "best" you converge on: it is the best
point **found under this budget on this surface**, NOT a proven global optimum. Never claim
the converged config is exactly optimal — say "best found within N trials" and that the
numbers come from schema-validated reports. If the surface is noisy, say so.

## Narration cadence (`read_knowledge('conversation_style')`)

One short trial-summary line per step — config, the SLO verdict (✓/✗), the objective value,
and the next move ("feasible with headroom → push load up", "overshot → bisect between 16 and
32"). When you stop, give the result in one breath: the best feasible config, its objective
value and SLO metric, that it's the best *found* under the budget, and the convergence card
(the trial table + feasible frontier). Then make ONE next offer (save as baseline, or push
past the SLO to find the ceiling) — not a menu.
