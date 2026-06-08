# Sweeps & A/B comparison playbook

Use this when the user wants to compare configurations — "how does latency change as
concurrency grows?", "is config A faster than B?", "find the best batch size", "sweep QPS".
The mechanism is two tools; the *judgment* (what to vary, how to read the deltas) is here.

> For **goal-seeking** (the user states a *target* and wants the best operating point, with the
> next config chosen adaptively from each result — not a fixed grid), see
> `read_knowledge('autotune_strategy')` (the `autotune_search` tool).

## Decide the shape first

Ask (or infer) **what single thing varies** and **whether it changes the deployment**:

- **Run-parameter sweep (PREFERRED, esp. on the kind/CPU-sim quickstart).** The thing you
  vary is a *workload/run* knob (max-concurrency, QPS, prompt/output tokens). The stack is
  deployed **once**, then benchmarked N times. Cheap and fast.
  → `execute_llmdbenchmark(subcommand="run", flags={experiments: "<treatments.yaml>"})`
  against an already-stood-up stack. One `standup`, N runs.
- **Full DoE sweep.** The thing you vary changes the *deployment itself* (replicas, tensor
  parallelism, prefill/decode split, model). Each treatment needs its own standup+teardown.
  → `execute_llmdbenchmark(subcommand="experiment", flags={experiments: "<doe.yaml>"})`.
  Heavy (re-deploys per treatment) — only when a run-parameter sweep can't express it. On a
  single local kind cluster, prefer the run-parameter sweep; full DoE shines on real/GPU.

Keep everything else **fixed** across the sweep — vary one factor at a time, or the deltas
aren't attributable.

## Build the experiments file (read the repo truth — don't guess)

Before writing one, read the authoritative format and examples (they are read-only refs):
- `read_repo_doc(path="llm-d-benchmark/docs/doe.md")` and
  `read_repo_doc(path="llm-d-benchmark/llmdbenchmark/experiment/README.md")`.
- Ready-made examples: `workload/experiments/max-concurrency-sweep.yaml` (a run-parameter
  sweep — top-level `treatments:` list, each a name + a workload override) and
  `docs/tutorials/kubecon/experiments/smoke.yaml` (full DoE — `setup.factors`/`treatments`
  + `run.factors`/`treatments`).

If the user's grid maps onto an existing example, reuse it. If it needs a custom grid,
**author it with `generate_doe_experiment`** (see "Generate the matrix" below) — it
cross-products your factors × levels into the full treatments matrix and writes a
structurally-validated experiment YAML into the workspace. (`write_and_validate_config`
remains for a hand-built one-off config that isn't a factor sweep.)
Note: `max-concurrency` is a **vllm-benchmark** profile field; the quickstart's default
harness is `inference-perf`. Match the swept key to the chosen harness/workload, or switch
harness accordingly.

## Generate the matrix with `generate_doe_experiment` (you choose the factors)

`generate_doe_experiment` is the MECHANISM that turns *factors × levels* into a full,
deduped, named **treatments** matrix and emits a valid experiment YAML. **YOU decide which
factors and levels to sweep — that judgment is below; the tool never picks for you.**

Each factor is `{name, key, levels}`:
- `name` — a short token used to build treatment names (e.g. `tp`, `rep`, `numCpuBlocks`).
- `key` — the **dotted override key** the level sets. **Read the repo's experiment examples
  first** (`read_repo_doc(path="llm-d-benchmark/llmdbenchmark/experiment/README.md")`,
  `read_repo_doc(path="llm-d-benchmark/docs/doe.md")`, and the files under
  `workload/experiments/`) to pick keys that actually exist. Setup-phase keys override the
  scenario config (e.g. `decode.parallelism.tensor`, `vllmCommon.flags.numCpuBlocks`); run-phase
  keys override the workload profile (e.g. `data.shared_prefix.num_groups`, `rate`).
- `levels` — the scalar values to sweep, e.g. `[2, 4, 8]`.

`run_factors` is required (the workload knobs, swept against one stack). `setup_factors` is
optional and only for a **full DoE** where the *deployment itself* changes (each setup
treatment gets its own standup/teardown). The emitted matrix is `setup × run` treatments.
Hold everything you are NOT sweeping fixed via `setup_constants` / `run_constants` so the
deltas are attributable. The tool returns the workspace `path`; pass it as `flags.experiments`
to `execute_llmdbenchmark` (`subcommand="experiment"` for a full DoE, `"run"` for a run-only
sweep), always with `flags={dry_run: true}` first.

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
case, state your assumption, and let them correct it — don't silently guess.

## Always preview, then run gated

`experiment`/`run` are mutating (they deploy). Add `flags={dry_run: true}` first to preview
the plan; the user approves the real sweep. Per-treatment reports are written under the
session workspace automatically (`experiment` → `workspace/experiment`, `run` → `workspace/results`).

## Compare the results

After the sweep, call **`compare_reports`**:
- Sweep via `experiment`/`run --experiments`: pass `experiment_dir` = the output dir
  (e.g. the `results_dir` returned by the run). It finds **every** report under it.
- Two separate runs (A/B): pass `sources=[dirA, dirB]` with `labels=["A","B"]`.

It validates each report against the BR v0.2 schema and returns per-metric **deltas vs a
baseline** plus the winning run for each metric.

For SLO-aware analysis — **goodput**, SLO pass/fail filtering, and **Pareto-optimal**
config selection across the sweep — use **`analyze_results`** instead (same `sources` /
`experiment_dir` shapes, plus the `slo` targets from the approved plan). See
`knowledge/analysis.md`. Rule of thumb: `compare_reports` for raw side-by-side deltas;
`analyze_results` when the user has QoS targets or wants "the best config".

`compare_reports`/`analyze_results` contrast **configurations of the same harness**. If you
ran **two different harnesses** in one session (e.g. `inference-perf` for SLO validation +
`guidellm` for a throughput sweep against the same stack), contrast *those* with
**`compare_harness_runs`** — see `knowledge/multi_harness.md`.

## Reading the deltas (what to tell the user)

`compare_reports` marks each metric's direction:
- **Latency — lower is better:** TTFT (time to first token), TPOT (time per output token),
  ITL (inter-token latency), end-to-end request latency.
- **Throughput — higher is better:** output/total token rate, request rate.
- **Success rate — higher is better** (watch for runs that "win" on throughput only because
  many requests failed — always check success rate before declaring a winner).

The central tradeoff to explain: **raising concurrency/QPS usually increases throughput but
also latency** (queuing). There is rarely a single "best" run — there's the best run *for the
user's goal*. Tie the recommendation back to what they care about:
- "Chat / interactive" → prioritize low TTFT & TPOT (responsiveness), accept lower throughput.
- "Batch / offline / cost" → prioritize high token throughput, tolerate higher latency.
- Look for the knee of the curve: the highest load where latency is still acceptable.

Report only numbers from the comparison object (validated reports) — never invent or
extrapolate. If a treatment's report is missing or schema-invalid, `compare_reports` lists it
under `skipped`; say so plainly rather than comparing a partial set silently.
