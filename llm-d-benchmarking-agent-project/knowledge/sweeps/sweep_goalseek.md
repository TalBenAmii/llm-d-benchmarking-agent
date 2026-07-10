# Goal-seeking: converge on the best config meeting an SLO

Use this when the user states a **goal** — "hit p95 TTFT under 300 ms at the best output-token
throughput you can", "find the most concurrency I can run while staying under 1 s end-to-end" —
rather than asking to compare a fixed set of configs. A goal means: *optimize one objective
subject to an SLO constraint.* There is no dedicated search tool; you converge by running
**rounds of the same sweep + analyze machinery** (`read_knowledge('sweep_authoring')` to author a
round, `read_knowledge('sweep_results')` / `read_knowledge('analysis')` to read it), narrowing the
grid each round. The judgment (what to sweep, when to stop) is yours.

## Goal-seek vs a one-shot sweep
- **One-shot sweep:** the user already knows the N configs ("compare tp=1,2,4") — expand the
  grid once, run it, compare. No iteration.
- **Goal-seek (this file):** the user has a *target* and wants the best operating point — you
  don't know the answer up front, so you iterate: coarse round → read the frontier → narrow →
  re-sweep.
- If the plausible range is narrow enough that ONE grid at the resolution you care about covers
  it (a handful of treatments), just run the one-shot sweep — iterate only when the space is too
  wide to grid affordably in one round.

## Agree the run budget FIRST
Before the first round, agree a **max total-run budget** with the user ("I'll spend at most 12
benchmark runs finding this") and honor it across ALL rounds: count every treatment you run, keep
the running total in your narration ("run 7 of 12"), and stop when it's spent — even mid-plan.
Never start an unbounded loop.

## The iterate loop
1. **Coarse round.** Pick ONE knob (prefer run/workload knobs — cheapest loop; deployment knobs
   only when a run knob can't express the goal) and 3–4 well-separated levels around the starting
   point, chosen to *straddle* the SLO boundary (a sane low end plus a high end you believe will
   breach it). Start point from the use case: interactive/chat → start low and push load up;
   batch/offline → start mid and push hard. → `generate_doe_experiment`, then
   `execute_llmdbenchmark` (run-parameter sweep) or `orchestrate_sweep`, `dry_run: true` first.
2. **Analyze.** `analyze_results` with the plan's `slo` → per-run SLO verdicts, goodput, and the
   SLO-feasible frontier. The **incumbent** = the best feasible point so far (the analyzer's
   `slo_frontier` pick).
3. **Narrow.** Next round's levels bracket the incumbent — between the best feasible level and the
   nearest infeasible one (where the SLO crossing must sit), at finer resolution. 2–3 levels
   usually suffice.
4. **Repeat** until a stop rule fires.

## Never re-run a tried treatment
Before each round, check what has already been run — this session's prior sweep treatments and
`result_history` for earlier runs on the same stack/model/workload — and drop duplicates from the
new grid. A repeated treatment spends budget to learn nothing; re-run one only if you suspect the
first result was noisy, and say that's why.

## Stop rules (your call — narrate why)
- **SLO met with margin + plateau** — the incumbent meets the SLO and the objective/goodput
  improved less than ~5% across the last round: the surface has flattened; recommend the incumbent.
  (Adjust the threshold up for a noisy surface and say so.)
- **Boundary resolved** — the gap between the best feasible and the nearest infeasible level is at
  or below the smallest step worth distinguishing (e.g. 1 for integer concurrency): the incumbent
  IS the answer.
- **Budget exhausted** — recommend the incumbent as the best found within N runs.
- **No feasible point** — the agreed range is exhausted and no treatment met the SLO: report the
  nearest miss plainly and offer to relax the SLO or widen the range — don't keep spending runs.

## Approval + honesty
Each round's sweep rides the **normal SessionPlan / sweep approval** — propose the round's grid,
preview with `dry_run`, run gated. There is no special goal-seeking approval block; the agreed
total budget is a promise you restate in each round's plan so the user can approve quickly. And
the converged pick is the best point **found under this budget on this surface** — not a proven
global optimum, and goodput stays an estimate (`read_knowledge('analysis')`). Say "best found
within N runs", never "optimal".

Narration (`read_knowledge('conversation_style')`): one short line per round — the grid, the SLO
verdicts, the incumbent, and the next move ("all feasible → push load higher", "16 missed → narrow
between 8 and 16"). When you stop, give the incumbent, its objective value and SLO metric, and
that it's the best *found* under the budget — then make ONE next offer (save as baseline, or push
past the SLO to find the ceiling), not a menu.
