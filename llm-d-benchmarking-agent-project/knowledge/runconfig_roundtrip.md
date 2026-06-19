# Run-config round-trip (`--generate-config` / `-c`)

The `llmdbenchmark` CLI can SAVE the effective settings of a run into a reusable run-config
YAML and REPLAY that file later. This is a SECOND way to make a run repeatable, alongside the
agent's own in-workspace config authoring (`write_and_validate_config`). Choosing between the
two — and choosing to generate-then-reuse at all — is judgment, not a default.

## The two mechanisms (so you pick the right one)

1. **Generate then replay (the CLI's own round-trip — this guide).**
   - `execute_llmdbenchmark(subcommand="run", …, flags={"generate_config": True})` emits
     `--generate-config`. The CLI takes the CURRENT settings (spec, namespace, harness `-l`,
     workload `-w`, model `-m`, monitoring, overrides, …) and writes a **run-config YAML from
     those settings under the session `--workspace`, then EXITS**. It deploys nothing and runs
     no load, so it auto-runs (read-only) — no approval prompt. Nothing is benchmarked yet.
   - `execute_llmdbenchmark(subcommand="run", flags={"run_config": "<path-to-that-yaml>"})`
     emits `-c <path>`, which **REPLAYS** the saved config in run-only mode. This *does* execute
     the benchmark load against an existing stack, so it is **mutating and approval-gated** like
     any normal run. You typically pass just `flags.run_config` (and `-p <ns>`); the harness/
     workload/model come from the saved file.
   - Upstream both flags are **`run`-ONLY** (not standup/plan/smoketest/teardown/experiment).
     The README round-trip is:
     ```
     llmdbenchmark --spec guides/optimized-baseline run -p NS -l inference-perf \
       -w sanity_random.yaml --generate-config        # writes run-config.yaml, exits
     llmdbenchmark run -c /path/to/run-config.yaml      # replays it
     ```

2. **Author a config in the workspace (`write_and_validate_config`).** You construct a
   scenario/experiment file yourself (e.g. with dotted `vllmCommon.*` / `kustomize.*` knobs) and
   gate it through `plan`/`--dry-run`. This is for config you are *designing* — knobs the current
   run settings don't already capture.

## When to use which (your judgment)

- **Generate-then-reuse (`--generate-config` → `-c`)** when you have a run whose settings are
  already correct and you want to **capture and replay them verbatim**: re-run the exact same
  benchmark later, hand a reproducible config to the user, or fire several identical runs
  (e.g. against a freshly re-stood-up stack) without re-specifying every flag. It is also the
  cleanest way to record "the run we actually did" for provenance.
- **Author in-workspace (`write_and_validate_config`)** when you are **changing** the
  configuration — setting finer vLLM/scheduling knobs, a kustomize deploy block, or an
  experiment matrix — i.e. when the config is something you're *designing*, not something an
  existing run already produced.
- **Neither (just run normally)** for a one-off run you won't repeat — don't generate a config
  you'll never replay.

When unsure, prefer generating a config after a good run (cheap, read-only) so the user has a
reproducible artifact; replay it only when they actually want the same run again.

## Mechanism notes (for grounding, not decisions)

- The generated YAML lands **under the session `--workspace`** because a non-preview `run` is
  anchored to `ctx.workspace`; a later `-c` reads that **workspace-relative path** back from the
  same session dir. Pass the path you got from the generate step (or one the user supplied that
  lives in the workspace). The agent never writes into the read-only sibling repos.
- `flags.run_config` is value-pinned by the allowlist to a `*.yaml`/`*.yml` path with **no `..`
  traversal** (`value_constraints.run_config_path`); `--generate-config` is allowlisted as a
  read-only trigger (generates-and-exits) on `run` only.
- **No env var** is involved — the CLI consumes `--generate-config` / `-c` directly. Don't set
  any `LLMDBENCH_*` config var yourself.
- A `-c` replay is **run-only mode**: it expects a stack already serving the model. If no stack
  is up, stand one up first (or use `-U <endpoint>` / a normal run). Treat a replay against a
  missing endpoint as an access/precondition problem, not a config failure.

## After a generate

Tell the user where the run-config was written (under the session workspace) and that you can
replay it any time with `flags.run_config`. After a replay, summarize the results exactly as you
would for a normal run — the load really executed; the config just told the CLI what to run.
