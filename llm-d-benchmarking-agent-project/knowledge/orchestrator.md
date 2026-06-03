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
  the user informed using what scrolls by, and report numbers only from the *parsed* Benchmark
  Report (`locate_and_parse_report`) — never scrape numbers from these raw log lines; they're
  for visibility, not results.
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

The orchestrator's K8s-native sweep path is **checkpoint-resilient**: consistent with the
stateless design (proposal §3.3/§4 — reconstruct from the cluster, store nothing locally), a
sweep's progress is persisted to a per-sweep Kubernetes ConfigMap (`llmd-bench-sweep-<sweep_id>`),
not a local file. That ConfigMap is the single source of truth — it records which treatments are
**completed** (with their outcome) and which are **in-flight** — so a resumed sweep skips the
already-completed treatments (idempotent; no duplicate work) and a fresh orchestrator process
can reconstruct exactly what is done from the cluster. Nothing is lost on a restart.

Judgment for the agent: this resilience is built into the orchestrated sweep machinery — you
don't drive it with a tool call (there is no agent-facing `sweep_id` argument or resume tool).
Agent-run sweeps go through the CLI's native DoE (`execute_llmdbenchmark subcommand="experiment"`,
see "Sweeps & retries" above). If a long sweep is interrupted, reassure the user the cluster
holds the source of truth rather than telling them all prior work is lost.
