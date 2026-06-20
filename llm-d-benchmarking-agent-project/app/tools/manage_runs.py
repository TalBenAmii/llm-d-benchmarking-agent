"""manage_orchestrated_runs tool: list / stop / reap the orchestrator's Kubernetes Jobs.

``cancel_run`` (app/tools/cancel.py) stops only the in-process agent WATCH task — a real
Kubernetes Job the orchestrator submitted keeps running on the cluster after that. This tool
reaches the CLUSTER itself:

  * ``list``    — classify the agent-managed benchmark Jobs fresh from the cluster
                  (``BenchmarkOrchestrator.reconstruct``; the cluster is the source of truth,
                  nothing is held locally). Read-only → auto-runs.
  * ``stop``    — DELETE the still-running Jobs in scope (``kubectl delete job``, mutating →
                  approval-gated) so the cluster work actually stops, not just the watch.
  * ``cleanup`` — reap only TERMINAL Jobs (``cleanup``) to tidy the namespace; an in-flight run
                  is never killed. Approval-gated (it deletes Jobs).

Deleting a Job never touches the results PVC, so benchmark artifacts survive a stop/cleanup.

All cluster access flows through the same allowlisted ``kubectl`` runner on the session's
ToolContext (deny-by-default allowlist + env scrub), so ``list`` auto-runs while ``stop`` and
``cleanup`` route through the approval gate like every other mutation. Judgment about WHEN to
stop or reap a run lives in knowledge/run_lifecycle.md — never here; this is mechanism.
"""
from __future__ import annotations

from typing import Any

from app.orchestrator.controller import BenchmarkOrchestrator
from app.orchestrator.job import LABEL_RUN, LABEL_SESSION, LABEL_SWEEP, LABEL_TREATMENT
from app.orchestrator.kube import RealKubeClient
from app.tools.context import ToolContext, ToolError


def _labels(status: Any) -> dict[str, str]:
    raw = getattr(status, "raw", None) or {}
    return (raw.get("metadata") or {}).get("labels") or {}


def serialize_status(status: Any) -> dict[str, Any]:
    """Flat, JSON-safe view of one orchestrated Job's live status: its phase plus the labels that
    say WHICH run/sweep/treatment/session it is. Classified fresh from ``kubectl get`` — nothing
    is held locally, so this is always the cluster's current truth."""
    labels = _labels(status)
    return {
        "name": status.name,
        "phase": status.phase,
        "terminal": status.terminal,
        "active": status.active,
        "succeeded": status.succeeded,
        "failed": status.failed,
        "reason": status.reason,
        "message": status.message,
        "run_id": labels.get(LABEL_RUN, ""),
        "sweep_id": labels.get(LABEL_SWEEP, ""),
        "treatment": labels.get(LABEL_TREATMENT, ""),
        "session_id": labels.get(LABEL_SESSION, ""),
    }


async def manage_orchestrated_runs(
    ctx: ToolContext,
    *,
    namespace: str,
    action: str = "list",
    session_id: str | None = None,
    sweep_id: str | None = None,
) -> dict[str, Any]:
    """List / stop / reap the orchestrator's K8s Jobs. ``stop`` and ``cleanup`` delete Jobs
    (approval-gated); ``list`` is read-only. Scope to a session and/or a sweep, or omit both to
    span every agent-managed Job in the namespace."""
    kube = RealKubeClient(ctx)
    orch = BenchmarkOrchestrator(kube, ctx.workspace)
    scope = {"namespace": namespace, "session_id": session_id, "sweep_id": sweep_id}

    if action == "list":
        statuses = await orch.reconstruct(namespace=namespace, session_id=session_id, sweep_id=sweep_id)
        runs = [serialize_status(s) for s in statuses]
        return {
            "action": "list",
            **scope,
            "runs": runs,
            "n": len(runs),
            "n_active": sum(1 for s in statuses if not s.terminal),
            "n_terminal": sum(1 for s in statuses if s.terminal),
        }

    if action == "stop":
        statuses = await orch.reconstruct(namespace=namespace, session_id=session_id, sweep_id=sweep_id)
        running = [s for s in statuses if not s.terminal]
        stopped: list[str] = []
        for s in running:
            await kube.delete_job(s.name, namespace=namespace)   # mutating → approval-gated
            stopped.append(s.name)
        return {
            "action": "stop",
            **scope,
            "stopped": stopped,
            "n_stopped": len(stopped),
            "note": (
                "deleted the still-running orchestrated Job(s); the cluster work is actually "
                "stopped now (cancel_run only stops the agent's in-process watch). Results "
                "already written to the PVC are preserved."
                if stopped else
                "no still-running orchestrated Jobs in scope to stop"
            ),
        }

    if action == "cleanup":
        deleted = await orch.cleanup(
            namespace=namespace, session_id=session_id, sweep_id=sweep_id, only_terminal=True,
        )
        return {
            "action": "cleanup",
            **scope,
            "deleted": deleted,
            "n_deleted": len(deleted),
            "note": (
                "reaped terminal orchestrated Job(s); in-flight runs were left untouched and "
                "results on the PVC are preserved"
                if deleted else "no terminal orchestrated Jobs in scope to reap"
            ),
        }

    # Unreachable when dispatched through the schema (action is a validated Literal); kept so a
    # direct call surfaces a clean, retryable error instead of a silent no-op.
    raise ToolError(f"unknown action {action!r}; use one of: list, stop, cleanup")
