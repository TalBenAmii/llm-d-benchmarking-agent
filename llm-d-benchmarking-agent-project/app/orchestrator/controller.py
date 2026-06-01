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
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml

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
from app.orchestrator.faults import EVICTED, TIMEOUT, UNKNOWN, Failure, classify_failure
from app.orchestrator.kube import KubeClient

OnStatus = Callable[[JobStatus], Awaitable[None]]

# Faults worth retrying: transient/environmental, where a fresh attempt may succeed. OOM /
# unschedulable / image / timeout are deterministic — retrying changes nothing, so they
# dead-letter immediately (the agent must adjust resources/spec/workload instead).
DEFAULT_RETRYABLE = frozenset({EVICTED, UNKNOWN})

# Floor for the status-poll sleep so poll_interval=0 (a schema-allowed value used in tests)
# can't busy-loop and hammer the cluster; max_wait is bounded on wall-clock independently.
_MIN_POLL_INTERVAL = 0.05


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

    @property
    def succeeded(self) -> list[str]:
        return [o.run_id for o in self.outcomes if o.succeeded]

    @property
    def dead_lettered(self) -> list[str]:
        return [o.run_id for o in self.outcomes if o.dead_lettered]

    @property
    def all_succeeded(self) -> bool:
        return bool(self.outcomes) and all(o.succeeded for o in self.outcomes)


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

    async def run_with_retries(
        self,
        base_spec: JobSpec,
        *,
        max_attempts: int = 3,
        retryable=DEFAULT_RETRYABLE,
        poll_interval: float = 2.0,
        max_wait: float = 7200.0,
        on_status: OnStatus | None = None,
    ) -> RunOutcome:
        """Submit a benchmark run, watching it to terminal; on a *transient* failure
        (``retryable``) resubmit as a FRESH Job (a new attempt, distinct run-id) up to
        ``max_attempts``. A deterministic fault (OOM/unschedulable/image/timeout) or an
        exhausted budget dead-letters the run. Every attempt is its own inspectable Job
        (``<run_id>-a<N>``), so failed attempts remain in the cluster for diagnosis."""
        attempts: list[AttemptResult] = []
        for i in range(1, max_attempts + 1):
            spec_i = replace(base_spec, run_id=f"{base_spec.run_id}-a{i}", attempt=i)
            await self.submit(spec_i)
            st = await self.watch(spec_i.run_id, namespace=spec_i.namespace,
                                  poll_interval=poll_interval, max_wait=max_wait, on_status=on_status)
            if st.phase == SUCCEEDED:
                attempts.append(AttemptResult(spec_i.run_id, st, None))
                return RunOutcome(base_spec.run_id, True, attempts)
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
            return RunOutcome(base_spec.run_id, False, attempts, dead_lettered=True, final_failure=failure)
        return RunOutcome(base_spec.run_id, False, attempts, dead_lettered=True,
                          final_failure=attempts[-1].failure if attempts else None)

    async def run_sweep(
        self,
        specs: list[JobSpec],
        *,
        max_parallel: int = 2,
        max_attempts: int = 2,
        retryable=DEFAULT_RETRYABLE,
        poll_interval: float = 2.0,
        max_wait: float = 7200.0,
    ) -> SweepOutcome:
        """Run a list of treatment specs as parallel Jobs under a concurrency cap, each with
        its own retry/dead-letter budget. A persistently-failing treatment dead-letters
        without sinking the rest of the sweep (the proposal's DoE parallel scheduling +
        dead-letter). Returns a per-treatment outcome roll-up."""
        sem = asyncio.Semaphore(max(1, max_parallel))

        async def _one(spec: JobSpec) -> RunOutcome:
            async with sem:
                try:
                    return await self.run_with_retries(
                        spec, max_attempts=max_attempts, retryable=retryable,
                        poll_interval=poll_interval, max_wait=max_wait,
                    )
                except Exception as exc:  # isolate: one treatment's error must not sink the sweep
                    return RunOutcome(spec.run_id, succeeded=False, dead_lettered=True,
                                      final_failure=Failure(UNKNOWN, message=f"orchestration error: {exc}"))

        outcomes = await asyncio.gather(*[_one(s) for s in specs])
        return SweepOutcome(outcomes=list(outcomes))

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
