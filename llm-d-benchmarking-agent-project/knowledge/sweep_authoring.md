# Authoring a sweep — picking factors & the workload grid

Companion to `read_knowledge('sweep_playbook')` (which picks the sweep shape and lists the
ready-made experiment files). This is how you AUTHOR a custom grid when none of those fits.
Before trusting any override key you pick here, check `read_knowledge('sweep_validity')` — some
dotted keys silently do nothing.

## Generate the matrix with `generate_doe_experiment` (you choose the factors)

`generate_doe_experiment` is the MECHANISM that turns *factors × levels* into a full, deduped,
named **treatments** matrix and emits a valid experiment YAML. **YOU decide which factors and
levels to sweep — that judgment is below; the tool never picks for you.**

Each factor is `{name, key, levels}`:
- `name` — a short token used to build treatment names (e.g. `tp`, `rep`, `numCpuBlocks`).
- `key` — the **dotted override key** the level sets. **Read the repo's experiment examples
  first** (`read_repo_doc(path="llm-d-benchmark/llmdbenchmark/experiment/README.md")`,
  `read_repo_doc(path="llm-d-benchmark/docs/doe.md")`, and the files under the repo-root
  `experiments/` dir) to pick keys that actually exist. Setup-phase keys override the
  scenario config (e.g. `decode.parallelism.tensor`, `vllmCommon.flags.numCpuBlocks`); run-phase
  keys override the workload profile (e.g. `data.shared_prefix.num_groups`, `rate`).
  ⚠️ Keys must resolve to a **dict path** — list-indexed keys (`load.stages.0.*`) never apply;
  see `read_knowledge('sweep_validity')` before picking one.
- `levels` — the scalar values to sweep, e.g. `[2, 4, 8]`.

`run_factors` is required (workload knobs, swept against one stack). `setup_factors` is optional,
only for a **full DoE** where the *deployment itself* changes (each gets its own standup/teardown);
the emitted matrix is `setup × run`. Hold everything you are NOT sweeping fixed via
`setup_constants` / `run_constants` so the deltas are attributable. The tool returns the workspace
`path`; pass it as `flags.experiments` to `execute_llmdbenchmark` (`subcommand="experiment"` for a
full DoE, `"run"` for a run-only sweep), always `flags={dry_run: true}` first.
(`write_and_validate_config` remains for a hand-built one-off config that isn't a factor sweep.)

### Picking factors for common questions
- **"What's the optimal prefill/decode ratio / split?"** — full DoE. Sweep the prefill and
  decode replica counts (and/or their tensor-parallelism) as `setup_factors`, e.g.
  `decode.replicas: [1,2,4]` × `prefill.replicas: [1,2]`. Keep the model, the workload, and
  the token mix fixed. Read the actual `LLMDBENCH_VLLM_MODELSERVICE_*REPLICAS` /
  `*_TENSOR_PARALLELISM` keys from `docs/doe.md` for the scenario you're using.
- **"How does latency scale with load?"** — run-only sweep. Sweep one load knob
  (`max-concurrency` for vllm-benchmark, or `rate`/QPS) as a single `run_factor`. One stack,
  N runs. Cheapest; prefer this on kind/CPU-sim.
- **"Does prefix caching help my workload?"** — sweep the prefix knobs (`num_groups`,
  `system_prompt_len`) as `run_factors` against a prefix-cache-aware scenario (often paired
  with a `setup_factor` over the cache config). See `precise-prefix-cache-aware.yaml`.
- **"Which batch size / model is fastest?"** — the batch knob is a run factor; a different
  model changes the deployment, so model is a `setup_factor`.

**Vary one thing at a time** unless you specifically want interaction effects — then a small
full-factorial (2–3 factors, 2–3 levels each) is fine, but the matrix grows multiplicatively
(3 factors × 3 levels = 27 treatments), and a full DoE re-deploys per setup treatment, so
keep it small on a single kind cluster.

## Elicit token characteristics during the interview (drives the workload + the grid)

Before designing a sweep — and ideally during the initial interview — **explicitly elicit the
workload's token characteristics**, because they determine the workload profile AND which
factors are worth sweeping. Ask (or infer from the use case):

- **Input (prompt) length distribution** — typical and tail. Short prompts (chat turns) vs
  long context (RAG, doc QA, agents) stress *prefill* very differently. → drives
  `prompt_tokens` / context-length factors and whether prefill is the bottleneck.
- **Output (generation) length distribution** — short answers vs long completions. Output
  length dominates *decode* cost and TPOT. → drives `output_tokens` factors and the
  decode-replica/parallelism sweep.
- **System-prompt / prefix reuse ratio** — how much of each request is a shared prefix
  (a fixed system prompt, a shared RAG preamble, few-shot examples) reused across requests.
  HIGH reuse → **prefix caching** matters a lot: pick a prefix-cache-aware scenario and sweep
  the prefix knobs (`num_groups`, `system_prompt_len`) and/or the cache config. LOW/no reuse →
  prefix caching won't help; don't sweep it. Map reuse to the `shared_prefix` workload's
  `num_groups` (more groups = less sharing) and `system_prompt_len`.
- **Concurrency / arrival pattern** — steady QPS vs bursty; expected in-flight requests. →
  drives the load factor (`max-concurrency` / `rate`) range.

Tie these back to the goal: interactive/chat → low TTFT & TPOT (sweep to find the knee before
latency degrades); batch/offline → high throughput (push load until throughput plateaus).
When the user can't give exact numbers, propose a sensible default distribution from the use
case, state your assumption, and let them correct it — don't silently guess. Confirm the real
shape of any profile you name with `inspect_workload_profile` before quoting its token/load mix.
