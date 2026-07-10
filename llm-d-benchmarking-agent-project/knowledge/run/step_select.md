# Step selection / re-run (`-s` / `--step`)

This is the JUDGMENT half of Phase 31. The mechanism (emitting `-s <spec>`, value-pinning the
spec in the allowlist) is in `app/tools/execute.py` + `security/allowlist.yaml`. WHICH step to
re-run — and the per-phase step numbering — is your call, grounded here. Set
`ExecuteInput.flags['step']` (NOT `extra`).

## What it does

Each `llmdbenchmark` phase (standup / smoketest / run / teardown) runs as an ordered list of
numbered steps. `-s` (long: `--step`) RESTRICTS that phase to only the steps you name. So
instead of tearing the whole stack down and redoing a 10-minute standup because it tripped on
step 5, you can re-run JUST from step 5 onward.

Set it as a flag on `execute_llmdbenchmark`:

```
execute_llmdbenchmark(subcommand="standup", spec="cicd/kind", flags={"step": "5-9"})
```

This emits `llmdbenchmark --spec cicd/kind standup -s 5-9`.

## Step-list grammar

The value is a STRING in the upstream step-list grammar (parsed by the CLI's
`StepExecutor.parse_step_list`):

| form        | meaning                                  | example   |
|-------------|------------------------------------------|-----------|
| `N`         | just step N                              | `5`       |
| `N-M`       | the inclusive range N..M                 | `5-9`     |
| `N,M`       | a comma list of individual steps         | `3,7`     |
| combos      | mix ranges and individual steps          | `3-5,9`   |

No spaces. Only digits, commas and hyphens — that's all the allowlist permits, so anything
else is refused before it runs. Out-of-range or reversed (`9-5`) specs are tolerated by the CLI
(they just select nothing / an empty range), but DON'T rely on that — name real steps.

## Which subcommands accept it

`-s`/`--step` is valid ONLY on **standup, smoketest, run, teardown** (verified against the
upstream interface — each of those `interface/<name>.py` declares `-s`/`--step`). It is NOT a
flag on **plan, experiment, results** — never set `step` there. The allowlist value-pins the
step spec only on the four accepting subcommands (so a malformed spec is refused) and adds no
step flagspec to plan/experiment/results, so the CLI would reject one as an unknown flag.

## Per-phase step numbering — READ IT AT RUNTIME, don't hardcode

Step numbers and what each step does are defined upstream and can drift. DO NOT memorize a fixed
table. To learn the steps of a phase, read the upstream registry at runtime:

- The step files live at `llm-d-benchmark/llmdbenchmark/<phase>/steps/step_NN_<name>.py`
  (phase dirs: `standup`, `smoketests`, `run`, `teardown`). The `NN` prefix IS the step number;
  the suffix names what it does (e.g. `run/steps/step_00_preflight.py`,
  `run/steps/step_04_verify_model.py`).
  - **smoketest gotcha:** the smoketest phase's steps dir is `smoketests/steps/` (PLURAL) even
    though the subcommand is `smoketest` (singular). Don't look for `smoketest/steps/` — it
    doesn't exist; the dir is `smoketests/steps/`.
- The ordered list per phase is `llmdbenchmark/<phase>/steps/__init__.py`.
- The interface README (`llmdbenchmark/interface/README.md`) tabulates the flags per subcommand.

So before you pick a step number, look at the relevant `<phase>/steps/` directory (e.g. via the
repo-reading tools / `read_knowledge` pointers) and map the FAILURE you saw to the step whose
name matches the failing action. The repos are READ-ONLY — never write into them.

## The core use case: recover from a mid-phase failure WITHOUT a full teardown

1. A standup/run failed partway — the CLI output / pod logs tell you it died at, or after,
   step K (the step files are named, so "failed creating the harness namespace" maps to the
   `harness_namespace` step's number).
2. Fix the underlying cause (missing secret, capacity, image pull, etc.).
3. Re-run from K onward: `flags={'step': 'K-<last>'}` (or just `flags={'step': 'K'}` to redo a
   single step). You skip the expensive earlier steps that already succeeded.

This is far cheaper than `teardown` + a fresh full `standup`. Use it when the earlier steps are
known-good and only a later step needs another attempt.

## Safety — `-s` does NOT change the command's mode

A `standup`/`run`/`teardown` is still a MUTATING command when scoped with `-s`; re-running a
mutating step is still **approval-gated**. `-s` only narrows WHICH steps run, never WHETHER
approval is required. Prefer steps the phase is designed to repeat (upstream steps are written
to be re-runnable); if in doubt about a destructive step, fall back to a clean `teardown` + full
`standup`.

## When NOT to use it

- For a clean first deploy, omit `step` entirely — run the WHOLE phase.
- Don't use it to "skip" steps you think are unnecessary on a healthy first run; the phase is
  ordered for a reason. Use it for RE-RUNS after a specific, understood failure.
- Don't pass it to plan/experiment/results (it isn't a flag there).
