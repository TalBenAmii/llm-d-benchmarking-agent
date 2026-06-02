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
