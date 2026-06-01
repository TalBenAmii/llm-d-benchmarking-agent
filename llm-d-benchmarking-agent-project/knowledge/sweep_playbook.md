# Sweeps & A/B comparison playbook

Use this when the user wants to compare configurations — "how does latency change as
concurrency grows?", "is config A faster than B?", "find the best batch size", "sweep QPS".
The mechanism is two tools; the *judgment* (what to vary, how to read the deltas) is here.

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
generate the YAML with `write_and_validate_config(artifact_type="run_config", ...)` so it
lands in the session workspace (never in the repos), then pass that path as `flags.experiments`.
Note: `max-concurrency` is a **vllm-benchmark** profile field; the quickstart's default
harness is `inference-perf`. Match the swept key to the chosen harness/workload, or switch
harness accordingly.

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
