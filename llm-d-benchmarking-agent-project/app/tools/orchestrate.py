"""Agent tool: run a benchmark as a Kubernetes Job via the orchestrator.

Thin wiring — it builds a :class:`~app.orchestrator.job.JobSpec` from the agent's intent and
drives :class:`~app.orchestrator.controller.BenchmarkOrchestrator` (submit → watch → diagnose,
with optional retry). All cluster access flows through the allowlisted kubectl runner on the
session's ToolContext, so apply/delete stay approval-gated. Judgment (which spec/harness/
workload, retry budget) is the agent's; this is mechanism.
"""
from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from app.orchestrator.controller import BenchmarkOrchestrator, RunOutcome
from app.orchestrator.job import JobSpec
from app.orchestrator.kube import RealKubeClient
from app.tools.context import ToolContext, ToolError


def _live_log_sink(ctx: ToolContext) -> Callable[[str], Awaitable[None]] | None:
    """Build the per-line sink that forwards a benchmark pod's live logs to the UI as
    ``output`` events — the exact event the runner already emits for streamed command output,
    so the UI renders pod logs with no new transport. Returns None when no emitter is wired
    (streaming is then simply disabled), keeping non-UI callers unchanged."""
    emit = ctx.emit
    if emit is None:
        return None

    async def _sink(line: str) -> None:
        await emit("output", {"line": line})

    return _sink


def _default_command(spec: str | None, harness: str | None, workload: str | None, namespace: str) -> list[str]:
    argv = ["llmdbenchmark"]
    if spec:
        argv += ["--spec", spec]
    argv += ["run", "-p", namespace]
    if harness:
        argv += ["-l", harness]
    if workload:
        argv += ["-w", workload]
    return argv


def _serialize_failure(failure) -> dict[str, Any] | None:
    if failure is None or not getattr(failure, "is_failure", False):
        return None
    return {"kind": failure.kind, "message": failure.message, "pod": failure.pod,
            "container": failure.container, "exit_code": failure.exit_code}


def _serialize_outcome(outcome: RunOutcome) -> dict[str, Any]:
    return {
        "run_id": outcome.run_id,
        "succeeded": outcome.succeeded,
        "dead_lettered": outcome.dead_lettered,
        "attempts": [
            {"run_id": a.run_id, "phase": a.status.phase, "reason": a.status.reason,
             "failure": _serialize_failure(a.failure)}
            for a in outcome.attempts
        ],
        "final_failure": _serialize_failure(outcome.final_failure),
    }


async def orchestrate_benchmark_run(
    ctx: ToolContext,
    *,
    namespace: str,
    spec: str | None = None,
    harness: str | None = None,
    workload: str | None = None,
    image: str | None = None,
    service_account: str | None = None,
    command: list[str] | None = None,
    cpu: str = "1",
    memory: str = "1Gi",
    active_deadline_seconds: int | None = None,
    max_attempts: int = 1,
    watch: bool = True,
    poll_interval: float = 3.0,
    max_wait: float = 3600.0,
) -> dict[str, Any]:
    image = image or ctx.settings.orchestrator_image
    if not image:
        raise ToolError(
            "an orchestrated benchmark run is a real Kubernetes Job and needs a container "
            "image carrying the llmdbenchmark CLI + kubectl — set ORCHESTRATOR_IMAGE in the "
            "backend .env or pass `image`. (The in-cluster agent image is built in the "
            "packaging phase; until then, use execute_llmdbenchmark for the local CLI path.)"
        )

    # Run the Job under the least-privilege ServiceAccount the packaging deploy creates (so an
    # in-cluster orchestrated run has exactly the RBAC it needs); empty → namespace default SA.
    sa = service_account if service_account is not None else (ctx.settings.orchestrator_service_account or None)

    run_id = uuid.uuid4().hex[:8]
    spec_obj = JobSpec(
        run_id=run_id,
        namespace=namespace,
        image=image,
        command=command or _default_command(spec, harness, workload, namespace),
        session_id=ctx.workspace.name,        # session dir name → labels for reconstruction
        spec=spec or "",
        harness=harness or "",
        workload=workload or "",
        active_deadline_seconds=active_deadline_seconds,
        cpu=cpu,
        memory=memory,
        service_account=sa,
    )

    orch = BenchmarkOrchestrator(RealKubeClient(ctx), ctx.workspace)

    if not watch:
        job = await orch.submit(spec_obj)
        return {"submitted": True, "run_id": run_id, "job": job, "namespace": namespace,
                "note": "Job submitted; not watched. Use list_orchestrated_runs to check status."}

    # Phase 21: forward the benchmark pod's live log lines to the UI as `output` events — the
    # SAME transport the runner already uses for streamed command output — so the user sees
    # benchmark progress in real time during the run, not just at the end. Best-effort: a
    # failing tail never breaks the run (guarded in the orchestrator), and with no emitter
    # wired (e.g. a bare unit test) streaming is simply disabled.
    on_log_line = _live_log_sink(ctx)

    outcome = await orch.run_with_retries(
        spec_obj, max_attempts=max_attempts, poll_interval=poll_interval, max_wait=max_wait,
        on_log_line=on_log_line,
    )
    return {"namespace": namespace, **_serialize_outcome(outcome)}
