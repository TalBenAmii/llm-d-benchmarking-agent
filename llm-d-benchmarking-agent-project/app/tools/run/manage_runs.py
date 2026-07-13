"""manage_orchestrated_runs tool: list / stop / reap the orchestrator's Kubernetes Jobs.

``cancel_run`` (merged into this module, below) stops only the in-process agent WATCH task — a real
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

All cluster access flows through the same policy-allowed ``kubectl`` runner on the session's
ToolContext (deny-by-default policy + env scrub), so ``list`` auto-runs while ``stop`` and
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


# ── cancel_run (merged from app/tools/cancel.py) ──────────────────────────────
# cancel_run tool (Phase 16): cancel a still-running background benchmark/turn so it stops
# holding a concurrency-cap slot and its subprocess/Job is cleaned up.
#
# This is pure MECHANISM: it asks the in-flight-run registry (``ToolContext.runs``) to cancel a
# session's turn task. Cancelling the task releases any semaphore slot it holds (asyncio unwinds
# the ``async with run_semaphore`` in ``ToolContext.run_command``) and the runner reaps the child
# process group — so the freed slot AND the no-orphan guarantee both fall out of the one cancel.
#
# The JUDGMENT about *when* to cancel a run (it's clearly stuck, the user changed their mind, a
# slot is needed for a more important run) lives in ``knowledge/run_lifecycle.md`` — never here.
# The tool refuses to cancel the very turn it is running inside (that would deadlock), so a run is
# always cancelled from OUTSIDE itself (another chat's agent, or the user's cancel control message).


async def cancel_run(
    ctx: ToolContext,
    *,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Cancel the in-flight run for ``session_id`` (a chat id surfaced in /api/sessions or the
    `ready` event). Frees its concurrency slot and cleans up the subprocess. Auto-runs (it stops
    work rather than starting any mutation), and is idempotent: cancelling a session with no live
    run reports ``cancelled=False`` rather than erroring."""
    if ctx.runs is None:
        raise ToolError("run cancellation is not available in this context")
    if not session_id or not isinstance(session_id, str):
        raise ToolError("session_id is required (the chat id of the run to cancel)")
    if ctx.session_id is not None and session_id == ctx.session_id:
        # Cancelling our own turn would cancel-then-await ourselves: a deadlock. A run is always
        # cancelled from outside itself.
        raise ToolError("cannot cancel the run you are calling this from; cancel a DIFFERENT "
                        "session's run, or stop this one with the cancel control instead")
    if not ctx.runs.is_running(session_id):
        return {"session_id": session_id, "cancelled": False,
                "note": "no in-flight run for that session (it may have already finished)"}
    cancelled = await ctx.runs.cancel(session_id)
    return {
        "session_id": session_id,
        "cancelled": bool(cancelled),
        "slot_released": bool(cancelled),
        "note": "the run was cancelled; its concurrency slot is freed and its subprocess "
                "cleaned up" if cancelled else "no in-flight run to cancel",
    }


# ── observe_run_metrics (merged from app/tools/observe.py) ────────────────────
# observe_run_metrics — live system metrics during a run (Phase 7 observability).
#
# Read-only. Surfaces the cluster's live CPU/memory usage for the benchmark pods (and,
# optionally, nodes) via the policy-allowed ``kubectl top`` (which reads the in-cluster
# metrics-server). This is the "live system metrics during a run" half of the observability
# phase; the agent/orchestrator's *own* counters are exported separately at ``/metrics`` in
# Prometheus format.
#
# Mechanism only: it runs the read-only probe and parses ``kubectl top``'s columnar text into
# structured rows. It does NOT decide whether a number is "too high", whether to scale, or what
# to do next — that judgment is the agent's, guided by ``knowledge/observability.md``. The
# ``-l`` selector reuses the orchestrator's run-id label so usage can be scoped to one run.

_VALID_SCOPES = ("pods", "nodes")


def _parse_top_table(text: str) -> list[dict[str, str]]:
    """Parse ``kubectl top``'s whitespace-aligned table into a list of row dicts keyed by the
    header. ``kubectl top`` emits a header row then data rows; we key each cell by its column
    name lower-cased (e.g. NAME -> name, ``CPU(cores)`` -> cpu(cores)). Empty/garbled output
    yields ``[]`` — the agent then relays that no metrics were available."""
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    if len(lines) < 2:
        return []
    header = lines[0].split()
    keys = [h.lower() for h in header]
    rows: list[dict[str, str]] = []
    for ln in lines[1:]:
        cells = ln.split()
        if len(cells) != len(keys):
            # A column may be empty/missing on a partial line; skip rather than misalign.
            continue
        rows.append(dict(zip(keys, cells, strict=True)))
    return rows


async def observe_run_metrics(
    ctx: ToolContext,
    *,
    namespace: str,
    scope: str = "pods",
    run_id: str | None = None,
    containers: bool = False,
) -> dict[str, Any]:
    """Read live CPU/memory usage from the cluster for the current run (or namespace/nodes).

    ``scope='pods'`` (default) lists pod usage in ``namespace``, optionally narrowed to one
    orchestrated run via ``run_id`` (the orchestrator's run-id label). ``scope='nodes'`` lists
    node usage (cluster-wide). Returns the parsed rows plus the exact command run. Read-only —
    requires the in-cluster metrics-server, which is NOT installed by kind or the cicd/kind
    spec; it must be added to the cluster separately (on kind, with --kubelet-insecure-tls). If
    it is unavailable the probe fails read-only and that fact is returned."""
    if scope not in _VALID_SCOPES:
        raise ToolError(f"scope must be one of {_VALID_SCOPES}, got {scope!r}")

    if scope == "nodes":
        argv = ["kubectl", "top", "nodes"]
    else:
        argv = ["kubectl", "top", "pods", "-n", namespace]
        if run_id:
            argv += ["-l", f"{LABEL_RUN}={run_id}"]
        if containers:
            argv += ["--containers"]

    res = await ctx.run_readonly(argv, timeout=20.0)
    if res.exit_code != 0:
        tail = (res.output or "").strip()[-400:]
        return {
            "scope": scope,
            "namespace": namespace if scope == "pods" else None,
            "available": False,
            "command": " ".join(argv),
            "note": "kubectl top failed — the metrics-server may not be installed/ready in "
                    "this cluster. This is read-only; nothing was changed.",
            "error_tail": tail,
        }

    rows = _parse_top_table(res.output)
    return {
        "scope": scope,
        "namespace": namespace if scope == "pods" else None,
        "run_id": run_id if scope == "pods" else None,
        "available": True,
        "command": " ".join(argv),
        "rows": rows,
        "row_count": len(rows),
    }
