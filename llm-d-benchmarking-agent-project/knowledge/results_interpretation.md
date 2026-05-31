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

Each metric carries `units` — respect them (e.g. TTFT may be in seconds `s` or `ms`).

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
