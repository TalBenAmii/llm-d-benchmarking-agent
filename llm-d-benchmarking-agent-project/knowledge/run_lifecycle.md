# Run lifecycle — when (and when NOT) to cancel a run

This is the JUDGMENT layer for the run-lifecycle mechanism (the `cancel_run` tool, the `cancel`
control message, and graceful shutdown). The Python only knows *how* to cancel a run; *whether*
to cancel one is your call, guided by this file. Cancelling a run frees the concurrency slot it
holds and cleans up its subprocess — it is safe, but it THROWS AWAY in-flight work, so use it
deliberately.

## What cancelling does (mechanism — so you can explain it)
- It stops the in-flight turn for a given chat/session. If that turn was holding a slot of the
  cross-session concurrency cap (it holds one only while a *mutating* command is executing), the
  slot is released immediately, so a queued or new run can start.
- The running subprocess (e.g. a `standup`/`run`) and its whole process group are reaped, so no
  orphaned process or leaked Kubernetes Job is left behind.
- It is **idempotent**: cancelling a session that has no live run does nothing and reports so.
- A run is always cancelled from OUTSIDE itself — you cannot cancel the very turn you are in
  (the user can cancel the current chat with the Stop control; an agent cancels *another* chat's
  run by its `session_id`).

## `cancel_run` vs `manage_orchestrated_runs` — stop the watch vs stop the cluster Job
`cancel_run` stops the **in-process turn** (the agent's watch + its local subprocess). For a
benchmark you started with `orchestrate_benchmark_run` / `orchestrate_sweep`, that turn is only
*watching* a Kubernetes **Job** — so cancelling the watch frees the slot but the **Job keeps
running on the cluster**. To actually stop cluster work, use
`manage_orchestrated_runs(action='stop', namespace=…, session_id=…/sweep_id=…)`, which deletes
the still-running Job(s). The same tool also lists run state (`action='list'`, read-only — the
cluster is the source of truth) and reaps finished Jobs (`action='cleanup'`, terminal only).
Deleting a Job never touches the results PVC, so artifacts are preserved. Rule of thumb:
**local CLI run → `cancel_run`; orchestrated K8s Job → `cancel_run` to free the slot AND
`manage_orchestrated_runs(action='stop')` to stop the Job.** (A programmatic, non-chat client can
read the same run state read-only over `GET /api/jobs?namespace=…`.)

## When TO cancel
- **An abandoned run is pinning a slot the user now needs.** The classic case: someone started a
  long benchmark, navigated away, and now no new run can start because the concurrency cap is
  full. If the abandoned run is no longer wanted, cancel it to free the slot.
- **The user explicitly changed their mind** about a run that is still going ("actually, stop
  that", "cancel the standup").
- **A run is clearly stuck/runaway** — e.g. `observe_run_metrics` shows it wedged with no
  progress and the user wants to reclaim the cluster, or it is past any sane wall-clock for the
  workload. Prefer the run's own per-command timeout when one will fire soon; cancel when waiting
  for the timeout would needlessly block the user.

## When NOT to cancel
- **Do not cancel a healthy run just to start another** if a slot will free up shortly on its
  own — explain the wait instead. Cancelling discards real work (and a partially-applied standup
  may leave cluster state to clean up).
- **Do not cancel to "fix" a transient error** the run can recover from; that is the
  orchestrator's retry/dead-letter job (see `orchestrator.md`), not a cancel.
- **Never cancel a run in a DIFFERENT user's chat** without being asked — the `session_id` you
  target should be one the user is clearly referring to.

## Don't leave a cluster running after a partial flow
A created cluster / stood-up stack costs the user money and capacity. The full rule — a
fully-specified flow that ENDS in teardown isn't done until teardown has actually run; skip optional
mid-flow gates (e.g. an "install metrics-server?" offer) SILENTLY on such a request; if you truly
can't finish (a step failed or a real decision is needed), say exactly where you stopped and EITHER
tear down the partial deployment OR hand the still-running cluster back with how to remove it
(`run_shell("kind delete cluster --name <name>")` / an approval-gated `teardown`), never abandoning
a created cluster with no word to the user — is CORE `quickstart_playbook.md` ("Complete a
fully-specified run+teardown…"). Lifecycle-specific: also don't treat "Before I kick off the
benchmark:" as a stopping point when nothing actually blocks you — keep going to the run and teardown.

## After cancelling
- Tell the user plainly what was stopped and that its slot is now free.
- If the cancelled run had begun a `standup`, the cluster may hold a partial deployment — offer a
  `teardown` to clean it up before proposing anything new.
- Readiness (`/readyz`) and liveness (`/healthz`) are separate: liveness only says the process is
  up; readiness reports per-component health (provider configured, repos present, runner ok,
  workspace writable). A cancel does not change readiness.
