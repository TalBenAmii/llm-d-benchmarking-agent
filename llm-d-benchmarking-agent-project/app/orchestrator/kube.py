"""Kubernetes access for the orchestrator — a thin abstraction over the allowlisted
``kubectl`` runner.

We shell out to ``kubectl`` (consistent with the agent's deny-by-default security model and
with how llm-d-benchmark itself talks to the cluster) rather than the Python kubernetes
client, which would bypass the allowlist, the approval gate, and the env scrub. ``apply`` and
``delete`` are mutating (approval-gated via ``ctx.run_command``); ``get``/``logs`` are
read-only and auto-run. A ``FakeKubeClient`` (in tests) mirrors this interface so the whole
Job lifecycle is testable with no cluster.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from app.security.runner import RunResult
from app.tools.context import ToolContext


class KubeError(RuntimeError):
    pass


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def parse_items(output: str) -> list[dict[str, Any]]:
    """Parse ``kubectl get ... -o json`` output into a list of objects. A plural ``get``
    returns a ``List`` with ``items``; a single object is wrapped into a one-element list."""
    text = (output or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict) and "items" in data:
        return list(data.get("items") or [])
    if isinstance(data, dict) and data.get("kind"):
        return [data]
    return []


@runtime_checkable
class KubeClient(Protocol):
    """The cluster operations the orchestrator needs. Both the real (kubectl-shelling)
    client and the test fake implement this."""

    async def apply(self, manifest_path: str | Path, *, namespace: str) -> RunResult: ...
    async def list_jobs(self, *, namespace: str, selector: str | None = None) -> list[dict[str, Any]]: ...
    async def list_pods(self, *, namespace: str, selector: str | None = None) -> list[dict[str, Any]]: ...
    async def logs(self, *, namespace: str, selector: str, tail: int | None = None,
                   follow: bool = False) -> str: ...
    async def delete_job(self, name: str, *, namespace: str, ignore_not_found: bool = True) -> RunResult: ...


class RealKubeClient:
    """Shells out to allowlisted ``kubectl`` through the session's :class:`ToolContext`.

    apply/delete route through ``ctx.run_command`` (mutating → approval-gated, concurrency-
    capped); get/logs are read-only and auto-run (logs stream to the UI via the standard
    ``output`` event). ``-f`` manifests are confined to the session workspace — defense in
    depth on top of the allowlist's ``.yaml``-only regex."""

    def __init__(self, ctx: ToolContext):
        self._ctx = ctx

    def _confine_to_workspace(self, manifest_path: str | Path) -> Path:
        p = Path(manifest_path).resolve()
        ws = self._ctx.settings.resolved_workspace_dir.resolve()
        if not _is_within(p, ws):
            raise KubeError(f"refusing to apply a manifest outside the workspace: {p}")
        return p

    async def apply(self, manifest_path: str | Path, *, namespace: str) -> RunResult:
        p = self._confine_to_workspace(manifest_path)
        return await self._ctx.run_command(["kubectl", "apply", "-f", str(p), "-n", namespace])

    async def list_jobs(self, *, namespace: str, selector: str | None = None) -> list[dict[str, Any]]:
        argv = ["kubectl", "get", "jobs", "-n", namespace, "-o", "json"]
        if selector:
            argv += ["-l", selector]
        res = await self._ctx.run_readonly(argv)
        return parse_items(res.output)

    async def list_pods(self, *, namespace: str, selector: str | None = None) -> list[dict[str, Any]]:
        argv = ["kubectl", "get", "pods", "-n", namespace, "-o", "json"]
        if selector:
            argv += ["-l", selector]
        res = await self._ctx.run_readonly(argv)
        return parse_items(res.output)

    async def logs(self, *, namespace: str, selector: str, tail: int | None = None,
                   follow: bool = False) -> str:
        argv = ["kubectl", "logs", "-l", selector, "-n", namespace]
        if tail is not None:
            argv += ["--tail", str(tail)]
        if follow:
            argv += ["-f"]
        # read-only → auto-runs and streams to the UI via the standard `output` event.
        res = await self._ctx.run_command(argv)
        return res.output

    async def delete_job(self, name: str, *, namespace: str, ignore_not_found: bool = True) -> RunResult:
        argv = ["kubectl", "delete", "job", name, "-n", namespace]
        if ignore_not_found:
            argv += ["--ignore-not-found"]
        return await self._ctx.run_command(argv)
