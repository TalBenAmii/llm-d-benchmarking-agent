# Historical result storage + trends (the `result_history` tool)

The `result_history` tool persists **validated** Benchmark Report summaries across
sessions and lets you read **trends** over time. The tool is pure mechanism — it stores
facts and returns value-series; *you* supply the judgment (is this a regression? is the
drift acceptable?). Nothing here touches the cluster or the repos, so every action
auto-runs (no approval).

## When to store a result
Store a run the user will care about *later*, **after** you've already located/parsed it
(`locate_and_parse_report`) and, ideally, analyzed it (`analyze_results`):
- A **baseline** the user wants to track regressions against ("remember this as our 8B
  baseline").
- Each treatment of a **sweep/experiment** they want to revisit or compare across days.
- Any run the user explicitly asks to "save", "keep", or "remember".

Do NOT auto-store every throwaway smoketest — storing is for results with lasting value.
Always pass a clear `label` and useful `tags` (e.g. `["8B","baseline"]`, or
`["concurrency-sweep","2026-06"]`) so `list`/`trend` can be filtered later. Pass the
`spec`/`harness`/`workload`/`namespace` you used as provenance. Storing is **idempotent**:
re-storing the same report returns the existing record (`created: false`) rather than a
duplicate — mention that to the user instead of implying a second save happened.

The tool **refuses to store a report that fails schema validation** (determinism gate d):
if `stored: false` with a validation reason, fix/locate a valid report first; never
hand-edit numbers to make it store.

## Reading a trend
`action="trend"` with a `metric` returns the chronological series (oldest → newest), the
metric's `better` direction (`lower` for latency, `higher` for throughput/success-rate),
the representative `stat` used (mean if present), units, and a factual first-vs-last delta.
Filter the series with `filter_tag` / `filter_model` so you trend *comparable* runs (don't
mix a 1B and a 70B model, or two different workloads, in one latency trend — that's not a
regression, it's a different test).

Available metrics: `ttft`, `tpot`, `itl`, `request_latency` (latency, lower is better);
`output_token_rate`, `total_token_rate`, `request_rate` (throughput, higher is better);
`success_rate_pct` (higher is better).

## Turning a trend into a verdict (your job, not the tool's)
- Use `better` to read the sign of `first_to_last.delta_pct`: a latency metric going **up**
  or a throughput metric going **down** is *worse*; the reverse is *better*.
- A single small wiggle is **noise**, not a regression — benchmark runs vary. Call out a
  trend only when it's consistent across several points or a large step change. Be explicit
  that you cannot prove statistical significance from these aggregates.
- Anchor "regression" to the user's SLO when they have one (see `knowledge/analysis.md`):
  a 5% TTFT increase that's still under the SLO target is usually fine; one that crosses the
  target is a real regression. Tie the trend back to `analyze_results` when SLOs exist.
- The series carries `run_uid`, `label`, and `tags` per point — name the specific runs when
  you explain a change so the user can go look at them.
- If `n` is 0 or 1, say there isn't enough history yet to call a trend, and suggest storing
  more baselines first.

## Browsing in the UI
The same store backs the read-only `GET /api/history` (results browser) and
`GET /api/history/trend?metric=...` (trend chart) endpoints, so anything you store is what
the user sees in the Results panel. The UI shows facts; you provide the narrative.
