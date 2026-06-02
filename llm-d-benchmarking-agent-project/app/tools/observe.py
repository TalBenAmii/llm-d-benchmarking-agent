"""observe_run_metrics — live system metrics during a run (Phase 7 observability).

Read-only. Surfaces the cluster's live CPU/memory usage for the benchmark pods (and,
optionally, nodes) via the allowlisted ``kubectl top`` (which reads the in-cluster
metrics-server). This is the "live system metrics during a run" half of the observability
phase; the agent/orchestrator's *own* counters are exported separately at ``/metrics`` in
Prometheus format.

Mechanism only: it runs the read-only probe and parses ``kubectl top``'s columnar text into
structured rows. It does NOT decide whether a number is "too high", whether to scale, or what
to do next — that judgment is the agent's, guided by ``knowledge/observability.md``. The
``-l`` selector reuses the orchestrator's run-id label so usage can be scoped to one run.
"""
from __future__ import annotations

from typing import Any

from app.orchestrator.job import LABEL_RUN
from app.tools.context import ToolContext, ToolError

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
    requires the in-cluster metrics-server (kind enables it for the cicd/kind spec); if it is
    unavailable the probe fails read-only and that fact is returned."""
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
