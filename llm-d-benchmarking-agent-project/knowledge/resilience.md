# Resilience drill — reading the report, choosing the faults, framing the verdict

The `run_resilience_drill` tool proves two things about the orchestrator under adverse
conditions, using the **completely unmodified** Job lifecycle (classify → retry/dead-letter →
checkpoint/reconstruct):

1. it correctly **classifies and recovers** from injected faults, and
2. it **survives its own restart** mid-run and resumes from cluster/checkpoint state.

It is **opt-in and double-gated**: it refuses unless the backend `CHAOS_ENABLED` flag is set
**and** you call this named tool — chaos is never reachable from `orchestrate_benchmark_run`.
The drill runs hermetically against an **in-process cluster**: it never deliberately breaks a
real one. If the tool returns a "disabled" error, tell the user to set `CHAOS_ENABLED=true` in
the backend `.env` — do not try to work around it.

## Which faults to inject (your judgment)

A `chaos_plan` is `{seed, injections: [...]}`; each injection is
`{kind, at_attempt, point, probability}`. `kind` is one of the classifier's kinds, split by
how the **unchanged** retry rule (`controller.py` `DEFAULT_RETRYABLE`) treats it:

| Kind | Class | Correct recovery |
|---|---|---|
| `evicted` | **transient** | retry → a fresh, distinct Job (`-a2`), often succeeds |
| `unknown` | **transient** | retry → a fresh Job |
| `oom` | deterministic | **dead-letter** (no retry — retrying OOMs again) |
| `unschedulable` | deterministic | **dead-letter** (fix resources/spec, don't retry) |
| `image_error` | deterministic | **dead-letter** (fix the image/registry) |
| `timeout` | deterministic | **dead-letter** (shorten workload / raise deadline) |
| `run_error` | deterministic | **dead-letter** (read the logs first) |

(Same semantics as `read_knowledge('orchestrator')` — read it for what each fault *means* and
how to remediate it for the user.)

A good demonstration plan covers **both halves**: inject an `evicted` at `at_attempt: 1` (to
prove the retry path produces a fresh Job that then succeeds) **and** a deterministic fault
like `oom` (to prove it dead-letters by design rather than wastefully retrying). Use a fixed
`seed` for a reproducible drill. `at_attempt` targets that attempt's Job, so a transient fault
on attempt 1 lets attempt 2 succeed under `max_attempts > 1`.

## Reading the report

The card carries facts; **you** narrate the verdict. Per injected fault it reports:

- **`classified_correctly`** — did the unmodified `classify_failure` map the injected fault to
  the same kind? (It should always be true — that is the classifier's contract.)
- **`recovery_action`** — `retry` (a fresh Job was submitted), `dead-letter` (no retry), or
  `completed` (a transient fault retried and then succeeded).
- **`recovered_as_designed`** — did the action match the rule above? A transient fault should
  retry (or, if the budget was spent, dead-letter); a deterministic fault should dead-letter.

The **restart drill** reports that a *fresh* orchestrator (holding no local state) resumed a
partial sweep from the cluster ConfigMap checkpoint with **0 duplicate Jobs** — the honest
"discard the object, rehydrate from the cluster" model. This reuses the real checkpoint/resume
+ `reconstruct` machinery (the same code exercised by the sweep tests), so it is a genuine
proof, not theatre. State that plainly: durability = the cluster is the source of truth, so a
restarted orchestrator reconstructs in-flight runs from labels and skips already-completed
treatments from the checkpoint.

`slo.met` = the drill completed within `slo_budget_s` (wall-clock, consistent with
`activeDeadlineSeconds`).

## Framing the verdict for a non-expert

Lead with the headline: "N/N faults classified correctly; N/N recovered as designed; restart
survived (no duplicate runs); SLO met." Then, if any `recovered_as_designed` is false, explain
the gap and what to change. If everything passed, say so confidently and explain *why* it is
resilient: transient faults self-heal via a retry, deterministic faults fail fast instead of
burning the budget, and a crash mid-run loses no work because the cluster (Job labels +
checkpoint ConfigMap), not the process, holds the truth. This is a demonstration of the
existing lifecycle's correctness — not a new behavior layered on top.
