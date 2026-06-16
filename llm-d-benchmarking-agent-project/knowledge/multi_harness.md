# Multi-harness in one session: recommend, run both, then contrast

Use this when the user wants more than a single benchmark angle — e.g. "is it fast enough
for chat AND how much throughput can it push?", "validate my SLOs and find the max load",
or just "benchmark this thoroughly". The goal: run **two harnesses in one session** and
contrast them. The mechanism is the existing tools plus `compare_harness_runs`; the
judgment — which harness for which question, how to read each, how to reconcile their
different methods — is here.

## Why two harnesses, not one

The llm-d-benchmark catalog ships several workload generators ("harnesses"). The two the
proposal calls out answer **different questions**, so running both gives a fuller picture
than either alone:

- **`inference-perf` — SLO / latency validation.** Drives a target request *rate* (QPS) or a
  fixed concurrency and reports the full latency ladder (TTFT, TPOT/TBT, ITL, end-to-end
  request latency) with percentiles. This is the harness for **"do we meet the QoS targets?"**
  — feed its report to `analyze_results` with the plan's `slo` to get the per-SLO verdict and
  the goodput estimate. It is the right tool for *interactive / chat / RAG* use cases.
- **`guidellm` — throughput sweep.** Sweeps load (e.g. a rate/concurrency schedule) to find
  how much the stack can push and where it saturates. This is the harness for **"what's the
  max sustainable throughput, and where's the knee?"** — feed its sweep output to
  `compare_reports`/`analyze_results` (`experiment_dir` / multiple `sources`) for the
  Pareto/throughput-vs-latency curve. It is the right tool for *batch / offline / cost* and
  for capacity planning.

Pick the harness from what the user cares about (don't run both reflexively — say why each is
worth it). A typical multi-harness session:
1. **inference-perf** at the user's target load → does it meet the SLOs? (goodput via
   `analyze_results`).
2. **guidellm** sweep → how far can it go, and where does latency break the SLO? (the knee /
   the max feasible load).
Then contrast the two with `compare_harness_runs` and tie back to the user's goal.

Confirm both harness names + their workload profiles exist in the **live catalog**
(`list_catalog`) before planning — never invent them. Both `inference-perf` and `guidellm`
ship a `sanity_random.yaml` and several shared synthetic profiles; match the workload to the
question (a chat profile for the SLO run, a throughput/concurrency profile for the sweep).

**Don't claim a "live"/"just-checked" catalog you didn't look at.** The authoritative catalog
arrives as an in-context "[live catalog snapshot …]" message and is re-enumerable with
`list_catalog` — but it is NOT a lookup you perform by reciting harness names from memory. Never
say "from the live catalog you can see right now…" or "checking the live catalog…" unless you
actually called `list_catalog` **this turn** or are quoting that snapshot. If you're listing
harness/spec/workload names from prior knowledge, say so and offer to verify live with
`list_catalog`; if a name matters (you're about to plan on it), call the tool first.

## Run them against the SAME stack — that's the whole point

Stand the stack up **once** (`standup`), then run each harness against it (one `run` per
harness, or a sweep for guidellm). Re-deploying between harnesses would change the system
under test and make the contrast meaningless. Keep the model, spec, namespace, and stack
**fixed**; the only thing that changes between the two is the harness (and its workload).
This is a *run-parameter* difference, not a deployment difference — so it does **not** need
the heavy full-DoE `experiment` path (see `sweep_playbook.md`). On the kind/CPU-sim
quickstart especially, one standup + two runs is the cheap, correct shape.

A single approved SessionPlan can cover the whole session: pick whichever harness/workload is
primary for the plan's enum fields, and list the second harness run in `expected_steps` and
`notes` so the user approves the multi-harness intent up front. Capture the user's `slo`
targets in the plan either way — they drive the inference-perf analysis.

## Reading the contrast (`compare_harness_runs`)

After both harnesses have run, call `compare_harness_runs(sources=[<inference-perf run dir>,
<guidellm run/sweep dir>, ...])`. It reads which harness produced each report **from the
report itself** (`scenario.load.standardized.tool`), groups the runs by harness, and returns:

- `harnesses` — per harness: its runs (with the load point: rate_qps / concurrency) and which
  metric families it measured (`latency_metrics`, `throughput_metrics`).
- `shared_metrics` — objective fields **both** harnesses measured. Treat these as a
  **cross-check**: if inference-perf and guidellm broadly agree on, say, TTFT at a comparable
  load, that raises confidence. They will rarely match exactly — different load models, warmup,
  and tokenizers — so look for *consistency of story*, not identical numbers.
- `unique_metrics` — fields only one harness reported. Report those **from that harness alone**
  and say which one; don't imply the other measured them.
- `cross_metrics` — for each shared metric, the per-harness value side by side. The tool
  deliberately **does not pick a winner** across harnesses: two different load generators are
  not directly comparable, so a "winner" would be misleading. (Within one harness's runs, use
  `compare_reports` — that's where picking a best config is legitimate.)
- `same_model` / `models` — if more than one model appears, the contrast is **not meaningful**;
  the tool flags it. Say so and don't compare across models.

## What to tell the user

Lead with the synthesis, not two disconnected dumps:
- **The SLO answer** (from the inference-perf run via `analyze_results`): "At your target load,
  ~X% of requests met your TTFT/latency targets" (goodput is an *estimate* — relay the honesty
  caveat from `knowledge/analysis.md`).
- **The capacity answer** (from the guidellm sweep): "throughput keeps rising to ~Y tokens/s,
  but the knee is around load Z — past that, latency blows the SLO."
- **The combined recommendation**: the operating point that satisfies the SLOs *and* leaves
  headroom — typically just below where the guidellm sweep shows the latency knee. Name the
  trade-off explicitly ("you can push to Y tok/s, but to stay under your 200ms TTFT SLO, hold
  load at ≤ Z").

Quote only numbers that appear in the validated reports / analysis objects — never invent or
extrapolate. If a harness's report is missing or schema-invalid, `compare_harness_runs` lists
it under `skipped`; say so plainly rather than contrasting a partial set silently. If only one
harness actually produced a valid report, the tool refuses and points you at `compare_reports`
— don't fabricate the missing side.

## The kind/CPU-sim caveat still applies

On the simulated CPU engine the absolute numbers aren't representative of GPU serving (see
`knowledge/analysis.md`). A multi-harness session on the quickstart demonstrates the
*methodology* (validate SLOs with inference-perf, sweep throughput with guidellm, reconcile
them) — not real-world performance. The identical flow is what you'd run against a real GPU
stack.
