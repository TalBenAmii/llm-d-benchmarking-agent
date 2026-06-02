# Kubernetes-native orchestration (orchestrate_benchmark_run)

Two ways to run a benchmark:

- **`execute_llmdbenchmark`** — runs the `llmdbenchmark` CLI locally as a blocking
  subprocess (it, in turn, creates the harness pod in the cluster). Simplest path; best for
  the quickstart and for a single interactive run you watch live.
- **`orchestrate_benchmark_run`** — runs the benchmark as a **Kubernetes Job** the
  orchestrator owns: submit → watch to completion → on failure, classify the cause; with
  `max_attempts>1` a *transient* failure retries as a fresh, distinct Job. Use this when you
  want a run that is **restart-resilient, individually retryable, and observable as a cluster
  object** — e.g. long runs, parallel/unattended runs, or a sweep. Needs the orchestrator
  container image (`ORCHESTRATOR_IMAGE` in the backend `.env`, or pass `image`); if it isn't
  set yet, fall back to `execute_llmdbenchmark`.

## Reading a failure (what the fault `kind` means, and what to do)

The tool returns a structured `failure` with a `kind`. Translate it for the user and suggest
the fix — these are operational facts; the remediation judgment is yours:

- **`oom`** — a container was OOMKilled. The model server or harness needed more memory than
  its limit. Suggest a lighter workload/profile or a smaller model, or (if the user controls
  the spec) more memory. Do NOT just retry — it will OOM again.
- **`unschedulable`** — the pod couldn't be placed (usually insufficient CPU/memory on the
  node; the message says which). Suggest lowering CPU/memory requests or choosing a smaller
  spec (e.g. the kind/sim path). Retrying unchanged won't help.
- **`timeout`** — the Job exceeded its `activeDeadlineSeconds`. Either the workload is too
  large or the deadline too tight; suggest a shorter workload or a larger deadline.
- **`evicted`** — the node was under resource pressure. This is usually **transient** —
  retrying (a fresh Job) often succeeds; `max_attempts>1` does this automatically.
- **`image_error`** — the image couldn't be pulled or the container failed to start. Check
  the image name / registry access.
- **`run_error`** — the benchmark container ran and exited non-zero. Read the logs (the tool
  streams them) for the real error before deciding.

## Live log streaming (real-time, during the run)

While an `orchestrate_benchmark_run` watches a Job, the benchmark pod's logs are followed in
the background and each line is surfaced to the user **as it is produced** — through the same
streamed-output channel the UI already renders for any other running command (not just dumped
at the end). So you can narrate progress live: "the harness is warming up", "it's on load
point 2 of 3", etc., straight from the pod's own output.

What this means for you:

- You don't have to call anything extra — streaming is automatic for a watched run. Just keep
  the user informed using what scrolls by, and reserve `summarize_report` for the *parsed*
  Benchmark Report (never scrape numbers from these raw log lines — they're for visibility,
  not for results).
- For a **sweep**, several treatments run at once, so each streamed line is prefixed with its
  treatment run-id (`[t1] …`, `[t2] …`) — tell the user which treatment a line belongs to.
- Log streaming is **best-effort**: if a pod isn't ready yet, logs rotate, or the follow
  stream hiccups, the live tail quietly stops **without affecting the run**. A run can still
  succeed even if no lines streamed. If the user saw nothing stream, that's not a failure —
  read the final outcome (and, for a `run_error`, the captured logs) instead.

## Sweeps & retries

- For a parameter sweep, prefer the CLI's native DoE (`execute_llmdbenchmark`
  `subcommand="experiment"`) when running locally. The orchestrator's parallel Job path
  (used internally for K8s-native sweeps) caps concurrency and dead-letters a treatment that
  keeps failing — one bad treatment doesn't sink the rest.
- Retries are only worth it for transient faults (`evicted`). Deterministic faults (`oom`,
  `unschedulable`, `image_error`, `timeout`) never auto-retry — fix the cause instead.
