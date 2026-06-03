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
- The **workload profile still matters**: a dataset-aware profile (e.g. vllm-benchmark's
  `sharegpt` / `fixed_dataset`) maps the replayed dataset into requests. Pick a profile that
  consumes a dataset when you replay one — a purely synthetic profile won't read it.
- **No env var to set.** The CLI itself derives `LLMDBENCH_RUN_DATASET_DIR` and
  `LLMDBENCH_RUN_DATASET_FILE` from the URL during profile rendering (a trailing `/` means a
  directory replay; otherwise it splits the URL into dir + filename). Those `RUN_DATASET_*`
  tokens are downstream-internal to the harness — the agent only emits `-x <url>` and never
  sets any `LLMDBENCH_*` dataset env var itself.

## Reading the results

When you replay a dataset, **say so** in your summary: the numbers reflect that specific
dataset's prompt distribution and (if a trace) its timing, not a synthetic ideal. They are NOT
directly comparable to a synthetic-profile run of the "same" workload — different request
content/shape. For an A/B, keep load source constant on both sides (both synthetic, or both the
same dataset). Gated/private datasets may need credentials the same way gated models do; if a
replay fails to fetch the dataset, treat it like an access problem, not a benchmark failure.
