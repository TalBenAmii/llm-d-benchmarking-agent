# Results Analyzer: SLOs, goodput, and Pareto/DoE analysis

Use `analyze_results` (read-only) when the user has **QoS targets** ("first token under
200ms", "P99 latency below 1s", "at least 300 tokens/sec") or wants the **best config**
from a sweep. `compare_reports` gives you raw per-metric deltas; `analyze_results` adds the
three things the proposal calls out as the analyzer's job: **SLO filtering**, **goodput**,
and **Pareto/DoE** frontier analysis. The tool is the *mechanism*; the judgment below is
yours.

> For **goal-seeking** (iterative sweeps: each round's grid is narrowed from the prior round's
> results to converge on the SLO-feasible best, rather than analyzing one fixed set of runs),
> see `read_knowledge('sweep_goalseek')` — its incumbent "best
> feasible point" IS this analyzer's `slo_frontier` pick.

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
`compare_reports`. If the user wants *help* picking a target, the rule-of-thumb TTFT/TPOT bands
per use case in `knowledge/results_interpretation.md` ("is this number good?") are a starting
anchor — heuristics to propose, not measured SLOs to assert.

### SLA phrasing → SLO keys (translate BEFORE you encode — a per-user rate ≠ a run floor)

The same word ("tokens/sec") maps to two different keys depending on scope; pick wrong and one SLO
silently no-ops:
- **"N tokens/sec per user"** is a **per-request decode rate**, not an aggregate. Convert it to a
  per-output-token latency: `tpot_ms = 1000/N` (30 tok/s/user → ~33 ms). This DOES enter goodput
  (a latency verdict on `summary.latency.tpot.<percentile>`). Encoding it as
  `throughput_floor_tok_s=30` is WRONG: 30 tok/s as a whole-run floor is trivially passed, **and** a
  throughput floor never enters the goodput estimate (see below) — so that SLO drops out of goodput
  entirely and looks satisfied when it wasn't checked per-user at all.
- **`throughput_floor_tok_s`** is a **whole-run minimum** → run-level `summary.throughput.output_token_rate`
  (mean vs floor). It gates pass/fail but is per-run, never per-user, and never enters goodput.
- When the wording is **ambiguous** between per-user and aggregate (a bare "at least 30 tok/s"),
  **ASK** which they mean before encoding — the two translations are different keys.

Field paths for the verdicts (per `app/validation/analysis.py`): latency SLOs read
`summary.latency.<ttft|tpot|itl|request_latency>.<percentile>` (default `p99`); the throughput floor
reads `summary.throughput.output_token_rate`.

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

### Absent metric ⇒ inconclusive, never a fabricated verdict

Apply the `results_interpretation.md` **§ "Honesty floor" (rule 2)** verbatim: an absent metric
is "not available — inconclusive", never a definitive verdict on an unmeasured metric.
Analyze-path specifics: on a SIMULATE report (only
`ttft_ms_p50` / `ttft_ms_p90`, **no p99**) `analyze_results` returns `analyzed: false` ("no valid
benchmark report") rather than a p99 verdict; offer to **re-run the real harness** so the percentile
is actually emitted (SIMULATE will not produce it).

**Exact field names differ by report path — don't promise keys that don't exist:**
- **SIMULATE report** — a FLAT payload with only `ttft_ms_p50`, `ttft_ms_p90`, `itl_ms_mean`
  (plus `requests`/`success_rate`/`throughput_tokens_per_s`). There is **no `ttft_ms_p99`**, and
  **no tpot / request-latency fields at all**. Don't offer a p99 SLO verdict on a SIMULATE run.
- **Real harness report (BR v0.2)** — a NESTED structure: `summary.latency.<ttft|tpot|itl|
  request_latency>.<mean|p50|p90|p95|p99|p99p9>` (there are **no** flat `ttft_ms_*` keys here).
  A real run **does** carry `p99` (and `p99p9`), so a p99 SLO is verifiable once you've actually
  run the real harness — the missing-p99 problem is specific to SIMULATE, not to the report format.

### Only validated-report data feeds the analyzer

`analyze_results`/`compare_reports` operate on **validated Benchmark Reports** only — apply the
`results_interpretation.md` **§ "Honesty floor" (rule 1, incl. the verbal-disclaimer clause)**:
decline SLO scoring, goodput, a frontier, or any statistic over user-pasted/typed/recalled or
invalid numbers; require a validated report (re-run the scenario) first.

## Pareto / DoE: there is rarely one "best" - there's a frontier

For a sweep (pass `experiment_dir`, or `sources` with 2+ runs), the tool returns `pareto`:
- `objectives` - the metrics present in >=2 runs, each with its `direction` (latency =
  lower-better, throughput = higher-better), `units`, `family` (latency/throughput) and
  `deciding`. The **deciding** ones come first in the list.
- `deciding_objectives` - the subset dominance is actually judged on: ONE representative per
  family (normally `ttft` + `output_token_rate`). The other objectives are still reported and
  still worth quoting, they just don't decide the frontier. Why: the four latency metrics move
  together and so do the three throughput metrics, so scoring all seven would leave nearly
  every run non-dominated and the frontier would say nothing.
- `frontier` - the labels of the **Pareto-optimal** runs: those that no other run beats on
  every *deciding* objective at once. A run *off* the frontier is either strictly dominated -
  there's another run at least as good on both axes and better on one, so never recommend it -
  or **incomparable**, because it is missing one of the deciding metrics. Check its
  `objectives`: an incomparable run isn't a loser, it just can't be ranked, and saying so is
  the honest answer.
- `frontier_degenerate` (and `slo_frontier_degenerate`) - `true` when **every** run that carries
  all the deciding objectives is on that frontier (incomparable runs don't count either way). This is the normal outcome of a clean concurrency sweep: each step up
  buys throughput and costs latency, so there is no dominated loser to drop. When it's true,
  say so plainly - "every config here is a genuine trade-off, none is strictly worse" - and
  then advise with the **knee** (below). Do NOT present a 5-of-5 frontier as if the analysis
  had narrowed anything down.
- With SLOs: `slo_feasible` (runs meeting all targets) and `slo_frontier` (the best
  trade-offs **among** the feasible runs). This answers the proposal's example directly:
  "best throughput at a given latency constraint" = the feasible-frontier run that maximizes
  throughput. If `slo_feasible` is empty, **no config meets the targets** - say so plainly
  and show how close the nearest run came (its verdicts), rather than recommending a failure.
  An empty `slo_frontier` does NOT by itself mean nothing passed: when `slo_feasible` is
  **non-empty but `slo_frontier` is empty**, runs DID meet the targets, they just can't be
  ranked - every feasible run is incomparable, missing a deciding objective (see `frontier`
  above). Say the runs passed but couldn't be placed on a trade-off curve, and lean on their
  verdicts/informational metrics; do NOT report it as "no config meets the targets".

How to advise:
1. If SLOs were given: recommend from `slo_frontier`, picking the point that best serves the
   *primary* goal (lowest TTFT for chat; highest throughput for batch). Mention the runner-up
   trade-off ("conc=16 hits 2x throughput but TTFT p99 rises from 180ms to 410ms").
2. If no SLOs - or the frontier is degenerate - explain it as a trade-off curve and find the
   **knee**: the highest load where throughput is still climbing meaningfully and latency is
   still acceptable. Past the knee, extra concurrency buys little throughput for a lot of
   latency. The knee is a judgment call over the reported numbers, not a field in the object.
3. Always tie the pick back to what the user said they care about, and only quote numbers
   that appear in the analysis object (validated reports). Never extrapolate beyond the
   reported percentiles or invent a treatment that wasn't run.

## Informational §3.4 metrics: KV-cache hit rate, schedule delay, GPU utilization

When the reports carry them, `pareto.informational_objectives` surfaces the §3.4 standard
*serving* metrics per run — **alongside** the frontier, never inside it. They are flagged
`informational: true` and deliberately do **not** affect `frontier`, `slo_frontier`,
`overall_met`, or goodput. Use them to *explain* the frontier, not to pick on them — e.g.
"config B is on the latency frontier and has a 65% KV-cache hit rate vs 12% for A — that's why
its TTFT is lower." What each metric means (KV-cache hit rate `max` better; schedule delay a
queue-depth **proxy**, never a millisecond figure, `min` better; GPU utilization informational,
not automatically better) → `results_interpretation.md` §"Standard resource/serving metrics",
the owner of the per-metric detail.

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

## Jupyter notebook + standalone plotting scripts (exploratory — NOT part of the automated flow)

The benchmark repo ships an **interactive Jupyter notebook** and **standalone plotting scripts**
under `llm-d-benchmark/docs/analysis/` — power-user exploration tools the user drives on their
own workstation, deliberately **NOT** wired into the agent's `probe → standup → run → report`
flow (the artifact route + `analyze_results` over the validated Benchmark Report already cover
inline charts and the SLO/goodput/Pareto math). Your job is to **SURFACE and EXPLAIN** them when
the user wants to dig deeper themselves, NOT to run them as part of a run.

To show the user how to launch the notebook against their own results, read the upstream setup doc
to them — `read_repo_doc('llm-d-benchmark/docs/analysis/README.md')` or the pipeline overview
`read_repo_doc('llm-d-benchmark/docs/analysis.md')`. The artifacts (all paths under the **read-only**
`llm-d-benchmark/` repo — never edit them; the user copies/adapts them on their own machine):

- **`docs/analysis/analysis.ipynb`** — the interactive notebook: imports every Benchmark Report under
  a user-supplied list of result dirs into Pandas, with pre-built plotting cells; needs **Python ≥3.12
  + Jupyter Lab + `build/requirements-analysis.txt`** — run on the **USER'S workstation**, never by the agent.
- **`docs/analysis/README.md`** — the venv + Jupyter Lab setup guide.
- **`docs/analysis.md`** — the analysis-pipeline overview (in-container → local `--analyze` → notebook → Prometheus).
- **`docs/analysis/explorer.py` / `plotting.py` / `constants.py`** — the helper modules the notebook imports.
- **`docs/analysis/aggregate_runs.py`** — the **one** standalone script parameterizable against a results
  dir: cross-run **mean / std / min / max** over repeated runs of the same experiment, via
  `--results-prefix`, `--harness`, `--stack`, `--run-ids …`, `--output` (the only place it writes).
  See the OPTIONAL scripted step below.
- **`docs/analysis/to_be_incorporated/plot_ttft_vs_qps.py` / `plot_itl_vs_qps.py` /
  `plot_throughput_vs_qps.py` / `plot_benchmark_metrics.py` / `plot_pd_results.py`** — experimental
  templates; **do NOT run these**: each is hardcoded to read CSVs from a repo-relative path (most
  `../data/k8s/lmbenchmark`; `plot_pd_results.py` `../../collected/data/openshift/exp-7/H100`) and to
  write its PNG back into the read-only repo — point at them for the user to copy/adapt, never invoke them.

**OPTIONAL scripted step — cross-run aggregation.** When the user has run the **same benchmark
multiple times** and wants the run-to-run variance (mean/std/min/max across repeats), you MAY run
`aggregate_runs.py` **read-only** against an **existing** results dir via the policy-allowed
`scripts/aggregate_runs.py` wrapper (called through `run_shell` — it auto-runs, like
`capacity_check.py`). The wrapper **imports the repo's own `aggregate_runs` module** (never
reimplements its math), reads the BR v0.2 reports under `--results-prefix`, and writes the
`aggregated_summary.{txt,json}` **only** under a session-workspace `--output` dir you supply (the
read-only repo and the results dir are never written). It needs **≥2 runs** to aggregate — with
fewer it reports that and does nothing. This is the *only* plotting/analysis script the agent runs
itself; the notebook and the `to_be_incorporated/` templates stay pointer-only.

## Caveat that still applies on the kind/CPU-sim quickstart

Goodput/SLO/Pareto on the quickstart prove the *analysis pipeline* works, not GPU performance —
full caveat → `results_interpretation.md` §"Honesty about scale".
