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

## Hardware & placement (the `scheduling` argument)

`orchestrate_benchmark_run` takes an optional `scheduling` object to request the right
hardware (a GPU type / count) and to place the Job so it does **not starve the llm-d stack
being measured** (proposal §4). Omit it and the Job is the generic cpu/memory baseline. The
full judgment — which GPU type, when to request a GPU at all, how to keep the load generator
off the served nodes (`avoid_labels` → pod anti-affinity), node pools, taints, quotas — lives
in [`knowledge/resource_management.md`](resource_management.md). Read it before setting
`scheduling`, and verify the real node/pod label values on the target cluster first.

## Endpoint readiness gate (don't benchmark an unready stack)

Before a benchmark Job is submitted, the inference endpoint must actually be **serving** — not
just "a pod exists". A pod can be `Running` yet failing its readiness probe, in which case it
is **not** in any Service's ready backing endpoints and a benchmark against it would fail or
mislead. So the gate goes BEYOND pod presence:

- **`check_endpoint_readiness`** (read-only, auto-runs) is the explicit check. It reads the
  authoritative Kubernetes signal — `kubectl get endpoints` — and asks: does any Service in the
  namespace have a **ready backing endpoint** (a live address that can receive traffic)? It
  corroborates with the benchmark CLI's own read-only `run --list-endpoints`. It returns a
  structured `ready` verdict plus per-service ready/not-ready endpoint counts.
- **`orchestrate_benchmark_run` gates on this automatically** (`require_ready_endpoint=true` by
  default). If the endpoint isn't ready it submits **nothing**, mutates nothing, and returns a
  structured not-ready result: `{submitted:false, ready:false, readiness:{…}, standup_suggestion:{…}}`.

When you get a not-ready result:

- **Tell the user plainly** what the readiness check found (e.g. "the `cicd/kind` namespace has
  no ready inference endpoint yet" or "the service exists but no pod is serving").
- **OFFER to stand up a stack** — the result's `standup_suggestion` names the approval-gated path
  (`execute_llmdbenchmark subcommand="standup"`). Standing up is **mutating**, so it requires the
  user's explicit approval. **The decision to stand up is yours and the user's, not the gate's** —
  the readiness check is only the mechanism; never stand up unprompted, and never auto-mutate.
- A ready result means the stack is serving — proceed with the benchmark as before.
- Only set `require_ready_endpoint=false` (or skip the check) when you KNOW the endpoint is
  reachable another way — e.g. you're benchmarking an **external** endpoint via an explicit
  `-U/--endpoint-url` rather than an in-cluster Service.

This is the proposal's §3.3 dependency management: the run depends on a healthy serving stack,
so we verify that dependency (readiness) and — only with approval — bring it up.

## Sweeps & retries

- For a parameter sweep, prefer the CLI's native DoE (`execute_llmdbenchmark`
  `subcommand="experiment"`) when running locally. The orchestrator's parallel Job path
  (used internally for K8s-native sweeps) caps concurrency and dead-letters a treatment that
  keeps failing — one bad treatment doesn't sink the rest.
- Retries are only worth it for transient faults (`evicted`). Deterministic faults (`oom`,
  `unschedulable`, `image_error`, `timeout`) never auto-retry — fix the cause instead.

## Checkpoint / resume for long DOE sweeps (the cluster is the source of truth)

A long DOE sweep (many treatments) can be interrupted part-way: the orchestrator restarts,
the chat session drops, or the host reboots. Consistent with the **stateless design**
(proposal §3.3/§4 — reconstruct from the cluster, store nothing locally), a sweep's progress
is persisted to a **Kubernetes ConfigMap named for the sweep** (`llmd-bench-sweep-<sweep_id>`),
labelled `managed-by` + the sweep label. That ConfigMap — NOT any local workspace file — is
the single source of truth: it records which treatments are **completed** (with their outcome)
and which are **in-flight**.

What this buys the user:

- **Resume, don't restart.** Re-run a sweep with the **same `sweep_id`** and it CONTINUES from
  where it stopped — the already-completed treatments are skipped (not re-run), and only the
  remaining `N-k` execute. The final result merges the prior outcomes with the newly-run ones,
  so it still covers all `N` treatments.
- **Idempotent.** Running the same sweep id twice never re-runs a completed treatment and never
  duplicates work — a completed treatment in the checkpoint is authoritative.
- **Stateless / recoverable.** Because the checkpoint lives in the cluster, a fresh orchestrator
  (new process, new session) can `reconstruct_sweep(sweep_id)` to see exactly what is done and
  what remains. Nothing is lost on a local restart.

Judgment for the agent: when a sweep is interrupted, DON'T tell the user to start over — re-run
it with the **same sweep id** to resume. To report progress mid-sweep, read the checkpoint
(completed vs in-flight) rather than guessing. A *fresh* sweep should use a *new* sweep id (a
new checkpoint); reuse an id only to deliberately resume that same sweep. The checkpoint
ConfigMap is small and is left in place after a run so a later resume still works.
