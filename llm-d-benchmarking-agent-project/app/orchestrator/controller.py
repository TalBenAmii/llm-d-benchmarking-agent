"""Benchmark Job orchestrator — submit a run as a K8s Job, watch it to a terminal state,
stream its logs.

Thin mechanism over a :class:`~app.orchestrator.kube.KubeClient`: it owns the Job lifecycle
but holds NO local source-of-truth — every status read comes from the cluster (so a restarted
orchestrator reconstructs from labels; see :meth:`reconstruct`). Monitoring is poll-based
(repeated ``kubectl get jobs -l run-id=<id> -o json``): simpler and more robust than a long
``--watch`` stream, and trivially testable against a fake. Fault classification (OOM /
eviction / unschedulable) and retry/dead-letter build on this in later sub-phases.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

from app.observability import metrics as instrument
from app.orchestrator.checkpoint import CheckpointStore, SweepCheckpoint
from app.orchestrator.faults import EVICTED, TIMEOUT, UNKNOWN, Failure, classify_failure
from app.orchestrator.job import (
    ABSENT,
    FAILED,
    LABEL_RUN,
    SUCCEEDED,
    JobSpec,
    JobStatus,
    build_job_manifest,
    classify_job_status,
    job_name,
)
from app.orchestrator.kube import KubeClient

OnStatus = Callable[[JobStatus], Awaitable[None]]
# Sink for a benchmark pod's live log lines (Phase 21). The tool wires this to the session's
# `output` event — the SAME transport the UI already renders for streamed command output — so
# pod logs surface in real time during a run. None disables streaming (e.g. unit tests / when
# the caller wants no live tail), keeping existing behavior unchanged.
OnLogLine = Callable[[str], Awaitable[None]]

# How long the watch loop waits for the in-flight log tail to wind down after the Job reaches a
# terminal phase, so any final buffered lines are flushed before we stop. Bounded so a tail that
# refuses to stop never delays the run's completion.
_TAIL_DRAIN_TIMEOUT = 2.0

# Faults worth retrying: transient/environmental, where a fresh attempt may succeed. OOM /
# unschedulable / image / timeout are deterministic — retrying changes nothing, so they
# dead-letter immediately (the agent must adjust resources/spec/workload instead).
DEFAULT_RETRYABLE = frozenset({EVICTED, UNKNOWN})

# Floor for the status-poll sleep so poll_interval=0 (a schema-allowed value used in tests)
# can't busy-loop and hammer the cluster; max_wait is bounded on wall-clock independently.
_MIN_POLL_INTERVAL = 0.05


def _safe_metric(fn, *args, **kwargs) -> None:
    """Record a metric without ever letting observability disrupt the Job lifecycle."""
    with contextlib.suppress(Exception):  # metrics must never disrupt the lifecycle
        fn(*args, **kwargs)


@dataclass
class AttemptResult:
    run_id: str              # the per-attempt Job's run-id (distinct, inspectable)
    status: JobStatus
    failure: Failure | None = None


@dataclass
class RunOutcome:
    run_id: str              # the logical run (base id shared across attempts)
    succeeded: bool
    attempts: list[AttemptResult] = field(default_factory=list)
    dead_lettered: bool = False     # exhausted retries or a non-retryable fault
    final_failure: Failure | None = None


@dataclass
class SweepOutcome:
    outcomes: list[RunOutcome] = field(default_factory=list)
    # Treatment run-ids that were SKIPPED on resume because the cluster checkpoint already
    # recorded them as completed (so they were not re-run). Empty for a fresh, non-checkpointed
    # sweep. Their outcomes are still merged into `outcomes`, so the result covers all N.
    resumed: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> list[str]:
        return [o.run_id for o in self.outcomes if o.succeeded]

    @property
    def dead_lettered(self) -> list[str]:
        return [o.run_id for o in self.outcomes if o.dead_lettered]

    @property
    def all_succeeded(self) -> bool:
        return bool(self.outcomes) and all(o.succeeded for o in self.outcomes)


def _outcome_from_checkpoint(checkpoint: SweepCheckpoint, run_id: str) -> RunOutcome:
    """Reconstruct a skipped-on-resume treatment's :class:`RunOutcome` from its checkpoint
    record, so a resumed sweep's merged result reflects the prior (completed) outcome without
    re-running it. The per-attempt Job objects remain in the cluster for diagnosis; the
    checkpoint carries only the terminal facts (succeeded / dead-lettered / fault kind)."""
    rec = checkpoint.treatments[run_id]
    failure = (
        Failure(rec.fault_kind, message="recovered from sweep checkpoint")
        if rec.dead_lettered and rec.fault_kind
        else None
    )
    return RunOutcome(
        run_id=run_id,
        succeeded=rec.succeeded,
        dead_lettered=rec.dead_lettered,
        final_failure=failure,
    )


class BenchmarkOrchestrator:
    def __init__(self, kube: KubeClient, workspace: str | Path):
        self._kube = kube
        # Manifests are written here; RealKubeClient confines `apply -f` to the workspace.
        self._workspace = Path(workspace)

    def _run_selector(self, run_id: str) -> str:
        return f"{LABEL_RUN}={run_id}"

    async def submit(self, spec: JobSpec) -> str:
        """Render + write the Job manifest, then `kubectl apply` it (approval-gated).
        Returns the Job name. The manifest is kept in the workspace as the run's record."""
        manifest = build_job_manifest(spec)
        path = self._workspace / "jobs" / f"{spec.run_id}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(manifest, sort_keys=False))
        await self._kube.apply(path, namespace=spec.namespace)
        _safe_metric(instrument.record_run_submitted)
        return job_name(spec.run_id)

    async def status(self, run_id: str, *, namespace: str) -> JobStatus:
        jobs = await self._kube.list_jobs(namespace=namespace, selector=self._run_selector(run_id))
        if not jobs:
            return JobStatus(name=job_name(run_id), phase=ABSENT)
        return classify_job_status(jobs[0])

    async def watch(
        self,
        run_id: str,
        *,
        namespace: str,
        poll_interval: float = 2.0,
        max_wait: float = 7200.0,
        on_status: OnStatus | None = None,
    ) -> JobStatus:
        """Poll until the Job reaches a terminal phase (succeeded/failed), vanishes after
        having existed (deleted out from under us), or ``max_wait`` elapses. Emits each
        observed status to ``on_status``. Never holds local state — the cluster is truth."""
        start = time.monotonic()
        seen = False
        last: JobStatus | None = None
        while True:
            st = await self.status(run_id, namespace=namespace)
            if st.phase != ABSENT:
                seen = True
            if on_status is not None and (last is None or st.phase != last.phase):
                await on_status(st)
            last = st
            if st.terminal:
                return st
            if st.phase == ABSENT and seen:
                return st  # the Job existed and is now gone — treat as terminal
            if time.monotonic() - start >= max_wait:
                return st  # client-side watch timeout (bounded on wall-clock, not on poll sum)
            # Floor the sleep so poll_interval=0 can't busy-loop and hammer the cluster.
            await asyncio.sleep(poll_interval if poll_interval > 0 else _MIN_POLL_INTERVAL)

    async def diagnose(self, run_id: str, *, namespace: str,
                       job_status: JobStatus | None = None) -> Failure:
        """Inspect a run's Job + pods and classify why it failed (OOM / timeout / eviction /
        unschedulable / image / run error). Facts only — the agent explains remediation."""
        if job_status is None:
            job_status = await self.status(run_id, namespace=namespace)
        pods = await self._kube.list_pods(namespace=namespace, selector=self._run_selector(run_id))
        return classify_failure(job_status, pods)

    async def stream_logs(self, run_id: str, *, namespace: str, tail: int | None = 500,
                          follow: bool = False) -> str:
        """Tail/stream the run's pod logs (selected by the run-id label). Streams to the UI
        via the runner's `output` event and returns the captured text."""
        return await self._kube.logs(
            namespace=namespace, selector=self._run_selector(run_id), tail=tail, follow=follow
        )

    async def _tail_logs(self, run_id: str, *, namespace: str, on_log_line: OnLogLine,
                         tail: int | None = None) -> None:
        """Background driver: follow a run's pod logs and forward each line to ``on_log_line``
        as it is produced (Phase 21 real-time streaming). This is a BEST-EFFORT side channel —
        it must NEVER break the run/watch loop:

        * It is launched as its own task and cancelled when the Job reaches a terminal state;
          ``CancelledError`` is allowed to propagate so the task stops promptly.
        * EVERY other failure (pod not ready yet, log rotation, the follow stream erroring out,
          a raised ``on_log_line``) is swallowed — a failing tail leaves the run untouched.

        The lines ride the existing allowlisted, read-only ``kubectl logs -f`` path (argv-only,
        ``shell=False``); ``on_log_line`` is the same ``output`` event the UI already renders."""
        try:
            stream = self._kube.stream_log_lines(
                namespace=namespace, selector=self._run_selector(run_id), tail=tail,
            )
            async for line in stream:
                try:
                    await on_log_line(line)
                except asyncio.CancelledError:
                    raise
                except Exception:  # a failing sink must not abort the tail or the run
                    continue
        except asyncio.CancelledError:
            raise  # terminal-state cancellation — stop the tail, never the run
        except Exception:
            # pod-not-ready / rotation / stream error: log streaming is best-effort. Swallow so
            # the run/watch loop is wholly unaffected (acceptance: a failing tail never fails it).
            return

    @contextlib.asynccontextmanager
    async def _log_tail(self, run_id: str, *, namespace: str, on_log_line: OnLogLine | None,
                        tail: int | None = None):
        """Run a live log tail for the duration of the enclosed block (one Job attempt's
        watch). On exit — the Job reached a terminal state, or the watch raised/was cancelled —
        the tail is cancelled and reaped, bounded by ``_TAIL_DRAIN_TIMEOUT`` so a stuck tail
        never delays the run. A no-op when ``on_log_line`` is None (streaming disabled)."""
        if on_log_line is None:
            yield
            return
        task = asyncio.create_task(
            self._tail_logs(run_id, namespace=namespace, on_log_line=on_log_line, tail=tail)
        )
        try:
            yield
        finally:
            # The Job is terminal, so a real `kubectl logs -f` exits on its own and the tail
            # finishes naturally — give it a brief, bounded window to flush any lines still in
            # flight (so the final benchmark output isn't dropped). If it does NOT settle in
            # time (a wedged follow stream), cancel + reap it so it can never delay the run.
            if not task.done():
                with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception):
                    await asyncio.wait_for(asyncio.shield(task), timeout=_TAIL_DRAIN_TIMEOUT)
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception):
                await asyncio.wait_for(asyncio.shield(task), timeout=_TAIL_DRAIN_TIMEOUT)

    async def reconstruct(self, *, namespace: str, session_id: str | None = None,
                          sweep_id: str | None = None) -> list[JobStatus]:
        """Rebuild run state purely from the cluster: list the agent-managed Jobs (optionally
        scoped to a session or sweep) and classify each. This is how a restarted orchestrator
        recovers in-flight runs — it stores nothing locally."""
        from app.orchestrator.job import LABEL_MANAGED, LABEL_SESSION, LABEL_SWEEP, MANAGED_BY

        parts = [f"{LABEL_MANAGED}={MANAGED_BY}"]
        if session_id:
            parts.append(f"{LABEL_SESSION}={session_id}")
        if sweep_id:
            parts.append(f"{LABEL_SWEEP}={sweep_id}")
        jobs = await self._kube.list_jobs(namespace=namespace, selector=",".join(parts))
        return [classify_job_status(j) for j in jobs]

    async def reconstruct_sweep(self, sweep_id: str, *, namespace: str) -> SweepCheckpoint:
        """Rebuild a DOE sweep's progress purely from the cluster checkpoint (Phase 22). Reads
        the sweep's ConfigMap (the source of truth) and returns the parsed
        :class:`~app.orchestrator.checkpoint.SweepCheckpoint` — which treatments are completed
        (with outcome) and which are in-flight. A restarted orchestrator calls this to decide
        what still needs running; an absent ConfigMap yields an empty checkpoint (nothing done
        yet). Stores nothing locally."""
        store = CheckpointStore(self._kube, self._workspace)
        return await store.load(sweep_id, namespace=namespace)

    async def run_with_retries(
        self,
        base_spec: JobSpec,
        *,
        max_attempts: int = 3,
        retryable=DEFAULT_RETRYABLE,
        poll_interval: float = 2.0,
        max_wait: float = 7200.0,
        on_log_line: OnLogLine | None = None,
    ) -> RunOutcome:
        """Submit a benchmark run, watching it to terminal; on a *transient* failure
        (``retryable``) resubmit as a FRESH Job (a new attempt, distinct run-id) up to
        ``max_attempts``. A deterministic fault (OOM/unschedulable/image/timeout) or an
        exhausted budget dead-letters the run. Every attempt is its own inspectable Job
        (``<run_id>-a<N>``), so failed attempts remain in the cluster for diagnosis.

        ``on_log_line`` (Phase 21): while each attempt's Job runs, its pod logs are followed in
        a background task and each line is forwarded here as it is produced — surfacing
        benchmark output live, not just at the end. The tail is cancelled when the attempt
        reaches a terminal state, and a failing tail never affects the run (see ``_tail_logs``)."""
        attempts: list[AttemptResult] = []
        outcome: RunOutcome | None = None
        # A run is "in flight" while the orchestrator is watching its attempts (a live gauge
        # the dashboard can show during a benchmark); always decremented in finally.
        _safe_metric(instrument.runs_in_flight.inc)
        try:
            for i in range(1, max_attempts + 1):
                spec_i = replace(base_spec, run_id=f"{base_spec.run_id}-a{i}", attempt=i)
                await self.submit(spec_i)
                # Follow this attempt's pod logs live for the span of the watch; the tail is
                # cancelled (and reaped) when the watch returns a terminal status.
                async with self._log_tail(spec_i.run_id, namespace=spec_i.namespace,
                                          on_log_line=on_log_line):
                    st = await self.watch(spec_i.run_id, namespace=spec_i.namespace,
                                          poll_interval=poll_interval, max_wait=max_wait)
                _safe_metric(instrument.record_attempt, st.phase)
                if st.phase == SUCCEEDED:
                    attempts.append(AttemptResult(spec_i.run_id, st, None))
                    outcome = RunOutcome(base_spec.run_id, True, attempts)
                    break
                # Only a genuinely FAILED Job warrants pod-level diagnosis. A watch that returned
                # ABSENT (Job vanished) or non-terminal (our max_wait elapsed while it ran) is its
                # own outcome, not a pod fault.
                if st.phase == FAILED:
                    failure = await self.diagnose(spec_i.run_id, namespace=spec_i.namespace, job_status=st)
                elif st.phase == ABSENT:
                    failure = Failure(UNKNOWN, message="job vanished from the cluster before completing")
                else:  # active/pending after max_wait — client-side watch timeout (Job may still run)
                    failure = Failure(TIMEOUT, message="watch timed out while the job was still running")
                attempts.append(AttemptResult(spec_i.run_id, st, failure))
                if i < max_attempts and failure.kind in retryable:
                    continue  # transient — try again as a fresh Job
                outcome = RunOutcome(base_spec.run_id, False, attempts, dead_lettered=True,
                                     final_failure=failure)
                break
            if outcome is None:
                outcome = RunOutcome(base_spec.run_id, False, attempts, dead_lettered=True,
                                     final_failure=attempts[-1].failure if attempts else None)
        finally:
            _safe_metric(instrument.runs_in_flight.dec)
        _safe_metric(
            instrument.record_run_outcome,
            succeeded=outcome.succeeded,
            dead_lettered=outcome.dead_lettered,
            fault_kind=outcome.final_failure.kind if outcome.final_failure else None,
        )
        return outcome

    async def run_sweep(
        self,
        specs: list[JobSpec],
        *,
        max_parallel: int = 2,
        max_attempts: int = 2,
        retryable=DEFAULT_RETRYABLE,
        poll_interval: float = 2.0,
        max_wait: float = 7200.0,
        on_log_line: OnLogLine | None = None,
        sweep_id: str | None = None,
        namespace: str | None = None,
    ) -> SweepOutcome:
        """Run a list of treatment specs as parallel Jobs under a concurrency cap, each with
        its own retry/dead-letter budget. A persistently-failing treatment dead-letters
        without sinking the rest of the sweep (the proposal's DoE parallel scheduling +
        dead-letter). Returns a per-treatment outcome roll-up.

        ``on_log_line`` (Phase 21): each treatment streams its pod logs live; because the
        sweep runs treatments in parallel, each line is prefixed with the treatment's run-id
        (``[<run_id>] <line>``) so interleaved lines stay attributable in the shared event
        stream. A failing tail on one treatment never affects the rest of the sweep.

        ``sweep_id`` (Phase 22): CHECKPOINT/RESUME. When given, the sweep's progress is the
        cluster ConfigMap named for the sweep — the source of truth, not a local file. Before
        running, the checkpoint is loaded and every treatment already recorded as COMPLETED is
        SKIPPED (its prior outcome is merged into the result); only the remainder N-k execute.
        Each treatment is marked in-flight before it runs and completed after, so a re-invoke
        with the SAME ``sweep_id`` continues from where it stopped — completed treatments are
        never re-run (idempotent resume). ``namespace`` is required alongside ``sweep_id`` (the
        ConfigMap lives in that namespace). Omit ``sweep_id`` for the original stateless behavior
        (no checkpoint)."""
        sem = asyncio.Semaphore(max(1, max_parallel))

        # Phase 22 checkpoint wiring. The ConfigMap is the source of truth; the store is a thin
        # read/serialize/write over the same allowlisted kubectl surface as the Job lifecycle.
        store: CheckpointStore | None = None
        checkpoint = SweepCheckpoint(sweep_id=sweep_id or "")
        if sweep_id is not None:
            if namespace is None:
                raise ValueError("run_sweep: namespace is required when sweep_id is given "
                                 "(the checkpoint ConfigMap lives in the cluster namespace)")
            store = CheckpointStore(self._kube, self._workspace)
            checkpoint = await store.load(sweep_id, namespace=namespace)

        # Serialize checkpoint mutations + writes across the parallel treatments so concurrent
        # completions never race on the in-memory document or the apply.
        ck_lock = asyncio.Lock()

        async def _safe_checkpoint_write() -> None:
            """Persist the checkpoint, BEST-EFFORT. The ConfigMap write is a mutating
            ``kubectl apply`` (approval- and quota-gated) and so CAN raise — quota exhausted
            mid-sweep, approval declined, or a transient apply error. It runs OUTSIDE the
            per-treatment try/except in ``_one`` (in ``_persist_in_flight`` before it and
            ``_persist_completed`` after it), so an uncaught error here would propagate through
            ``asyncio.gather`` and SINK THE WHOLE SWEEP — destroying every other treatment's
            result — which directly violates the sweep's per-treatment isolation invariant.
            Since the cluster is the source of truth and a missed/stale checkpoint write only
            degrades to "re-run this treatment on resume" (never data loss — the in-memory
            mutation still happened, so the next successful write and the live result are
            correct), a write failure must NEVER abort a run. Swallow it like the other
            best-effort side channels (``_safe_metric`` / ``_tail_logs``)."""
            if store is None:  # never None at the call sites (both early-return) — narrows for mypy
                return
            with contextlib.suppress(Exception):
                await store.write(checkpoint, namespace=namespace)  # type: ignore[arg-type]

        async def _persist_in_flight(run_id: str) -> None:
            if store is None:
                return
            async with ck_lock:
                checkpoint.record_in_flight(run_id)
                await _safe_checkpoint_write()

        async def _persist_completed(outcome: RunOutcome) -> None:
            if store is None:
                return
            async with ck_lock:
                checkpoint.record_completed(
                    outcome.run_id, succeeded=outcome.succeeded,
                    dead_lettered=outcome.dead_lettered,
                    fault_kind=outcome.final_failure.kind if outcome.final_failure else None,
                )
                await _safe_checkpoint_write()

        def _tagged_sink(run_id: str) -> OnLogLine | None:
            if on_log_line is None:
                return None

            async def _emit(line: str) -> None:
                await on_log_line(f"[{run_id}] {line}")

            return _emit

        async def _one(spec: JobSpec) -> RunOutcome:
            async with sem:
                # Persist in-flight BEFORE submitting so an interruption mid-run is visible in
                # the checkpoint as in-flight (not lost). Completed treatments are filtered out
                # before we get here, so this never re-marks a completed one.
                await _persist_in_flight(spec.run_id)
                try:
                    outcome = await self.run_with_retries(
                        spec, max_attempts=max_attempts, retryable=retryable,
                        poll_interval=poll_interval, max_wait=max_wait,
                        on_log_line=_tagged_sink(spec.run_id),
                    )
                except Exception as exc:  # isolate: one treatment's error must not sink the sweep
                    # run_with_retries didn't reach its own outcome-recording, so count this
                    # dead-letter here too (keeps the terminal/fault metrics complete).
                    _safe_metric(instrument.record_run_outcome, succeeded=False,
                                 dead_lettered=True, fault_kind=UNKNOWN)
                    outcome = RunOutcome(spec.run_id, succeeded=False, dead_lettered=True,
                                         final_failure=Failure(UNKNOWN, message=f"orchestration error: {exc}"))
                await _persist_completed(outcome)
                return outcome

        # Partition: a treatment already recorded COMPLETED in the cluster checkpoint is skipped
        # and its prior outcome is reconstructed from the checkpoint (so the merged result
        # covers all N); only the remainder is actually run.
        to_run = [s for s in specs if not checkpoint.is_completed(s.run_id)]
        resumed = [s.run_id for s in specs if checkpoint.is_completed(s.run_id)]
        prior = [_outcome_from_checkpoint(checkpoint, s.run_id) for s in specs
                 if checkpoint.is_completed(s.run_id)]

        fresh = await asyncio.gather(*[_one(s) for s in to_run])
        # Preserve the input order in the merged result (prior outcomes slotted by spec order).
        by_id: dict[str, RunOutcome] = {o.run_id: o for o in (list(prior) + list(fresh))}
        merged = [by_id[s.run_id] for s in specs if s.run_id in by_id]
        return SweepOutcome(outcomes=merged, resumed=resumed)

    async def cleanup(self, *, namespace: str, session_id: str | None = None,
                      sweep_id: str | None = None, only_terminal: bool = True) -> list[str]:
        """Reap the agent's Jobs (optionally scoped to a session/sweep). Only terminal Jobs
        are removed by default, so an in-flight run is never killed. Deleting a Job does not
        touch the results PVC, so benchmark artifacts are preserved. Returns deleted names."""
        statuses = await self.reconstruct(namespace=namespace, session_id=session_id, sweep_id=sweep_id)
        deleted: list[str] = []
        for st in statuses:
            if only_terminal and not st.terminal:
                continue
            await self._kube.delete_job(st.name, namespace=namespace)
            deleted.append(st.name)
        return deleted
