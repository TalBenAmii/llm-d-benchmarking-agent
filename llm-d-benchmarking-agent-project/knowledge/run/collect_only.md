# Collect-only / skip-execution mode (`-z`/`--skip`) — re-collect a run without re-running it

A normal `run` executes the load AND collects/analyzes the results into a Benchmark Report.
`-z`/`--skip` (upstream help: *"Skip execution and only collect data from existing results"*)
runs the **second half only**: no new traffic is generated and the cluster is not mutated — it
re-collects and re-analyzes the artifacts an earlier run already left in the same session
workspace (the tool anchors a `run` to the session `--workspace`, so a skip-run looks in that
same dir). Because it mutates nothing it is read-only and **auto-runs** (no approval prompt,
like `--dry-run`/`--list-endpoints`).

Set it by passing `flags={'skip': True}` on an `execute_llmdbenchmark(subcommand='run', …)` —
the tool emits the short `-z` after the subcommand. It is a **`run`-subcommand-only** flag:
upstream defines it on `run` alone and the command policy only permits it there — never set it
on `standup`/`plan`/`experiment`.

## WHEN to use it — your judgment

Reach for `skip: True` when the load already ran and you want the report **without paying for
another benchmark run**:

- **The load completed but collection/analysis failed or was interrupted** (e.g. a transient
  error after the harness finished, or you cancelled before the report was written).
- **You want to re-derive the report from the same raw data** — e.g. after fixing a collection
  setting. A second load is a *different* measurement, so it can't reproduce the first run's
  results; only collect-only re-reads the identical artifacts.
- **You only need the report from artifacts already in the workspace** and don't want to
  perturb the stack with new traffic.

Not for a first/fresh measurement (there is nothing to collect yet), and not a way to "make a
run cheaper" — if you actually want new load/numbers, run normally without `skip`.

## How it fits the workflow

1. A prior normal `run` loaded and left raw artifacts under the session workspace.
2. `execute_llmdbenchmark(subcommand='run', …, flags={'skip': True})` against the **same**
   workspace re-collects/re-analyzes them — no second load.
3. Then `locate_and_parse_report` / `analyze_results` over the freshly collected report,
   exactly as after a normal run.

Mechanism vs. judgment: emitting `-z` is mechanism (`build_argv`); the command policy permits
`-z`/`--skip` as a `read_only_trigger` under `run` (data). WHETHER a collect-only re-derivation
beats a fresh run is the judgment that lives **here**, never in Python.
