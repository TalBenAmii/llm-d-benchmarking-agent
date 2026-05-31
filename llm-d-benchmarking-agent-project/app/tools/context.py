"""Shared dependencies passed to every tool handler.

Bundles config, the security allowlist, the command runner, and the per-session
workspace. Provides a single ``run_readonly`` helper so even probe commands pass
through the allowlist gate (defense in depth — nothing bypasses validation).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.config import Settings
from app.security.allowlist import READ_ONLY, Allowlist, Decision
from app.security.runner import CommandRunner, RunResult
from app.tools.catalog import build_catalog, catalog_for_allowlist


class ToolError(RuntimeError):
    pass


class ApprovalRejected(RuntimeError):
    """Raised when the user rejects a mutating command at the approval gate."""

    def __init__(self, argv: list[str]):
        super().__init__("user rejected the command: " + " ".join(argv))
        self.argv = argv


# Callbacks the agent loop wires in per dispatch.
#   request_approval(kind, payload) -> approved?   (kind is "command" or "session_plan")
#   emit(event_type, payload)                       (stream to the UI)
ApproveFn = Callable[[str, dict[str, Any]], Awaitable[bool]]
EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass
class ToolContext:
    settings: Settings
    allowlist: Allowlist
    runner: CommandRunner
    workspace: Path
    # Wired by the agent loop before each tool dispatch.
    request_approval: ApproveFn | None = field(default=None, repr=False)
    emit: EmitFn | None = field(default=None, repr=False)
    _catalog: dict[str, Any] | None = field(default=None, repr=False)

    def catalog(self, *, refresh: bool = False) -> dict[str, Any]:
        if self._catalog is None or refresh:
            self._catalog = build_catalog(self.settings.bench_repo)
        return self._catalog

    def catalog_for_allowlist(self) -> dict[str, list[str]]:
        return catalog_for_allowlist(self.catalog())

    async def _emit_line(self, line: str) -> None:
        if self.emit is not None:
            await self.emit("output", {"line": line})

    async def run_readonly(self, argv: list[str], *, timeout: float = 20.0) -> RunResult:
        """Validate + run a command that MUST be read-only. Raises if the allowlist
        would not classify it read-only (these are trusted probes, but we still gate)."""
        decision = self.allowlist.validate(argv, catalog=self.catalog_for_allowlist())
        if not decision.allowed:
            raise ToolError(f"probe command denied by allowlist: {decision.reason}")
        if decision.mode != READ_ONLY:
            raise ToolError(f"probe command is not read-only: {' '.join(argv)}")
        entry = self.allowlist.executable(argv[0])
        return await self.runner.execute(argv, entry, timeout=timeout)

    async def run_command(
        self,
        argv: list[str],
        *,
        timeout: float | None = None,
        cwd: str | Path | None = None,
        stream: bool = True,
    ) -> RunResult:
        """Validate, gate (approval if mutating), then run a command — streaming output
        to the UI. Read-only commands auto-run; mutating commands require approval via
        the wired ``approve`` callback and raise :class:`ApprovalRejected` if declined."""
        decision = self.allowlist.validate(argv, catalog=self.catalog_for_allowlist())
        if not decision.allowed:
            raise ToolError(f"command denied by allowlist: {decision.reason}")
        if decision.requires_approval:
            if self.request_approval is None:
                raise ToolError("approval required but no approver is wired")
            payload = {"command": " ".join(decision.argv), "argv": decision.argv, "mode": decision.mode}
            if not await self.request_approval("command", payload):
                raise ApprovalRejected(argv)
        entry = self.allowlist.executable(argv[0])
        on_line = self._emit_line if (stream and self.emit is not None) else None
        return await self.runner.execute(argv, entry, on_line=on_line, timeout=timeout, cwd=cwd)
