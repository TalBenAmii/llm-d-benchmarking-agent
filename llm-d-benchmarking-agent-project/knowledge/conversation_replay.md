# conversation_replay load semantics (think-time, recycling, run length — multi-turn "N users" modeling)

The inference-perf `agentic_code_generation.yaml` profile (`data.type conversation_replay`)
replays multi-turn conversations with per-turn **think-time** (`tool_call_latency_sec`). Its
concurrency model is subtle and users routinely mis-model it. Verified against **inference-perf
v0.6.0** — the tag `llm-d-benchmark`'s `build/Dockerfile` bakes into the harness image (facts
below hold for that pin; re-verify if the image tag moves).

- **First, route by the user's GOAL.** If the user's actual goal is **decoupling active users from
  in-flight concurrency** (thinking users must NOT hold a slot — "N active users, C < N concurrent"),
  inference-perf CANNOT express it; do NOT conclude "no harness supports this" —
  `read_knowledge('multi_harness')` §"Route by the user's GOAL" routes to aiperf (user-centric
  steady-state) or guidellm (`requeue_delay` releases the slot). Everything below assumes
  inference-perf `conversation_replay` IS the right harness.
- **Think-time HOLDS the concurrency slot.** A worker acquires the concurrency semaphore
  *before* dequeuing a turn and releases it only in `finally` after the whole request — the
  think-time sleep happens *inside* the request, while the slot is still held. So a "thinking"
  user still occupies a concurrency slot. **"200 active users at concurrency 100" does NOT model
  200 users** — only `concurrency` conversations are ever in flight, think-time included. To model
  N truly-concurrent users you must set concurrency = N (see the recipe below), not add think-time
  to a smaller pool.
- **Conversations are RECYCLED by default** (closed-loop replenishment): when a conversation
  finishes, its slot resets to a fresh conversation at turn 0 (the data generator is an infinite
  cycle). You do **not** need a workaround to keep load steady. **Sizing recipe:**
  `num_requests = concurrency × turns_per_conversation × num_rounds`, and treat the **first round
  as warmup** (cold cache). This is how you dial run length, not `request_timeout`.
- **`request_timeout` is a PER-REQUEST HTTP timeout** (aiohttp `ClientTimeout(total=…)`), not a
  whole-run wall-clock budget. Unset → aiohttp's default (~5 min) per request. Do not compute run
  duration from it, and do not use `mean_turns > timeout/latency`-style formulas — they are bogus.
- **The `concurrent` load type has NO wall-clock cap.** The harness overwrites the stage to
  `duration=1, rate=num_requests`; the run ends when `num_requests` complete — full stop. (A
  `timeout` stage field exists only on the *trace*-session-replay load stage, not on `concurrent`.)
  So run length is governed by `num_requests`, per the recipe above.
- **Which knob caps concurrency depends on the load type.** Under `concurrent`, the per-stage
  **`concurrency_level`** REPLACES `worker_max_concurrency` (it is divided across workers and
  rebuilds the semaphore). For `constant`/`poisson` load types the cap is
  `num_workers × worker_max_concurrency` instead. (The key is **`worker_max_concurrency`** — not
  `max_concurrency` / `num_concurrent_requests`, which don't exist.)

## "N concurrent users" → which knob to set (recipe)

When a user says "~500 concurrent users", engage the number — the stock workload ladders top out
far below that, so a stock profile silently under-tests. Map it to the load config, don't ignore it:

- **`conversation_replay` (concurrent):** set **`concurrency_level = N`** on the stage (this is the
  in-flight cap; think-time users still count against it, per above), then size the run with
  `num_requests = N × turns × num_rounds`.
- **`constant`/`poisson` synthetic:** N in-flight ⇒ ensure `num_workers × worker_max_concurrency ≥ N`
  (raise `worker_max_concurrency` or `num_workers`); for rate-based load, target *rate* follows from
  Little's Law (`rate ≈ N / mean_request_latency`), not `rate = N`.
- Confirm the profile's actual defaults against the live catalog before quoting a ladder; a
  non-expert's "500 users" almost always needs an explicit override, not a stock profile.
