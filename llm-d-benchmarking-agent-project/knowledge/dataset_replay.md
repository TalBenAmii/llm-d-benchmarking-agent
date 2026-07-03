# Dataset replay vs synthetic workload profiles

The benchmark can drive load in two fundamentally different ways. Decide WHICH one fits the
user's question, then explain your choice — this is judgment, not a default.

1. **Synthetic workload profile (the default).** The workload profile (e.g.
   `sanity_random.yaml`, `chatbot_synthetic.yaml`, `shared_prefix_synthetic.yaml`) *generates*
   requests from a statistical description: input/output length distributions, an arrival
   pattern, a shared-prefix structure, a concurrency or request-rate sweep. Reproducible,
   tunable, and self-contained — nothing to download. This is the right tool for stress tests,
   apples-to-apples comparisons, parameter sweeps (DoE), and "what's the ceiling?" questions.

2. **Dataset replay (`-x`/`--dataset`).** Instead of generating requests, the harness REPLAYS a
   *real* dataset — actual prompts (and, for some formats, real arrival timings/traces). You
   supply it via `execute_llmdbenchmark(flags={"dataset": "<url-or-path>"})`, which emits
   `-x <url>` to the CLI. Use this when the user wants *representative-of-production* numbers: a
   captured trace of their own traffic, a public conversational dataset (ShareGPT), or a fixed
   request set they must benchmark against. Real prompt length/shape and (for trace formats)
   real timing are things a synthetic profile only approximates.

## When to replay a dataset (your judgment)

Prefer **dataset replay** when the user says any of: "use real/production traffic", "replay our
trace", "benchmark against ShareGPT / this dataset", "I have a captured request log", or when
the realism of prompt content/timing materially changes the answer (e.g. cache-hit behavior on
real shared prefixes, or bursty real arrival patterns).

Prefer the **synthetic profile** (omit `dataset`) for: controlled stress tests, sweeps over
concurrency/rate/parallelism, A/B of two stack configs, capacity ceilings, and any "sanity"
run — anything where reproducibility and a clean knob to turn matter more than literal realism.

If unsure, ask the user whether they want *representative* (their data) or *controlled*
(synthetic) load — don't guess silently.

## How it works (mechanism — for grounding, not decisions)

- `-x`/`--dataset` is upstream-valid **only on `run` and `experiment`** — never on
  standup/plan/smoketest/teardown. The agent's `build_argv` enforces this (it emits `-x` for
  those two subcommands only), and the allowlist (`security/allowlist.yaml`,
  `value_constraints.dataset_url`) permits the flag only there.
- You pass a **URL or path**. Accepted forms: `http(s)://…`, `hf://…` (HuggingFace),
  `gs://…` (GCS), `s3://…` (S3), or a workspace/cluster filesystem path. Example trace URL:
  `https://github.com/alibaba-edu/qwen-bailian-usagetraces-anon/raw/refs/heads/main/qwen_traceA_blksz_16.jsonl`.
- The **workload profile still matters**: a dataset-aware profile maps the replayed dataset
  into requests. Pick a profile that consumes a dataset when you replay one — a purely
  synthetic profile won't read it. The dataset-/trace-aware profiles on disk (verbatim names +
  their `data.type`):
  - **vllm-benchmark** `sharegpt.yaml` (`dataset-name sharegpt`) and `fixed_dataset.yaml` — a
    fixed request set.
  - **aiperf** `dataset.yaml` (`custom-dataset-type mooncake_trace`) — a Mooncake trace replay.
  - **inference-perf** `chatbot_sharegpt.yaml` (`data.type shareGPT`),
    `agentic_code_generation.yaml` (`data.type conversation_replay`), and `otel_traces.yaml`
    (`data.type otel_trace_replay`, `load.type trace_session_replay`).
  Trace formats (otel / mooncake) carry real ARRIVAL timing, not just prompt content, so they
  reproduce bursty production arrival patterns. NOTE: `guide_wide-ep-lws_1.yaml` is NOT a replay
  — its `data.type` is `random` (synthetic), so don't reach for it when the user wants a trace.
- **No env var to set.** The CLI itself derives `LLMDBENCH_RUN_DATASET_DIR` and
  `LLMDBENCH_RUN_DATASET_FILE` from the URL during profile rendering (a trailing `/` means a
  directory replay; otherwise it splits the URL into dir + filename). Those `RUN_DATASET_*`
  tokens are downstream-internal to the harness — the agent only emits `-x <url>` and never
  sets any `LLMDBENCH_*` dataset env var itself.

## `conversation_replay` load semantics (multi-turn think-time — read before you model "N users")

The inference-perf `agentic_code_generation.yaml` profile (`data.type conversation_replay`)
replays multi-turn conversations with per-turn **think-time** (`tool_call_latency_sec`). Its
concurrency model is subtle and users routinely mis-model it. Verified against **inference-perf
v0.6.0** — the tag `llm-d-benchmark`'s `build/Dockerfile` bakes into the harness image (facts
below hold for that pin; re-verify if the image tag moves):

- **Think-time HOLDS the concurrency slot.** A worker acquires the concurrency semaphore
  *before* dequeuing a turn and releases it only in `finally` after the whole request — the
  think-time sleep happens *inside* the request, while the slot is still held. So a "thinking"
  user still occupies a concurrency slot. **"200 active users at concurrency 100" does NOT model
  200 users** — only `concurrency` conversations are ever in flight, think-time included. To model
  N truly-concurrent users you must set concurrency = N (see the recipe below), not add think-time
  to a smaller pool.
- **If the user's actual GOAL is decoupling active users from in-flight concurrency** (thinking
  users must NOT hold a slot — "N active users, C < N concurrent"), inference-perf cannot express
  it; do NOT conclude "no harness supports this" — `read_knowledge('multi_harness')` §"Route by
  the user's GOAL" routes to aiperf (user-centric steady-state) or guidellm (`requeue_delay`
  releases the slot).
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

### "N concurrent users" → which knob to set (recipe)

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

## Reading the results

When you replay a dataset, **say so** in your summary: the numbers reflect that specific
dataset's prompt distribution and (if a trace) its timing, not a synthetic ideal. They are NOT
directly comparable to a synthetic-profile run of the "same" workload — different request
content/shape. For an A/B, keep load source constant on both sides (both synthetic, or both the
same dataset). Gated/private datasets may need credentials the same way gated models do; if a
replay fails to fetch the dataset, treat it like an access problem, not a benchmark failure.
