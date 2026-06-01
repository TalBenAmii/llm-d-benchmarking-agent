"""Shared dependencies passed to every tool handler.

Bundles config, the security allowlist, the command runner, and the per-session
workspace. Provides a single ``run_readonly`` helper so even probe commands pass
through the allowlist gate (defense in depth — nothing bypasses validation).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.config import Settings
from app.observability import instrument
from app.security.allowlist import MUTATING, READ_ONLY, Allowlist, Decision
from app.security.runner import CommandRunner, RunResult
from app.storage.history import HistoryStore
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
    # Id of the tool call currently being dispatched; set by the loop so an approval gate
    # raised mid-dispatch can be tied back to its tool call (for ordered history replay).
    current_tool_call_id: str | None = field(default=None, repr=False)
    # Shared across sessions: caps concurrent heavy (mutating) executions so parallel
    # benchmark runs stay bounded. None = unlimited.
    run_semaphore: asyncio.Semaphore | None = field(default=None, repr=False)
    _catalog: dict[str, Any] | None = field(default=None, repr=False)

    def catalog(self, *, refresh: bool = False) -> dict[str, Any]:
        if self._catalog is None or refresh:
            self._catalog = build_catalog(self.settings.bench_repo)
        return self._catalog

    def history_store(self) -> HistoryStore:
        """The cross-session historical-result store. Rooted at the SHARED workspace root
        (the parent of the per-session ``sessions/<id>`` dir) so stored results persist
        across sessions; for a bare workspace (e.g. tests) it sits beside it. Resolving it
        from ``self.workspace`` rather than settings keeps it co-located with whatever
        workspace this context actually uses (and hermetic in tests)."""
        ws = self.workspace
        root = ws.parent.parent if ws.parent.name == "sessions" else ws.parent
        return HistoryStore(root)

    def catalog_for_allowlist(self) -> dict[str, list[str]]:
        return catalog_for_allowlist(self.catalog())

    async def _emit_line(self, line: str) -> None:
        if self.emit is not None:
            await self.emit("output", {"line": line})

    async def _emit_command(self, decision: Decision, *, auto_run: bool) -> None:
        """Announce a command the instant before it runs — for EVERY execution, not just
        the approval-gated ones, so the UI can show the full executed-command trail and a
        debug view. ``auto_run`` is True for read-only commands that ran without a prompt."""
        if self.emit is not None:
            await self.emit("command", {
                "argv": list(decision.argv),
                "text": " ".join(decision.argv),
                "mode": decision.mode,
                "auto_run": auto_run,
            })

    def _record_metric(self, decision: Decision, *, auto_run: bool, result: RunResult) -> None:
        """File the executed-command fact into the metrics registry. Best-effort: observability
        must never break command execution, so any error here is swallowed. ``exe`` is argv[0]
        only (bounded cardinality — never the full argv, which would explode the label space)."""
        try:
            instrument.record_command(
                exe=decision.argv[0] if decision.argv else "",
                mode=decision.mode,
                auto_run=auto_run,
                duration_s=result.duration_s,
            )
        except Exception:  # noqa: BLE001 — metrics must not affect the run
            pass

    async def run_readonly(self, argv: list[str], *, timeout: float = 20.0) -> RunResult:
        """Validate + run a command that MUST be read-only. Raises if the allowlist
        would not classify it read-only (these are trusted probes, but we still gate)."""
        decision = self.allowlist.validate(argv, catalog=self.catalog_for_allowlist())
        if not decision.allowed:
            raise ToolError(f"probe command denied by allowlist: {decision.reason}")
        if decision.mode != READ_ONLY:
            raise ToolError(f"probe command is not read-only: {' '.join(argv)}")
        entry = self.allowlist.executable(argv[0])
        await self._emit_command(decision, auto_run=True)
        result = await self.runner.execute(argv, entry, timeout=timeout)
        self._record_metric(decision, auto_run=True, result=result)
        return result

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
        # Announce the command for the full executed-command trail / debug view. For a
        # mutating command this fires only after approval, so it records what truly ran.
        await self._emit_command(decision, auto_run=not decision.requires_approval)
        on_line = self._emit_line if (stream and self.emit is not None) else None
        auto_run = not decision.requires_approval
        # Bound concurrent heavy runs across sessions (read-only commands run uncapped).
        if self.run_semaphore is not None and decision.mode == MUTATING:
            async with self.run_semaphore:
                result = await self.runner.execute(argv, entry, on_line=on_line, timeout=timeout, cwd=cwd)
        else:
            result = await self.runner.execute(argv, entry, on_line=on_line, timeout=timeout, cwd=cwd)
        self._record_metric(decision, auto_run=auto_run, result=result)
        return result
