# Capacity pre-flight (check_capacity) — "will this fit?"

A real `standup` runs the benchmark repo's **capacity planner** as a sanity check: it sizes
the model (weights + activation + non-torch + CUDA-graph memory) and the KV cache against
the GPU memory you've configured, and verifies tensor-parallelism and max-context-length are
valid for the model's architecture. When that check fails, a `standup` either halts or runs
on and OOMs minutes later with an opaque pod error.

`check_capacity` runs **that same planner** *before* you deploy, so you can tell the user
"this won't fit, here's why" at the plan gate instead of after a long, failed standup. It is
read-only and auto-runs.

## When to call it

Call it **right after `propose_session_plan` is approved and BEFORE any mutating step**
(`ensure_repos`/`run_setup`/`standup`/`run`). The natural order is:

1. `propose_session_plan` → user approves the shape.
2. `check_capacity(spec=<plan.spec>, overrides=…)` → confirm feasibility.
3. If feasible, proceed to standup. If infeasible, do **not** stand up — explain and adjust.

It needs the benchmark venv (the planner package lives there). If the verdict comes back
`ran: false` with a "missing planner" error, run `run_setup` (install.sh) first, then retry.

## Reflecting the conversation with `overrides`

The spec carries defaults (e.g. `cicd/kind` is `facebook/opt-125m`, CPU-sim, no GPU;
`examples/gpu` is a single-GPU NVIDIA path). When the user wants something different, pass
`overrides` so the pre-flight checks what they actually asked for — not the stock spec:

- `model` / `huggingface_id` — a different served model (this is what makes the check
  meaningful: a 70B model on one 24 GB GPU will not fit).
- `max_model_len` — longer context costs KV-cache memory *per request*; this is the most
  common reason a model "loads but can't serve a single request".
- `gpu_memory_gb` — per-GPU memory (e.g. 24, 40, 80). Without it, GPU-memory checks are
  skipped and you only get parallelism / context-length validation.
- `gpu_memory_utilization` — fraction of GPU memory vLLM may use (e.g. 0.9).
- `accelerator_count`, `tensor_parallelism`, `data_parallelism` — the parallelism shape.
- `decode_replicas` / `prefill_replicas` — for the disaggregated (modelservice) path.

## Reading the verdict (facts → your judgment)

The result echoes the planner's own diagnostics, bucketed:

- `feasible: true` — no hard-fail / error line. Proceed. `warnings` may still note things
  worth telling the user (e.g. some GPUs idle, low concurrency).
- `feasible: false` + `will_fail: true` — the planner emitted **`DEPLOYMENT WILL FAIL`**.
  Do **not** deploy. Two flavors, both in `errors`/`diagnostics`:
  - *Insufficient GPU memory to load model* — weights + activation exceed available memory.
    Suggest: a smaller/quantized model, more tensor-parallelism (more GPUs), or a bigger GPU.
  - *Model loads but cannot serve any requests* — no KV-cache headroom for even one request
    at `max_model_len`. Suggest **reducing `max_model_len`** first (cheapest fix), then more
    GPUs or a bigger GPU, or a higher `gpu_memory_utilization` (cautiously — it can OOM).
- `errors` mentioning **invalid TP** (`TP=… is invalid … Valid values: […]`) — pick a
  tensor-parallelism from the listed valid divisors of the model's attention heads.
- `errors` mentioning **maxModelLen exceeds the model limit** — lower `max_model_len` to at
  or below the model's max context.

The sizing `info` lines (parameters, memory required, allocatable KV cache, **max concurrent
requests**) are useful even on the feasible path: surface "max concurrent requests" when the
user cares about how many simultaneous users a config can serve.

## `enforce`

By default the planner tags shortfalls as advisory `WARNING` (matching the repo's
`ignoreFailedValidation: true`, which the kind/sim path relies on — its `gpuMemoryUtilization`
is 0, so GPU checks are skipped by design and TP=0 warnings are expected and harmless). Pass
`enforce: true` to get the strict, deployment-halting `ERROR` read — use it when the user is
about to commit real GPU time and wants the same gate a production standup would apply.

## Limits

- The check looks up the model's config on **HuggingFace**; a gated model needs `HF_TOKEN`
  configured in the backend, and an offline/unreachable network yields `ran: false` (proceed
  with caution, the verdict is unavailable — it is not a green light).
- For the CPU **sim** (`cicd/kind`), GPU-memory checks are intentionally skipped; the
  pre-flight there mainly confirms the model config is reachable and flags nothing fatal.
- This validates *capacity*, not cluster *availability* — whether a node actually has that
  GPU or that much memory free is a separate `probe_environment` / scheduling concern.
