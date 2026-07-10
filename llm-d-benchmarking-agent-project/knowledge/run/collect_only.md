# Collect-only / skip-execution mode (`-z`/`--skip`) — re-collect a run without re-running it

A normal `run` does two things in one shot: it **executes the load** (the harness drives
traffic at the served model) and then **collects + analyzes** the results into a Benchmark
Report. The collect-only / skip-execution mode (`-z`/`--skip`, upstream help: *"Skip execution
and only collect data from existing results"*) runs the **second half only**: it skips the
harness/load execution and just collects and analyzes the data that an **earlier run already
produced** in the same workspace.

Set it from the agent by passing `flags={'skip': True}` on an
`execute_llmdbenchmark(subcommand='run', …)`. The tool emits the short `-z` after the
subcommand. It is a **`run`-subcommand-only** flag — upstream defines `-z`/`--skip` on `run`
alone (not `standup`/`plan`/`experiment`), so only ever set it on a `run`.

## WHAT it actually does

- **Skips the load.** No new traffic is generated; the served stack is not exercised. So it
  does **not** mutate the cluster — it is read-only and **auto-runs** (no approval prompt, like
  `--dry-run`/`--list-endpoints`); it never tears down or redeploys the stack.
- **Re-collects + re-analyzes the EXISTING results.** It reads the raw artifacts a prior `run`
  left in the workspace and re-derives the report from them. So it only makes sense **after a
  real run has already loaded** against the same workspace — there must be existing results to
  collect. The tool anchors a `run` to the session `--workspace`, so a skip-run looks in that
  same session dir where the prior run's artifacts live.

## WHEN to use it — your judgment

Reach for `skip: True` when the load already ran and you want the report (or a fresh report)
**without paying for another benchmark run**:

- **The load completed but collection/analysis failed or was interrupted** (e.g. a transient
  error after the harness finished, or you cancelled before the report was written). Re-collect
  instead of re-running — the expensive part (the load) is already done.
- **You want to re-derive the report from the same raw data** — e.g. after fixing a collection
  setting — and re-running would waste time and change the numbers (a second load is a *different*
  measurement, so it can't reproduce the first run's results; only collect-only re-reads the
  identical artifacts).
- **You only need the report from a run whose artifacts you already have** in the workspace and
  do not want to perturb the stack with new traffic.

Do **not** use it:

- **For a first/fresh measurement** — there is nothing to collect yet; run normally (no `skip`).
- **To "make a run cheaper"** in general — if you actually want new load/numbers, you must run.
- **On `standup`/`plan`/`experiment`** — they have no such flag; setting `skip` there would be a
  no-op (the tool only emits `-z` for the `run` you set it on, and the allowlist only permits it
  under `run`).

## How it fits the workflow

1. A normal `run` loads and collects → a Benchmark Report under the session workspace.
2. If only the **collection** went wrong (the load itself succeeded), call
   `execute_llmdbenchmark(subcommand='run', …, flags={'skip': True})` against the **same**
   workspace to re-collect/re-analyze the existing results — no second load.
3. Then `locate_and_parse_report` / `analyze_results` over the freshly collected report, exactly
   as after a normal run.

## Notes

- Pure mechanism vs. judgment: emitting `-z` is mechanism (`build_argv`); the allowlist permits
  `-z`/`--skip` as a `read_only_trigger` under `run` (data). WHETHER a collect-only re-derivation
  is the right move — vs. a fresh run — is the judgment that lives **here**, never in Python.
