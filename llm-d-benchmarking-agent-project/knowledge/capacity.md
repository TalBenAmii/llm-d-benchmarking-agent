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
  meaningful: a 70B model on one 24 GB GPU will not fit). When the standup itself will carry
  an explicit model override (`ExecuteInput.models`, emitted as `-m`, see
  `knowledge/model_override.md`), you MUST pass that SAME id here as
  `overrides={'model': '<id>'}` so the pre-flight sizes and gated-access-checks the IDENTICAL
  model you are about to deploy — not the spec's stock default. The two must always match.
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

### "How many concurrent users?" — use Little's Law, never throughput-as-concurrency

When the user asks **how many simultaneous/concurrent users** a config supports, **do not equate
replies-per-second (throughput) with concurrent users** — they're only equal if a reply takes
exactly 1 second. Concurrency and throughput relate by **Little's Law**:

> **concurrent ≈ throughput × per-request latency.**

So replies/s must be multiplied by how long a reply takes. Example of the trap: 5000 tok/s with
~100-token replies = ~50 replies/s, but at ITL ≈ 47 ms/token a 100-token reply takes ~4.7 s, so
concurrency ≈ 50 × 4.7 ≈ **~235** in flight — **not** "50 concurrent users". Quoting 50 here is
both ~5× low and internally inconsistent with your own latency numbers. Either apply Little's Law
explicitly (and show the per-request latency you used), or defer the number to the capacity
pre-flight's **"max concurrent requests"** sizing line above — don't do throughput-as-concurrency
back-of-envelope math.

### Borderline "just fits" — leftover after weights is NOT all KV cache

Memory left after the weights does **not** all become KV cache / concurrency headroom. The engine
also needs **activation + runtime overhead** — CUDA context, activation buffers, CUDA-graph
capture, the framework's own working set — on the order of **~2–5 GB per GPU** before a single
request's KV cache. The planner's sizing accounts for this, but when *you* eyeball a borderline
result, don't spend that overhead as if it were serving capacity:

- When leftover memory is thin (the model loads with only a couple of GB to spare), the honest
  verdict is **"this will likely fail to start / OOM on load"**, NOT "it fits, good for ~N
  concurrent chats." A config that barely loads has **no** headroom to quote a concurrency number
  from — quoting "N concurrent users" there is doubly wrong (it may not even boot).
- Reach for a concurrency figure **only** off the planner's own **"max concurrent requests"**
  sizing line (which already nets out weights + overhead), never from raw leftover memory. If that
  line is small or absent on a borderline fit, say the config is memory-bound and suggest the same
  fixes as a hard fail (smaller/quantized model, more GPUs/TP, bigger GPU, lower `max_model_len`).

## Gated-model access pre-flight — "can your token even pull the weights?"

`check_capacity` pairs the "will it fit?" sizing verdict with a **gated-model access**
pre-flight, using the benchmark repo's OWN gating check (`check_model_access` /
`GatedStatus`). The point: a gated model whose weights your HuggingFace token can't pull
fails the standup minutes in, with an opaque image-pull / weights error. This surfaces the
exact verdict **up front, at the plan gate, before any mutating step** — so a non-expert
hears "your token can't pull this model, here's the fix" instead of watching a long deploy
die. The result carries three facts (plus a per-model `gated_access.models` breakdown):

- **`gated`** — `true` if any served model is gated, `false` if all are public, `null` if
  the gating check couldn't run (offline / HF unreachable — see below).
- **`authorized`** — for the gated case: `true` if your `HF_TOKEN` can pull **every** gated
  model, `false` if it can't pull at least one, `null` if access couldn't be determined.
  `null` for the public case (no token is needed). `gated_reason` is the upstream detail
  text (it never contains the token).

Read the three situations and say this (the **decision is here, not in Python**):

- **PUBLIC** (`gated: false`) — *no token needed.* Say nothing about tokens; just proceed to
  the capacity verdict. Don't ask the user for an HF token for a public model.
- **GATED + AUTHORIZED** (`gated: true`, `authorized: true`) — *your token can pull it.*
  Tell the user this model is gated but their configured HuggingFace token has access, so
  the deploy can proceed. Continue to the "will it fit?" verdict.
- **GATED + UNAUTHORIZED** (`gated: true`, `authorized: false`) — **do not stand up.**
  Explain plainly: *"This model is gated and the HuggingFace token the backend has can't
  pull it."* Quote `gated_reason` (it tells them whether **no token** is configured, or the
  token simply **lacks access**). Then offer the fix:
  - If **no token** is configured: they need to provide one. Offer to **provision the
    `HF_TOKEN` secret (Phase 30 secret-provisioning)** so the backend has a token to use —
    that is the next step to suggest, approval-gated like any secret write.
  - If a token **lacks access**: the token is fine but this account isn't approved for the
    model — point them to `https://huggingface.co/<model>` to request access, then retry the
    pre-flight once granted. (Provisioning a *different* token with access is also valid.)
  Either way, retry `check_capacity` after the fix to confirm `authorized: true` before
  standing up.

  ### Provisioning the cluster HF secret (`provision_hf_secret`)

  The "no token" case has a concrete, in-agent fix. A gated-model standup pulls the weights
  using a cluster Secret (the upstream `llm-d-hf-token`); if the backend HAS a real
  `HF_TOKEN` configured but that Secret was never created in the target namespace, the
  standup fails minutes in with an opaque image-pull/weights error. When the gated verdict's
  `gated_reason` indicates **no token is configured cluster-side** (vs. a token that simply
  lacks access), OFFER the approval-gated mutating step:

  > `provision_hf_secret(namespace=<the plan's namespace>, name='llm-d-hf-token')`

  This materializes the cluster HF token Secret from the backend's `HF_TOKEN`, exactly as
  `llm-d/helpers/hf-token.md` does. It is **approval-gated** (it writes a Secret to the
  cluster): **CALL the `provision_hf_secret` tool** — calling it IS how you propose it, because
  the user consents at the approval prompt the call raises. So "propose it / never run it
  unprompted" means *call the approval-gated tool* (the gate is the prompt) — it does **not**
  mean describe it in prose or defer via `suggest_next_steps`. State plainly alongside the call:

  - It must run **before** the standup (a gated standup can't pull weights without it).
  - You **never see the token** — it is read backend-side and never shown, never logged,
    never in the argv/command events. You only see kubectl's confirmation line.
  - After it succeeds, **re-run `check_capacity`** (same model/overrides) to confirm
    `authorized: true`, and only THEN proceed to standup.
  - `name` defaults to the upstream `llm-d-hf-token`; only override it if the deployment was
    configured to read a differently-named Secret.

  ### Boundaries (when NOT to provision)

  - **Never for a PUBLIC model** (`gated: false`). A public model needs no token and no
    Secret — say nothing about tokens and just proceed to the sizing verdict.
  - **A token that merely LACKS access is NOT a provisioning case.** If `gated_reason` says
    the configured token does not have access to this model, a new Secret won't help — the
    account isn't approved. Point the user to `https://huggingface.co/<model>` to request
    access (or to provide a *different* token that has access), then retry `check_capacity`.
    Provisioning the same access-less token would just re-create a Secret that still can't
    pull the weights.
  - **Only when the backend actually has an `HF_TOKEN`.** If none is configured at all, the
    provisioning step can't materialize a Secret (it reports that plainly) — the user must
    first set `HF_TOKEN` in the backend env (it stays backend-only), then provision.
- **UNKNOWN** (`gated: null`) — the gating check couldn't run (HF unreachable / offline, or
  the repo's gating util wasn't importable). `gated_reason` says "gated check unavailable".
  Treat it like the `ran: false` capacity case: **not a green light** — tell the user the
  gated verdict is unavailable and let them decide whether to proceed with caution.

The **token stays backend-only**: it's read from the scrubbed child env, passed to the
gating check, and never echoed into the result, the command events, or the logs. You will
never see the token value — only these gated/authorized/`gated_reason` facts.

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
  `advise_accelerators` + `read_knowledge('accelerators')` closes exactly that gap: it detects
  whether a node ADVERTISES an accelerator extended resource (`nvidia.com/gpu` or the
  amd/gaudi/tpu/xpu siblings) vs CPU-only, and carries the real (non-sim) CPU-only 64c/64GB-per-
  replica floor (Kind/CPU-sim exempt) plus the CUDA/driver minimums and the Device-Plugin-vs-DRA
  choice. Pair the two: `check_capacity` answers "will the model FIT in the accelerator's
  memory?", `advise_accelerators` answers "does a node even ADVERTISE that accelerator / meet the
  CPU floor?".
