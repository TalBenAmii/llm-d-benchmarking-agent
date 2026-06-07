"""Shared dependencies passed to every tool handler.

Bundles config, the security allowlist, the command runner, and the per-session
workspace. Provides a single ``run_readonly`` helper so even probe commands pass
through the allowlist gate (defense in depth — nothing bypasses validation).
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import Settings
from app.security.allowlist import Allowlist
from app.security.quota import QuotaCounter, QuotaExceeded
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


class QuotaError(ToolError):
    """Raised BEFORE execution when a command would exceed its allowlist-declared usage
    quota (per_session / per_day). It is a ToolError, so the agent loop already relays it
    as a clean tool error; the carried fields (key/window/cap/used) make it structured."""

    def __init__(self, exc: QuotaExceeded):
        super().__init__(str(exc))
        self.key = exc.key
        self.window = exc.window
        self.cap = exc.cap
        self.used = exc.used


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
    # Shared across sessions (Phase 16): the in-flight-run registry the cancel tool reaches to
    # cancel a still-running background turn (and free its concurrency slot). None in contexts
    # that have no lifecycle wiring (e.g. unit tests of a single tool). Mechanism only.
    runs: Any = field(default=None, repr=False)
    # The id of THIS context's own session, so the cancel tool can refuse to cancel the very
    # turn it is running inside (that would deadlock: cancel-self then await-self).
    session_id: str | None = field(default=None, repr=False)
    # Per-session usage-quota counter (Phase 13). MECHANISM only — the CAPS come from the
    # allowlist DATA via the Decision; this just tallies and compares. One per session
    # (a ToolContext is created per session), so per_session counts are naturally scoped.
    quota: QuotaCounter = field(default_factory=QuotaCounter, repr=False)
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

    async def run_readonly(
        self, argv: list[str], *, timeout: float | None = 20.0, quiet: bool = False
    ) -> RunResult:
        """Validate + run a command that MUST be read-only. Raises if the allowlist
        would not classify it read-only (these are trusted probes, but we still gate).
        The policy's ``timeout_s`` (if declared) supersedes ``timeout``; probes default to
        a short 20s bound when the policy declares none.

        ``quiet=True`` skips ONLY the ``command`` event emit — every gate (allowlist, read-only
        classification, quota) still applies and the command still runs/records metrics. Used by
        the live resource poller so its 5s ``kubectl top`` polls don't flood the persisted,
        500-capped command trail with hundreds of identical rows.

        Mechanism lives in app/tools/command_exec.py (CommandExecutor); this thin delegator keeps
        the execution engine separable from this dependency-injection container."""
        from app.tools.command_exec import CommandExecutor
        return await CommandExecutor(self).run_readonly(argv, timeout=timeout, quiet=quiet)

    async def run_command(
        self,
        argv: list[str],
        *,
        timeout: float | None = None,
        cwd: str | Path | None = None,
        stream: bool = True,
        on_line: Callable[[str], Awaitable[None]] | None = None,
        env: dict[str, str] | None = None,
    ) -> RunResult:
        """Validate, gate (approval if mutating), then run a command — streaming output
        to the UI. Read-only commands auto-run; mutating commands require approval via
        the wired ``approve`` callback and raise :class:`ApprovalRejected` if declined.

        ``on_line`` (when given) receives each output line as it arrives and OVERRIDES the
        default UI ``output`` emission (so the caller — e.g. the orchestrator's live log tail —
        owns where each line goes, while still passing through the SAME allowlist/runner path).
        ``stream`` still gates the default UI emission when ``on_line`` is not supplied.

        ``env`` is a BACKEND-ONLY per-run env overlay merged last into the child process
        environment (e.g. a right-sized ``LLMDBENCH_HARNESS_CPU_NR`` for a small Kind node).
        It is never surfaced to the browser: the emitted ``command`` event carries only
        argv/text/mode, so the env never appears in any event, log, or scrubbed UI surface.

        Mechanism lives in app/tools/command_exec.py (CommandExecutor); this thin delegator keeps
        the execution engine separable from this dependency-injection container."""
        from app.tools.command_exec import CommandExecutor
        return await CommandExecutor(self).run_command(
            argv, timeout=timeout, cwd=cwd, stream=stream, on_line=on_line, env=env
        )
