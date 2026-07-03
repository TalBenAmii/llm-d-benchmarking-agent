# Reading sweep / A-B results

After a sweep, compare the reports. **First run the validity gate**
(`read_knowledge('sweep_validity')`) — a delta between treatments that didn't actually differ is
noise, not a result. Once the treatments are confirmed distinct:

## Compare the results

Call **`compare_reports`**:
- Sweep via `experiment`/`run --experiments`: pass `experiment_dir` = the output dir (e.g. the
  `results_dir` returned by the run). It finds **every** report under it.
- Two separate runs (A/B): pass `sources=[dirA, dirB]` with `labels=["A","B"]`.

It validates each report against the BR v0.2 schema and returns per-metric **deltas vs a
baseline** plus the winning run for each metric.

For SLO-aware analysis — **goodput**, SLO pass/fail filtering, and **Pareto-optimal** config
selection across the sweep — use **`analyze_results`** instead (same `sources` / `experiment_dir`
shapes, plus the `slo` targets from the approved plan). See `read_knowledge('analysis')`. Rule of
thumb: `compare_reports` for raw side-by-side deltas; `analyze_results` when the user has QoS
targets or wants "the best config".

`compare_reports`/`analyze_results` contrast **configurations of the same harness**. If you ran
**two different harnesses** in one session (e.g. `inference-perf` for SLO validation + `guidellm`
for a throughput sweep against the same stack), contrast *those* with **`compare_harness_runs`** —
see `read_knowledge('multi_harness')`.

## Reading the deltas (what to tell the user)

`compare_reports` marks each metric's direction:
- **Latency — lower is better:** TTFT (time to first token), TPOT (time per output token),
  ITL (inter-token latency), end-to-end request latency.
- **Throughput — higher is better:** output/total token rate, request rate.
- **Success rate — higher is better** (watch for runs that "win" on throughput only because
  many requests failed — always check success rate before declaring a winner).

The central tradeoff to explain: **raising concurrency/QPS usually increases throughput but
also latency** (queuing). There is rarely a single "best" run — there's the best run *for the
user's goal*. Tie the recommendation back to what they care about:
- "Chat / interactive" → prioritize low TTFT & TPOT (responsiveness), accept lower throughput.
- "Batch / offline / cost" → prioritize high token throughput, tolerate higher latency.
- Look for the knee of the curve: the highest load where latency is still acceptable.

Report only numbers from the comparison object (validated reports) — never invent or
extrapolate. If a treatment's report is missing or schema-invalid, `compare_reports` lists it
under `skipped`; say so plainly rather than comparing a partial set silently.

**Attribute a delta only to a config difference you can point to.** Before you say "config A won
because of X", confirm X actually differed between the treatments (the validity gate above, plus
the treatment's own overrides). If the cause isn't visible in the configs, say the delta is real
but its *cause* is undetermined from this data, and propose how to find out — don't invent a
causal story, and never assert one your own tool result contradicts. (Same rule for trends across
stored runs: `read_knowledge('history')`.)
