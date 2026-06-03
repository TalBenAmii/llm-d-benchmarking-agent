# Interpreting the Benchmark Report (for non-experts)

`locate_and_parse_report` returns a validated summary. Translate it into plain language,
always tied to the user's stated goal. Never quote numbers that aren't in the summary.

## The metrics that matter
- **TTFT (time to first token)** — how long until the first token appears. This is the
  "responsiveness" users feel in a chat. Lower is better. Watch `mean` and `p90`/`p99`
  (tail latency — the slow requests).
- **TPOT / ITL (time per output token / inter-token latency)** — the pace of streaming
  after the first token. Lower = text appears faster. Drives "tokens/sec per user".
- **request_latency** — end-to-end time for a whole request.
- **throughput.total_token_rate** — total tokens/sec the system pushed (capacity).
- **throughput.request_rate** — requests/sec completed.
- **success_rate_pct** — fraction of requests that succeeded; flag anything below ~100%.

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

## Units — read them off the report, never guess

Every latency/throughput entry in the summary carries an explicit `units` field. **Read it and
trust it. Never infer a unit from how big or small a number looks.**

- BR v0.2 reports latency in **seconds**: TTFT and `request_latency` are `units: s`; TPOT and
  ITL are `units: s/token`. A `mean` of `0.13` with `units: s` is **0.13 seconds = 130 ms**, not
  130 ns and not 130 ms-raw.
- When you narrate latency to a non-expert, **convert seconds → milliseconds** (×1000) and label
  it `ms` (e.g. `ttft.mean = 0.13 s` → "first token in ~130 ms"; a `tpot` of `0.021 s/token` →
  "~21 ms per token"). Milliseconds are the canonical unit users expect.
- **Never report nanoseconds or microseconds.** BR v0.2 carries no ns/µs latency — if you find
  yourself writing "nanoseconds", you misread a `units: s` value. There is no nanosecond field.
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
`monitoring.installPrometheusCrds` knobs — lives in `knowledge/observability.md` (§3). Never
fabricate these numbers when the block is empty; instead explain that monitoring needs to be
enabled.

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

## Honesty about scale
The quickstart uses a **simulated** engine on CPU with one tiny replica. Numbers prove the
pipeline works; they are **not** representative of real GPU serving performance, and
routing benefits (load/prefix-aware) don't show with a single replica. Say this clearly.
