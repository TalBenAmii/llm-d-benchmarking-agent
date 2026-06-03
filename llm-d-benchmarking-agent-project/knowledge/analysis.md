# Results Analyzer: SLOs, goodput, and Pareto/DoE analysis

Use `analyze_results` (read-only) when the user has **QoS targets** ("first token under
200ms", "P99 latency below 1s", "at least 300 tokens/sec") or wants the **best config**
from a sweep. `compare_reports` gives you raw per-metric deltas; `analyze_results` adds the
three things the proposal calls out as the analyzer's job: **SLO filtering**, **goodput**,
and **Pareto/DoE** frontier analysis. The tool is the *mechanism*; the judgment below is
yours.

## Capture the SLOs during the interview, into the plan

The SLO targets belong in the **SessionPlan** (`slo` field). Elicit them from the use case:
- **Chat / interactive** -> a TTFT ceiling (responsiveness) and often a TPOT/ITL ceiling
  (streaming pace). Throughput is secondary.
- **RAG** -> TTFT + end-to-end request-latency ceiling (retrieval + generation budget).
- **Batch / offline / cost** -> a throughput floor; latency is loose.

Express latency targets as **maxima in milliseconds** (`ttft_ms`, `tpot_ms`, `itl_ms`,
`request_latency_ms`), the throughput target as a **minimum in tokens/sec**
(`throughput_floor_tok_s`), and optionally a `min_success_rate_pct`. Pick the `percentile`
the SLO is really about - usually **p99** (the tail users actually feel); use `mean` only
if the user truly means the average. Don't invent targets the user didn't state; if they
have none, run `analyze_results` without `slo` for a pure frontier analysis, or just use
`compare_reports`.

## Goodput is the differentiator - and be honest about how it's computed

**Goodput = the fraction of requests that meet ALL the SLO targets.** It is the headline
the proposal wants, because a run can have great raw throughput while quietly failing the
latency SLO for most requests. Lead with it: "~X% of requests met your targets" is far more
useful than "throughput was Y".

Honesty constraint you MUST relay: Benchmark Report v0.2 stores **aggregate** statistics
(mean + percentiles), **not per-request data**. So the goodput number is an **estimate**,
derived by locating each latency target on the report's percentile ladder and interpolating
the fraction of requests below it. The tool flags this (`goodput.is_estimate = true`,
`method`, and `goodput.estimate_pct`). When multiple latency SLOs apply, the combined
goodput is the **min** across them - an *upper bound*, since the report can't tell us how
violations correlate across requests. Say "estimated" and never present it as exact.
If you need exact per-request goodput, that requires per-request output the aggregate report
doesn't carry - say so rather than overclaiming.

The per-metric `verdicts` are exact: each says whether that metric at the chosen percentile
met its target (`met`), with `observed` vs `target` in canonical units (ms / tokens/s, after
unit conversion). A throughput floor is a run-level gate (mean vs floor), not a per-request
fraction, so it gates the pass/fail but does not enter the goodput estimate.

`overall_met` is true only when **every** checked SLO (and the success-rate floor, if set)
passed. A run that "wins" on throughput but fails the latency SLO is **not** a pass - always
check `success_rate_met` too, since a run can look fast only because requests were dropped.

## Pareto / DoE: there is rarely one "best" - there's a frontier

For a sweep (pass `experiment_dir`, or `sources` with 2+ runs), the tool returns `pareto`:
- `objectives` - the metrics present in >=2 runs, each with its `direction` (latency =
  lower-better, throughput = higher-better) and units.
- `frontier` - the labels of the **Pareto-optimal** runs: those that no other run beats on
  every objective at once. A run *off* the frontier is strictly dominated - there's another
  run at least as good everywhere and better somewhere, so never recommend a dominated run.
- With SLOs: `slo_feasible` (runs meeting all targets) and `slo_frontier` (the best
  trade-offs **among** the feasible runs). This answers the proposal's example directly:
  "best throughput at a given latency constraint" = the feasible-frontier run that maximizes
  throughput. If `slo_feasible` is empty, **no config meets the targets** - say so plainly
  and show how close the nearest run came (its verdicts), rather than recommending a failure.

How to advise:
1. If SLOs were given: recommend from `slo_frontier`, picking the point that best serves the
   *primary* goal (lowest TTFT for chat; highest throughput for batch). Mention the runner-up
   trade-off ("conc=16 hits 2x throughput but TTFT p99 rises from 180ms to 410ms").
2. If no SLOs: explain the frontier as a trade-off curve and find the **knee** - the highest
   load where latency is still acceptable - rather than declaring a single winner.
3. Always tie the pick back to what the user said they care about, and only quote numbers
   that appear in the analysis object (validated reports). Never extrapolate beyond the
   reported percentiles or invent a treatment that wasn't run.

## Informational §3.4 metrics: KV-cache hit rate, schedule delay, GPU utilization

When the reports carry them, `pareto.informational_objectives` surfaces the §3.4 standard
*serving* metrics per run — **alongside** the frontier, never inside it. They are flagged
`informational: true` and deliberately do **not** affect `frontier`, `slo_frontier`,
`overall_met`, or goodput. Use them to *explain* the frontier, not to pick on them:

- **KV-cache hit rate** (`max` better) — the headline informational objective. A frontier
  run with a much higher hit rate is faster *because* it recomputes less prefill; this is the
  evidence that prefix-cache-aware routing/config is working. Quote it when recommending:
  "config B is on the latency frontier and has a 65% KV-cache hit rate vs 12% for A — that's
  why its TTFT is lower."
- **Schedule delay** (`min` better) — a queue-depth **proxy** (requests waiting), not a time.
  A run with a low TTFT but a climbing queue depth is near saturation; flag that its tail will
  degrade under more load. Never present the proxy as a millisecond delay.
- **GPU utilization** (`max` = more utilized, not automatically better) — read it next to
  throughput: high util + high throughput = the GPU is well used; low util + bad latency
  points at a non-GPU bottleneck (queueing/CPU/network).

`leader` names the run that leads each informational metric. A metric absent from every run
is simply omitted — say nothing about it. On the CPU-sim quickstart these are usually absent.

## Optional `--analyze` plot families: supplementary pictures, not new math

A `run` can be told to ALSO render the CLI's own workstation matplotlib **plot families** by
setting `flags={'analyze': True}` on `execute_llmdbenchmark(subcommand='run', ...)` — it emits a
bare `--analyze`. This is **run-only** (upstream defines `--analyze` on the `run` subcommand
alone; never pass it on standup/plan/experiment) and it does **not** change the run's mutating
mode (a real run still loads the cluster and stays approval-gated). It is pure *mechanism*; the
judgment below is yours.

It writes **three extra families**, all under the run's `analysis/` dir, **in addition** to the
harness's own latency/throughput PNGs:

- **`analysis/distributions/`** — *per-request distributions* (histograms/CDFs of TTFT, ITL,
  end-to-end latency, token counts). Reach for this when the user asks about the **shape of the
  tail** ("are a few requests dragging the p99?") rather than a single percentile.
- **`analysis/session/`** — *session-lifecycle* bar charts (session rate, session duration
  mean/p99, events & output-tokens per session, failed/cancelled sessions). Useful for
  **multi-turn / agentic** workloads where per-session behavior matters; on single-turn runs the
  harness writes no session-lifecycle files, so this family is simply empty (never fabricated).
- **`analysis/graphs/`** — *Prometheus time-series* line graphs over the captured
  `metrics/raw/*.log`. These need the **monitoring producer** to have run, so set
  `flags['monitoring']=True` (Phase 27) on the standup/run too — otherwise there's nothing to
  plot. Use them to **explain WHY** the frontier looks the way it does (KV-cache hit rate /
  queue depth / GPU util over the run), corroborating the §3.4 informational metrics above.

Each requires **matplotlib** on the workstation; if it's absent the CLI skips that family without
failing the run. The generated PNGs are surfaced **alongside** the harness charts through the
same artifact route (`locate_and_parse_report` lists them in `charts`, each titled with its
family subdir — `Distributions: …`, `Session: …`, `Graphs: …` — so the families don't collide).

**These plots are SUPPLEMENTARY visualizations — they do NOT change any number.** Your SLO
verdicts, goodput estimate, and Pareto/`slo_frontier` analysis come from `analyze_results` over
the **validated Benchmark Report**, exactly as above, whether or not `--analyze` ran. Use the
plots to *illustrate* a finding to the user ("here's the TTFT distribution behind that p99"),
never to derive the recommendation. WHEN to ask for them: the user wants to *see* the data
(tail shape, per-session breakdown, time-series), or you're explaining a non-obvious frontier.
Skip them for a quick pass/fail check, or when the user just wants the headline goodput number.

## Caveat that still applies on the kind/CPU-sim quickstart

The simulated CPU engine's absolute numbers are **not** representative of GPU serving, and
single-replica routing benefits don't show. Goodput/SLO/Pareto on the quickstart prove the
*analysis pipeline* works end to end; say the methodology is what's demonstrated, not the
performance. The same `analyze_results` call is what you'd run against real GPU sweep output.
