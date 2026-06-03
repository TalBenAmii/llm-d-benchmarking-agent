# Harness debug mode (`-d`/`--debug`, `sleep infinity`) — launch a pod to poke at, by hand

A normal `run` deploys the harness launcher and then **runs the benchmark load** (the harness
drives traffic at the served model). Debug mode (`-d`/`--debug`, upstream env `LLMDBENCH_DEBUG`)
deploys the **same harness pods but runs `sleep infinity` in them instead of the load** — so the
pods come up, stay up, and do nothing, and a human can `kubectl exec`/`oc exec` into one and run
commands by hand to figure out why the real run is failing or behaving strangely.

Set it from the agent by passing `flags={'debug': True}` on an
`execute_llmdbenchmark(subcommand='run', …)` **or** `subcommand='experiment'`. The tool emits a
bare short `-d` after the subcommand.

## Run/experiment ONLY — and NEVER teardown

`-d`/`--debug` is **only** valid on `run` and `experiment` upstream. Critically, the SAME short
`-d` means something completely different — and **destructive** — on `teardown`:

| subcommand        | `-d` means | effect |
|-------------------|------------|--------|
| `run`, `experiment` | `--debug` | start harness pods with `sleep infinity` (this feature) |
| `teardown`        | `--deep`   | **full-namespace wipe** — removes cluster-scoped roles, deletes everything |

So **never** set `debug: True` expecting a teardown to honor it as debug — it would request a
*deep teardown* instead. The agent only ever emits `-d` for `run`/`experiment` (the tool guards
on the subcommand), and the allowlist only permits it there; teardown deliberately has no
`-d`/`--debug` entry. If you want a deep teardown, that is a separate, explicit, destructive
choice — not something to back into via this flag.

## It still LAUNCHES a real pod — so it stays approval-gated

Debug mode is **not** read-only. It creates real harness pods on the cluster (they just sleep
instead of loading). So a `run … -d` (or `experiment … -d`) is **mutating** and **requires
approval**, exactly like a normal run — it is *not* a collect-only/auto-run flag like
`skip`/`-z`. Treat it as "I am about to deploy something to the cluster, on purpose, to debug
it," and let the approval gate do its job.

## WHEN to use it — your judgment

Reach for `debug: True` when a run is failing or misbehaving in a way you cannot diagnose from
logs/reports alone and you need a **live, stable pod to inspect by hand**:

- **The harness pod keeps crashing / exiting before you can look at it.** With `sleep infinity`
  the pod stays Running, so a human can exec in and inspect the filesystem, the mounted dataset,
  env vars, network reachability to the model endpoint, installed tooling, etc.
- **You suspect an environment/wiring problem** (the dataset volume didn't mount, the endpoint
  URL is wrong, a token/secret is missing, DNS to the service fails) and want to reproduce the
  harness's own view of the world interactively (e.g. `curl` the endpoint from inside the pod).
- **You want to hand the user a pod to experiment in** — try a one-off harness command, tweak a
  config, sanity-check a model response — before committing to a full real run.

Do **not** use it:

- **For a normal measurement** — debug mode produces **no benchmark results** (it never runs the
  load), so there is nothing for `locate_and_parse_report`/`analyze_results` to read. If you want
  numbers, run **without** `debug`.
- **As a way to "keep the cluster warm"** — it pins real pods; tear it down when done.
- **On `teardown`** — see the table above; it means `--deep` there.

## The hard boundary: you EXPLAIN the exec, you do NOT drive the shell

This is the most important rule of this feature. The whole point of debug mode is an **interactive
human session inside the pod**. That interactive in-pod shell is a **manual, user-driven step**.
**The agent never drives it.** Concretely:

- After the debug pod is up, **tell the user how to exec into it** — e.g.:
  ```
  kubectl exec -it -n <NS> $(kubectl get pod -n <NS> -l app=llmdbench-harness-launcher -o name) -- bash
  ```
  (or `oc exec …` on OpenShift). Give them the command and explain what to do once inside.
- **Do not** try to run `kubectl exec -it … -- bash` yourself, do not attempt to stream an
  interactive shell, and do not invent an `exec` tool. There is **no** `kubectl`/`oc` `exec`
  subcommand in the allowlist, on purpose — that boundary is structurally enforced, not just a
  convention. An interactive TTY session is a human activity; the agent's job ends at launching
  the pod and **explaining** how to get into it.
- You *may* still use the agent's existing **read-only** tools to help (e.g. `kubectl get pods` /
  logs / readiness probes already in the allowlist) to confirm the debug pod is Running and to
  point the user at the right pod name — but the interactive shell itself is theirs to drive.

## How it fits the workflow

1. A real `run` (or `experiment`) is failing/misbehaving and logs alone are not enough.
2. Re-issue it with `flags={'debug': True}` (approval-gated — the user approves the launch). The
   harness pods come up with `sleep infinity`; no load runs, no report is produced.
3. Confirm the pod is Running (read-only `kubectl get pods` / readiness check), then **hand the
   user the `exec` command** and explain what to inspect. They drive the interactive session.
4. When they are done debugging, tear the stack/pods down (a normal, separate teardown).

## Notes

- Pure mechanism vs. judgment: emitting `-d` on run/experiment is mechanism (`build_argv`); the
  allowlist permits `-d`/`--debug` as a plain boolean flag (no `read_only_trigger`) under
  `run`/`experiment` only (data). WHETHER a debug launch is the right move, and the no-drive
  in-pod boundary, are the judgment that lives **here**, never in Python.
- Debug mode changes only *what the harness pod runs* (`sleep infinity` vs. the load); it does not
  change the stack you stood up, and it does not collect or analyze anything.
