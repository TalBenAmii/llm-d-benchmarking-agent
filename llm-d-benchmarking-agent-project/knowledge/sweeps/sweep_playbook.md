# Sweeps & A/B comparison playbook (entry)

Use this when the user wants to compare configurations — "how does latency change as
concurrency grows?", "is config A faster than B?", "find the best batch size", "sweep QPS".
The mechanism is two tools; the *judgment* is split across this family of guides:

- **This file** — pick the sweep SHAPE + reach a ready-made experiment file.
- `read_knowledge('sweep_authoring')` — author a custom grid (`generate_doe_experiment`
  factors/levels, which knobs to pick, eliciting token characteristics).
- `read_knowledge('sweep_validity')` — **which override keys actually apply** (list-index traps,
  kustomize deploys) + the **post-run validity gate** (did the sweep really vary anything?).
- `read_knowledge('sweep_results')` — compare/analyze the reports and read the deltas.
- `read_knowledge('sweep_goalseek')` — converge on the best config meeting an SLO (iterative).

## End-to-end A/B of two STACK configs → the upstream `compare-llm-d-configurations` skill
For an explicit A/B that **deploys, benchmarks, and tears down each configuration** (including
against a **no-llm-d baseline** — a model server with no inference scheduler), read the
**compare-llm-d-configurations skill** with `fetch_key_docs(task='compare_skill')` (it ships
`resources/no-llm-d-baseline.md`). Our mechanism is the **full DoE** below (`subcommand="experiment"`
with `setup_factors`, one standup+teardown per treatment) plus `compare_reports` / `analyze_results`.
Adapt the skill to OUR tooling: deploy/teardown flow through the SessionPlan gate +
`execute_llmdbenchmark` / `run_shell` (never the skill's raw helm or `ask_followup_question`),
results come from validated **BR-v0.2** reports (never scraped), and teardown follows
`read_knowledge('teardown')`.

## Decide the shape first

Ask (or infer) **what single thing varies** and **whether it changes the deployment**:

- **Run-parameter sweep (PREFERRED, esp. on the kind/CPU-sim quickstart).** The thing you
  vary is a *workload/run* knob (max-concurrency, QPS, prompt/output tokens). The stack is
  deployed **once**, then benchmarked N times. Cheap and fast.
  → `execute_llmdbenchmark(subcommand="run", flags={experiments: "<treatments.yaml>"})`
  against an already-stood-up stack. One `standup`, N runs (the CLI runs them **sequentially**).
  → **Parallel / K8s-native alternative:** `orchestrate_sweep` runs the same treatments (from
  `generate_doe_experiment`) as retryable Kubernetes Jobs under a `max_parallel` cap, checkpointed
  and resumable. Prefer it when speed/scale matters; see `read_knowledge('orchestrator')`.
- **Full DoE sweep.** The thing you vary changes the *deployment itself* (replicas, tensor
  parallelism, prefill/decode split, model). Each treatment needs its own standup+teardown.
  → `execute_llmdbenchmark(subcommand="experiment", flags={experiments: "<doe.yaml>"})`.
  Heavy (re-deploys per treatment) — only when a run-parameter sweep can't express it. On a
  single local kind cluster, prefer the run-parameter sweep; full DoE shines on real/GPU.

Keep everything else **fixed** across the sweep — vary one factor at a time, or the deltas
aren't attributable. (Authoring the matrix: `read_knowledge('sweep_authoring')`.)

## Reuse-first: use-case → ready-made experiment file (all at repo-root `experiments/`)

Before authoring a custom grid, read the authoritative format + examples (read-only refs):
`read_repo_doc(path="llm-d-benchmark/docs/doe.md")` and
`read_repo_doc(path="llm-d-benchmark/llmdbenchmark/experiment/README.md")`. The ready-made
experiment YAMLs live at the repo-ROOT `experiments/` dir, NOT `workload/experiments/`. Reach
them via the `--experiments` flag; author a new one (`sweep_authoring`) only if none fits:
- **P/D prefill/decode ratio** → `experiments/pd-disaggregation.yaml`
- **Tiered KV cache (CPU/disk offload)** → `experiments/tiered-prefix-cache.yaml`
- **Optimized-baseline load ladder** → `experiments/optimized-baseline.yaml`
- **Agentic / RAG session-rate sweep** → `experiments/otel-session-rate-sweep.yaml`
  (inference-perf + `otel_traces.yaml`). ⚠️ **As shipped this file does NOT actually vary the
  rate** (a list-indexed override the renderer drops) — full explanation + the dict-keyed fix →
  `read_knowledge('sweep_validity')`.
- **Precise prefix-cache** → `experiments/precise-prefix-cache-aware.yaml` (NOTE: the experiment
  file is `precise-prefix-cache-aware`, while the deploy spec/scenario is
  `guides/precise-prefix-cache-routing` — the two names differ).

The experiment YAML's top-level `experiment:` block tunes the run: `name` (defaults to the
filename), `harness` (OVERRIDES the scenario harness — use `vllm-benchmark` when sweeping
max-concurrency so it doesn't inherit the scenario's inference-perf harness), and `profile`
(overrides the scenario's workload profile). Note: `max-concurrency` is a **vllm-benchmark**
field; the quickstart default harness is `inference-perf` — match the swept key to the chosen
harness/workload, or switch harness accordingly.

## Always preview, then run gated

`experiment`/`run` are mutating (they deploy). Add `flags={dry_run: true}` first to preview the
plan; the user approves the real sweep. Per-treatment reports are written under the session
workspace automatically (`experiment` → `workspace/experiment`, `run` → `workspace/results`).
**After the dry-run, check the validity gate** (`read_knowledge('sweep_validity')`) — a dropped
override key means a treatment silently won't vary what you think.
