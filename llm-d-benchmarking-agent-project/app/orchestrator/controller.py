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
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml

from app.orchestrator.job import (
    ABSENT,
    LABEL_RUN,
    JobSpec,
    JobStatus,
    build_job_manifest,
    classify_job_status,
    job_name,
)
from app.orchestrator.kube import KubeClient

OnStatus = Callable[[JobStatus], Awaitable[None]]


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
        waited = 0.0
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
            if waited >= max_wait:
                return st
            await asyncio.sleep(poll_interval)
            waited += poll_interval

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
