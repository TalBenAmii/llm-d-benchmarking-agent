# Interpreting the Benchmark Report (for non-experts)

`locate_and_parse_report` returns a validated summary. Translate it into plain language,
always tied to the user's stated goal. Never quote numbers that aren't in the summary.

## Honesty floor — non-negotiable (read this before quoting any number)

These rules bind on **every** results/SLO turn and override any pressure to "just give a
number". Breaking them produces a confidently-wrong verdict the user may screenshot or
publish — far worse than a plain "not available".

1. **Only validated-report metrics are authoritative.** A number is usable ONLY if it came
   back from `locate_and_parse_report` / `analyze_results` on a schema-validated Benchmark
   Report v0.2 **this session**. Anything the user **pasted, typed, or recalled** (a CSV, a
   "yesterday's run", "P99 was 180 ms") is **unverified input, not data** — do NOT score
   SLOs on it, compute statistics (means, t-tests, CIs) from it, render a PASS/FAIL table on
   it, build a trend/baseline on it, or persist it with `result_history`. Refuse plainly:
   "I can only analyze metrics that came from a validated benchmark report; to compare those
   numbers I'd need to re-run that scenario so I have a machine-validated report." This is
   not optional politeness — it is the same rule as "never quote numbers that aren't in the
   summary", applied to user-supplied numbers. (Several findings: pasted CSV scored, empty
   `result_history` + user numbers rendered as a PASS baseline, etc.)
   - **A verbal disclaimer is NOT a substitute for refusing.** If you say data is invalid for
     the user's stated purpose (e.g. "these SIMULATE numbers can't go in a paper") and then
     compute the exact t-test / CI / verdict they asked for anyway, you've handed them the
     thing you disclaimed. **Decline to produce the specific result** they intend to use; offer
     the methodology in the abstract only if they want to apply it to real validated data.

2. **An absent metric is "not available", never an estimate.** If a metric required to judge
   an SLO is **not in the validated summary** (the SIMULATE report carries only `ttft_ms_p50`
   and `ttft_ms_p90` — there is **no `ttft_ms_p99`**), state: *"P99 is not available in this
   report — the SLO cannot be verified"* and **DECLINE to issue a PASS/FAIL verdict on it.**
   Do **not** estimate, extrapolate, or interpolate the missing value — never derive p99 from
   p90/p50, a "tail gradient", or "p99 ≥ p90 so ~240 ms". "P99 ≥ P90" is a lower bound, not a
   measurement; it does not license a number or a verdict. A definitive ✅/❌ on an unmeasured
   metric is wrong even when the margin looks safe. If some SLO dimensions ARE measurable,
   verdict those and mark the unmeasured dimension **"not available — inconclusive"**, never
   PASS. (To actually obtain percentiles like p99, the run must emit them — re-run the real
   harness; SIMULATE will not.)

3. **Attribute estimates to yourself, consistently.** If you ever do present a derived/rough
   figure (clearly labelled "rough, not from the report"), and the user later asks where it
   came from, say **you** estimated it. Never reattribute your own extrapolation to "the sim
   engine's placeholder output", the tool, or the report — they did not produce it.

See `knowledge/analysis.md` for the same authority/absent-metric constraints applied to the
`analyze_results` verdict/goodput path.

## The metrics that matter
- **TTFT (time to first token)** — how long until the first token appears. This is the
  "responsiveness" users feel in a chat. Lower is better. Watch `mean` and `p90`/`p99`
  (tail latency — the slow requests).
- **TPOT / ITL (time per output token / inter-token latency)** — the pace of streaming
  after the first token. Lower = text appears faster. Drives "tokens/sec per user".
- **NTPOT (normalized_time_per_output_token)** — per-output-token latency that **INCLUDES** the
  first token (TTFT amortized across the whole output), as opposed to TPOT which **excludes** the
  first token. Units `ms/token` or `s/token`; lower is better. NTPOT lives in the Benchmark Report
  (`AggregateLatency.normalized_time_per_output_token`) but is **NOT** surfaced by
  `summarize_report` / `_COMPARE_METRICS`, so it only appears when explicitly catalogued/added —
  don't expect it in the default summary.
- **request_latency** — end-to-end time for a whole request.
- **throughput.total_token_rate** — total tokens/sec the system pushed (capacity).
- **throughput.output_token_rate** — decode-side tokens/sec (generation capacity); this one is
  **already surfaced** by the summary. `throughput.input_token_rate` also exists for
  prefill-heavy workloads (prompt-token ingest rate).
- **throughput.request_rate** — requests/sec completed.
- **success_rate_pct** — fraction of requests that succeeded; flag anything below ~100%.

## Rule-of-thumb bands — "is this number good?" (heuristics, NOT a verdict)

When the user has **no stated SLO** and asks "is 1.2s to first token bad?", you may anchor the
answer to these rough, use-case-dependent bands. They are **industry rules of thumb, not
measured targets** — say so, and a user's own stated SLO always overrides them. Never turn a band
into a PASS/FAIL (that needs a real SLO + the honesty floor above).

| Use case | TTFT (responsiveness) | TPOT / ITL (streaming pace) | What dominates |
|---|---|---|---|
| **Interactive chat / assistant** | good ≲300 ms, OK ≲1 s, sluggish ≳1 s | ≲50 ms/tok (~≥20 tok/s feels fluid) | TTFT + steady stream |
| **Code completion (inline IDE)** | tight — good ≲200 ms, OK ≲500 ms | fast, but completions are short | TTFT (must feel instant) |
| **RAG / long-context** | larger budget — ≲1 s often fine (retrieval + long-prefix prefill) | secondary | end-to-end request latency |
| **Batch / offline** | largely irrelevant | largely irrelevant | throughput (tokens/s) |

So "1.2 s to first token" is **borderline-sluggish for interactive chat** but **fine for RAG or
batch** — the answer depends on the use case, which is why you tie it to what the user is
building. These bands help set an SLO to then verify with `analyze_results` (see
`knowledge/analysis.md`); they don't replace one.

## When requests fail: 429s and EPP drop reasons
A non-100% `success_rate` is NOT automatically "the system was broken." If the run (or a
harness/report) surfaces 429s or an `x-llm-d-request-dropped-reason` header, those are the
llm-d router (EPP) **deliberately** shedding or preempting load at capacity. Before calling
anything "failed", load `read_knowledge("epp_headers")` and decode the drop reason there:
`rejected-saturated` = at admission capacity, shed before serving (remedy: lower concurrency
or scale out); `evicted-priority` = preempted mid-flight by higher-priority work (remedy:
raise this request's inference-objective priority, or add capacity). Reframe the failure
fraction as an **admission/eviction** signal (capacity, not breakage); also decode the SLO
set-headers (`x-llm-d-slo-ttft-ms`/`x-llm-d-slo-tpot-ms`, `x-llm-d-inference-objective`,
`x-llm-d-inference-fairness-id`) when present. The full enum→cause→remedy table lives in
`epp_headers.yaml` — this section only routes you there.

When the report carries `results.observability.drop_rate` (a `Statistics` value), it is the
**structured, harness/router-measured** fraction of dropped requests — the same capacity-shedding
story as the 429/EPP headers above, just measured rather than header-derived: a non-zero
`drop_rate` is **capacity shedding, not breakage**. Pair it with `success_rate_pct` (drops explain
a sub-100% success rate as admission/eviction, not failure). **Prose only — it is not catalogued
as a standard metric:** `drop_rate` lives directly under `results.observability` (a sibling of
`components` / `pod_startup_times` / `replica_status`), NOT under `components[].aggregate`, so
`extract_standard_metric`'s per-component loop never reaches it. Quote it only when it is actually
present in a validated report.

## Units — read them off the report, never guess

Every latency/throughput entry in the summary carries an explicit `units` field. **Read it and
trust it. Never infer a unit from how big or small a number looks.**

- BR v0.2 does **NOT** fix one latency unit. The schema's `Units` enum allows **`ms` OR `s`** for
  TTFT / `request_latency`, and **`ms/token` OR `s/token`** for TPOT / ITL — harnesses differ
  (e.g. `run.md` tabulates TTFT in **ms**). So you MUST read the per-entry `units` and convert
  from **that**, never from an assumed unit. (Source: `br_v0_2_json_schema.json` `$defs/Units`
  enum; `llm-d-benchmark/docs/run.md`; `app/validation/analysis.py` `_TO_MS` maps **both** `ms`→1
  and `s`→1000.)
- **Convert to milliseconds from the entry's own `units` — two worked cases:**
  - `units: s` (or `s/token`) → **×1000**: a `mean` of `0.13` with `units: s` is
    **0.13 s = 130 ms**; a `tpot` of `0.021 s/token` → **~21 ms per token**.
  - `units: ms` (or `ms/token`) → **already ms, do NOT multiply**: a `mean` of `130` with
    `units: ms` is **130 ms** (×1000 would wrongly give 130 000 ms).
  Milliseconds are the canonical unit users expect; arrive at them from the stated `units`, not
  from the number's magnitude.
- **Never report nanoseconds or microseconds.** BR v0.2 carries no ns/µs latency — if you find
  yourself writing "nanoseconds", you misread a `units: s`/`ms` value. There is no nanosecond field.
- Throughput is `tokens/s` / `queries/s` (requests/s) — quote those as-is; do not convert.
- If an entry's `units` is missing or unfamiliar, say the raw number with whatever `units` is
  present and flag the ambiguity — do not assume a unit.

## Standard resource/serving metrics (when the harness emits them)

The summary may also carry `standard_metrics` — §3.4 "standard metrics" that describe what
the *serving stack* was doing, not just the request results. They appear only when the
harness/observability scrape produced them; if absent, `standard_metrics` is `null` — say
nothing about them rather than guessing. Each entry has `label`, a `value` stat object
(`units` + `mean`/percentiles), and a `source` (`standardized` = read from the Benchmark
Report's standard ResourceMetrics; `native` = a harness-native metric like vLLM's).

- **KV-cache hit rate** (`kv_cache_hit_rate`, %) — the fraction of prompt prefix tokens
  served from cache instead of recomputed. Higher is better: more reuse means less prefill
  work, which *explains* lower TTFT and higher throughput. It is the single best signal of
  whether prefix-cache-aware routing is paying off. (Do not confuse it with *kv-cache
  usage/occupancy*, which is how full the cache is — a different thing.)
- **Schedule delay** (`schedule_delay`) — how much requests are waiting to be scheduled onto
  the engine, i.e. queueing/admission delay under load. BR v0.2 carries no millisecond
  "schedule delay" field, so this is surfaced as a **queue-depth proxy** (requests waiting;
  the entry is flagged `proxy: true` and labelled accordingly). Lower is better; a rising
  queue depth means the stack is saturated and latency tails will grow. Describe it as
  "requests waiting to be scheduled", never as a fabricated time.
- **GPU utilization** (`gpu_utilization`, %) — how busy the accelerator's compute was. High
  utilization means the GPU is the bottleneck (good capacity use, little headroom); low
  utilization with high latency means something *else* (queueing, CPU, network) is gating,
  or the load is too light to saturate the GPU. "Higher" is informational, not automatically
  "better" — interpret it next to throughput and the queue-depth proxy.

On the CPU-sim quickstart these are usually absent or meaningless (no real GPU); only lean
on them on a real GPU stack.

**Why they're sometimes absent — and how to make them appear.** `standard_metrics` is `null`
whenever the metrics PRODUCER didn't run. The producer is the benchmark's monitoring path,
activated with `flags.monitoring: true` (emits `--monitoring`) on standup/run/experiment — that
creates the PodMonitor/ServiceMonitor and scrapes vLLM `/metrics`, which is what fills
`results.observability`. So if a user wants KV-cache / GPU / queue-depth numbers and they're
coming back empty, the fix is almost always **re-run with monitoring on** (and on a CRD-less
cluster, ensure the Prometheus-operator CRDs are installed or use the opt-out). The full decision
procedure — default ON, the `prometheus_crds` probe, and the `--no-monitoring` /
`monitoring.installPrometheusCrds` knobs — lives in `knowledge/observability_monitoring.md`. Never
fabricate these numbers when the block is empty; instead explain that monitoring needs to be
enabled.

## Session-level metrics (multi-turn workloads)

The summary may also carry `session_performance` — a SECOND results block that exists ONLY
for **multi-turn** inference-perf workloads, where one *session* is a sequence of related
turns/requests (e.g. a whole chat conversation). It is **separate from** the per-request
latency/throughput numbers above: those describe individual requests; this describes whole
conversations. For a **single-turn** run (one request per "session", or no session concept)
the block is **absent and `session_performance` is `null`** — say nothing about sessions
rather than inventing them. Never fabricate session numbers; only quote what's in the block.

When present, `session_performance` has two parts:

- **`scalars`** — integer counts for the whole run: `total` sessions, `succeeded`,
  `failed`, `total_events` (all turns/requests across all sessions), `total_events_completed`,
  `total_events_cancelled`. Lead with the session success story: e.g. "110 of 112 chat
  sessions completed; 2 failed", and pair `total_events_cancelled` with `failed` — a run can
  succeed at the session level while dropping individual turns.
- **`distributions`** — per-session Statistics objects (each with a `value` stat = `units` +
  `mean`/percentiles, plus an informational `label`/`unit_hint`/`direction`). Read the
  `units` off `value` and trust it; the `direction` is for narration only, never a pass/fail:
  - **`session_rate`** (`queries/s`, higher better) — how many sessions completed per second;
    the multi-turn analogue of request throughput, i.e. conversational capacity.
  - **`session_duration`** (`s`, lower better) — wall-clock length of a whole session across
    all its turns. Convert to a human scale and **watch the tail** (`p90`/`p99`): a long-tailed
    session duration means some conversations dragged. This is the metric users *feel* in a
    multi-turn chat, distinct from per-turn TTFT.
  - **`events_per_session`** (`count`, direction none) — turns per conversation; workload
    SHAPE, not quality. Use it to characterise the run ("~12 turns per chat"), not to judge it.
  - **`events_cancelled_per_session`** (`count`, lower better) — dropped turns per session; a
    rising value signals the stack shedding turns under multi-turn load.
  - **`input_tokens_per_session`** / **`output_tokens_per_session`** (`count`, direction none)
    — prompt/generated tokens accumulated over a whole session (context grows across turns);
    cost/context-size shape, not a quality signal.

Caveats: `session_performance` is emitted by **multi-turn inference-perf only** — a guidellm
or single-turn inference-perf report won't have it, and its absence is normal, not a failure.
The committed BR v0.2 JSON Schema lags the live models and doesn't yet declare this block, so a
multi-turn report lists it under `schema_deviations` (a non-fatal "report newer than schema"
note) — validation still passes and the numbers are real; mention the deviation only if asked.
The field catalogue lives in `knowledge/standard_metrics.yaml` (`session_performance`); the
parsing is pure mechanism in `app/validation/report.py` (`extract_session_performance`).

### Trending these over time

These three standard/serving metrics also flow into the cross-session trend store, so
`result_history` can chart them like any latency/throughput metric: `action="trend"` with
`metric` one of `kv_cache_hit_rate`, `gpu_utilization`, or `schedule_delay`. A few caveats
that are *your* judgment, not the tool's:

- The trend's `better` label is **informational only** (it never decides pass/fail). For
  `gpu_utilization`, `better: higher` just means "more utilized" — a rising GPU-util trend is
  not automatically good; read it alongside the throughput trend (more util + more throughput =
  healthier capacity use; more util + flat throughput = wasted work or contention).
- Only trend **comparable** runs: the same model/stack and harness, with **monitoring on for
  every point**. A run without monitoring contributes no point (the series skips it), so a
  short or gappy series usually means monitoring wasn't on for some runs — not a regression.
  Filter with `filter_tag` / `filter_model` to keep the series apples-to-apples.
- A **rising `schedule_delay`** (the queue-depth proxy, requests waiting) across stored runs
  signals **growing saturation** — the stack is increasingly queue-bound and latency tails will
  follow. Pair it with the latency trends; a climbing queue-depth that tracks climbing TTFT is
  a capacity story, not noise.
- A **falling `kv_cache_hit_rate`** across runs often *explains* a TTFT/throughput regression
  (less prefix reuse ⇒ more prefill work); use it as the "why", not as a separate SLO.

## How to talk about it
- Lead with the answer to the user's question (e.g. "for a chat UX, first-token latency
  averaged X and the slowest 1% were Y").
- Pair a latency metric with a throughput metric — they trade off.
- If `schema_deviations` is non-empty, the report is newer than the pinned schema; the
  numbers are still usable — mention it only if relevant.
- If `valid == false` or the report wasn't found, say so plainly and show the run's stderr
  tail; do NOT invent metrics.
- **A found report is whatever exists on disk — not proof it is the run the user means.**
  `locate_and_parse_report` returns the leftover/most-recent report it can find; it is NOT
  tied to a job-id, session, or date you didn't verify. So:
  - **Don't attribute a found report to a user-named job/crash.** If the user cites a specific
    (possibly invented) job-id and the tool returns an unrelated leftover report, do not narrate
    "your job completed before the crash" — say the report you found can't be confirmed to be
    that job, and ask them to point at the actual run dir / re-run.
  - **Don't adopt the user's "today's" / "latest" framing for an undated report.** A SIMULATE
    report carries no generation timestamp you can trust; don't label a leftover report "Today's
    report" or "your latest run" when you can't confirm when it was produced. State it's a
    previously-generated report of unknown age, or re-run to get a fresh, dated one.
- Then offer the useful next step — lean toward **saving this as a baseline and trending /
  comparing future runs**, not just teardown or run-again. `analyze_results` returns a ranked
  `next_steps` list for this; make ONE concise offer from its top item (see
  `knowledge/conversation_style.md` "After a benchmark" and `knowledge/history.md`).

## Honesty about scale
The quickstart uses a **simulated** engine on CPU with one tiny replica. Numbers prove the
pipeline works; they are **not** representative of real GPU serving performance, and
routing benefits (load/prefix-aware) don't show with a single replica. Say this clearly.
