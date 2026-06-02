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

from app.observability.resource_poller import resource_stats_poller
from app.orchestrator.controller import BenchmarkOrchestrator, RunOutcome
from app.orchestrator.job import JobSpec, Scheduling
from app.orchestrator.kube import RealKubeClient
from app.tools.context import ToolContext, ToolError
from app.tools.readiness import check_endpoint_readiness


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
    scheduling: dict[str, Any] | None = None,
    active_deadline_seconds: int | None = None,
    max_attempts: int = 1,
    watch: bool = True,
    poll_interval: float = 3.0,
    max_wait: float = 3600.0,
    require_ready_endpoint: bool = True,
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

    # Parse the agent's scheduling intent (node affinity / GPU selection / anti-starvation
    # placement). PURE PARSING — the WHICH-GPU / WHERE-to-place choice is the agent's judgment
    # (knowledge/resource_management.md); a malformed shape is a ToolError the agent self-corrects.
    try:
        sched = Scheduling.from_dict(scheduling)
    except ValueError as exc:
        raise ToolError(f"invalid scheduling: {exc}") from exc

    # Phase 24: GATE on a real inference-endpoint readiness check before submitting — don't
    # benchmark an unready stack. This goes BEYOND pod presence: it verifies a Service has a
    # READY backing endpoint (see app/orchestrator/readiness.py). When not ready we submit
    # NOTHING and return a structured not-ready outcome carrying a standup suggestion the agent
    # can OFFER (approval-gated). The DECISION to stand up is the agent's/user's judgment; this
    # is just the mechanism. Skipped in simulate mode (the synthetic walk deploys nothing) and
    # when the caller explicitly opts out (e.g. it just stood the stack up and knows it's ready).
    if require_ready_endpoint and not ctx.settings.simulate:
        readiness = await check_endpoint_readiness(ctx, namespace=namespace, spec=spec)
        if not readiness.get("ready"):
            return {
                "submitted": False,
                "ready": False,
                "namespace": namespace,
                "readiness": readiness,
                "standup_suggestion": readiness.get("standup_suggestion"),
                "note": "Benchmark NOT submitted: the inference endpoint in this namespace is "
                        "not ready (no Service has a ready backing endpoint). Nothing was "
                        "mutated. Offer to stand up a stack first (approval-gated) — see "
                        "standup_suggestion — or pass require_ready_endpoint=false to override "
                        "if you know the endpoint is reachable another way.",
            }

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
        scheduling=sched,
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

    # Stream live cluster CPU/memory for the run alongside it — backend-only, zero LLM cost (it
    # never enters the message stream). Namespace-wide (the bench namespace is dedicated, so no
    # run-id selector needed). No-op without a UI emitter or in simulate mode.
    async with resource_stats_poller(ctx, namespace=namespace):
        outcome = await orch.run_with_retries(
            spec_obj, max_attempts=max_attempts, poll_interval=poll_interval, max_wait=max_wait,
            on_log_line=on_log_line,
        )
    return {"namespace": namespace, **_serialize_outcome(outcome)}
