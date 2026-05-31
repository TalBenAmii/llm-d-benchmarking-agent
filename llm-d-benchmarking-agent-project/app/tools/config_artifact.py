"""write_and_validate_config — materialize a generated workload/run config into the
session workspace and validate it before any execution.

MVP scope: the quickstart uses stock profiles, so this is intentionally minimal. It
writes the artifact into the workspace (never into the repos) and does a structural YAML
check. Full validation via the CLI's own ``--dry-run`` / ``--generate-config`` is the
documented extension point for bespoke generated workloads (deferred).
"""
from __future__ import annotations

from typing import Any

import yaml

from app.tools.context import ToolContext, ToolError

_ARTIFACT_TYPES = {"workload", "run_config"}


async def write_and_validate_config(
    ctx: ToolContext,
    *,
    artifact_type: str,
    target_filename: str,
    content: dict[str, Any],
) -> dict[str, Any]:
    if artifact_type not in _ARTIFACT_TYPES:
        raise ToolError(f"artifact_type must be one of {sorted(_ARTIFACT_TYPES)}")
    # Constrain the filename to the workspace — no path traversal, no repo writes.
    if "/" in target_filename or ".." in target_filename or not target_filename.endswith((".yaml", ".yml")):
        raise ToolError("target_filename must be a bare *.yaml name (no path separators)")

    ctx.workspace.mkdir(parents=True, exist_ok=True)
    dest = ctx.workspace / target_filename
    text = yaml.safe_dump(content, sort_keys=False)
    dest.write_text(text)

    # Structural re-parse as a minimal validity gate.
    try:
        yaml.safe_load(text)
        valid, errors = True, []
    except yaml.YAMLError as exc:  # pragma: no cover - defensive
        valid, errors = False, [str(exc)]

    return {
        "path": str(dest),
        "artifact_type": artifact_type,
        "valid": valid,
        "errors": errors,
        "note": "structural check only in MVP; deep validation via CLI --dry-run is deferred",
    }
