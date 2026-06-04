# Multi-stack scenarios: --stack subset + --parallel cap

Some scenarios are **multi-stack**: they deploy **N independent model pools behind one
gateway**, each serving a different model on its own URL path. The canonical example is
`guides/multi-model-wva` (the `multi-model-wva` example scenario), whose `scenario:` list holds
two stacks — `qwen3-06b` and `llama-31-8b` — each its own deployment/service/route, routed at
`/{stack.name}/...`. A single-stack scenario (the Kind MVP `cicd/kind`, `guides/optimized-baseline`)
has exactly one pool, so NOTHING here applies — `--stack`/`--parallel` are inert and you should
not set them. This is judgment about WHICH pools and HOW MANY at once; it is not a default.

## Two mechanisms (both pure flag emission; you decide the values)

These are emitted by `execute_llmdbenchmark(flags={...})` — mechanism only. The WHICH/HOW-MANY
choice below is yours.

- **`stack` → `--stack NAME[,NAME...]` — the SUBSET selector.** Restricts the command to one
  stack or a comma-separated subset of the scenario's pools. Valid on
  **standup / smoketest / run / teardown** (upstream rejects it on plan/experiment). Omit it and
  the command operates on **every** stack. The names come from the scenario's own `scenario:`
  list (read the spec; do not guess) — unknown names fail loudly upstream.

- **`parallel` → `--parallel <int>` — the per-pool concurrency CAP.** Caps how many stacks are
  deployed / smoketested **in parallel** (upstream default **4**). Valid on
  **standup / smoketest / experiment** (NOT run, NOT teardown). On `run`, the analogous knob is
  the SEPARATE `parallelism`/`-j` flag, which is the number of parallel **harness pods**, NOT
  stacks — do not confuse the two.

## WHICH stack(s) to target (`stack`)

- **Benchmark / debug ONE model of a multi-model deploy without disturbing the others.** The
  user asks "how does the qwen pool do under load?" on a `multi-model-wva` deploy → `run` with
  `flags={"stack": "qwen3-06b"}`. The endpoint URL auto-resolves for the selected stack, so you
  do NOT also pass `endpoint_url`. The sibling `llama-31-8b` pool keeps serving, untouched.
- **Re-deploy a single pool after a partial failure.** Only one stack's standup failed (the
  others are healthy) → `standup` with `flags={"stack": "<failed-name>"}` to bring just that pool
  back, instead of tearing the whole scenario down and redoing it. Combine with `step` (Phase 31)
  to re-run a single step of that one stack.
- **Tear down one pool, leave the rest.** `teardown` with `flags={"stack": "<name>"}` removes
  that pool only. Note: when `--stack` is set, the per-namespace WVA controller is **preserved**
  (sibling stacks still need it); a full teardown (no `--stack`) also uninstalls the controller.
  (`--deep` overrides this and always uninstalls the controller.)
- **A subset.** Two of three pools → `flags={"stack": "qwen3-06b,llama-31-8b"}` (comma-separated,
  no spaces).
- **Omit `stack`** for the normal case: stand up / smoketest / benchmark / tear down the **whole**
  multi-stack scenario at once. This is the default and is correct unless the user's question is
  scoped to a specific model.

## WHEN to cap parallelism (`parallel`)

Default (4) is fine on a roomy multi-node cluster. **Lower it** when bringing several pools up at
once would overwhelm the cluster:

- **Small / single-node / Kind cluster.** One node cannot schedule several model-server pools
  simultaneously — they fight for CPU/memory and sit in `Pending`/`FailedScheduling`. Set
  `flags={"parallel": 1}` on `standup`/`smoketest` to deploy the pools **one at a time**
  (serially). On the Kind MVP this is the safe choice for any multi-stack scenario; pair it with
  `harness_cpu_nr` (Phase 61, knowledge/harness_sizing.md) and the capacity pre-flight.
- **Limited accelerators.** Fewer GPUs than pools → cap `parallel` so you never try to schedule
  more model servers than the cluster can hold at once.
- **Leave `parallel` unset** (default 4) on a cluster with ample headroom — capping it below the
  pool count only slows the deploy with no benefit.

`parallel` is also a DoE-`experiment` knob (max stacks deployed in parallel per treatment); the
same small-cluster reasoning applies there.

## Interplay & guardrails

- `--stack` does NOT change a command's mode: a `standup`/`run`/`teardown` scoped to one stack is
  still **mutating** and stays approval-gated; only `--dry-run`/`-n` previews it.
- `--stack`/`--parallel` compose with the other modeled flags (`monitoring`, `step`, `dataset`,
  `models`, `kubeconfig`, …) — they ride alongside, they don't replace.
- Stack names are value-pinned by the allowlist (`stack_list` — RFC1123 labels, comma-separated)
  and `--parallel` by `positive_int`; an injection-laden value is refused. You still must use a
  name that actually exists in the scenario.
- For a SINGLE-stack scenario, do not set either flag — there is nothing to subset or cap.
