"""Shared dependencies passed to every tool handler.

Bundles config, the security policy, the command runner, and the per-session
workspace. Provides a single ``run_readonly`` helper so even probe commands pass
through the policy gate (defense in depth — nothing bypasses validation).
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import Settings
from app.security.policy import CommandPolicy
from app.security.runner import CommandRunner, RunResult
from app.storage.history import HistoryStore
from app.tools.setup.catalog import build_catalog, catalog_for_policy


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
    policy: CommandPolicy
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
    _catalog: dict[str, Any] | None = field(default=None, repr=False)
    # Per-session ledger of which key_docs TASKS were fetched (skill-grounding gate). Populated by
    # fetch_key_docs on the task ARG — regardless of read success, so an absent skills repo can't
    # defeat the gate — and read by app/tools/run/skill_gate.py to REFUSE a mutating operation (and the
    # plan that proposes it) until its grounding doc was fetched this session. RUNTIME-ONLY (not
    # persisted), like gated_access: a resumed chat re-grounds on its next fetch.
    consulted_skills: set[str] = field(default_factory=set, repr=False)
    # Per-session gated-model access verdicts: model_id -> {gated, authorized, gated_reason},
    # recorded by check_capacity and read by the command guardrail (app/tools/run/gated_access.py) to
    # REFUSE a standup/run/smoketest of a model the backend HF token can't pull — a safety gate
    # like the approval gate, not judgment. RUNTIME-ONLY (not persisted), like consulted_skills:
    # it lives for the session process; a resumed chat re-establishes it on its next check_capacity
    # (the mandatory pre-flight before any standup). Overwriting an entry on re-check is how the
    # block clears (authorized:false -> authorized:true). Mechanism only.
    gated_access: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    # Mid-turn user STEER queue (Claude-Code style). Any message the user types WHILE a turn is
    # running is dropped here by the WS handler (app/main.py): (a) mid-thinking — no gate open —
    # the message is simply queued; (b) type-instead-of-approve — the handler ALSO declines the
    # open gate. Either way the agent loop drains this queue at its next step boundary, injecting
    # each entry into the transcript as a user message (after any tool_results, so tool-call/result
    # pairing is intact), and keeps the SAME turn alive so the model picks it up and responds —
    # rather than the message being dropped with a "please wait". RUNTIME-ONLY (not persisted) —
    # the loop appends the drained text to session.messages, which IS persisted. Mechanism only.
    steer_messages: list[str] = field(default_factory=list, repr=False)

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

    def catalog_for_policy(self) -> dict[str, list[str]]:
        return catalog_for_policy(self.catalog())

    async def run_readonly(
        self, argv: list[str], *, timeout: float | None = 20.0, quiet: bool = False,
        cwd: str | Path | None = None,
    ) -> RunResult:
        """Validate + run a command that MUST be read-only. Raises if the policy
        would not classify it read-only (these are trusted probes, but we still gate).
        The policy's ``timeout_s`` (if declared) supersedes ``timeout``; probes default to
        a short 20s bound when the policy declares none.

        ``quiet=True`` skips ONLY the ``command`` event emit — every gate (policy, read-only
        classification) still applies and the command still runs/records metrics. Used by
        the live resource poller so its 5s ``kubectl top`` polls don't flood the persisted,
        500-capped command trail with hundreds of identical rows.

        ``cwd`` narrows where a read-only probe runs (e.g. a ``git rev-parse`` against a specific
        read-only repo for provenance capture). It cannot widen capability — the policy /
        read-only gates still apply and an entry's own pinned ``cwd_must_be`` wins.

        Mechanism lives in app/tools/command_exec.py (CommandExecutor); this thin delegator keeps
        the execution engine separable from this dependency-injection container."""
        from app.tools.command_exec import CommandExecutor
        return await CommandExecutor(self).run_readonly(argv, timeout=timeout, quiet=quiet, cwd=cwd)

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
        owns where each line goes, while still passing through the SAME policy/runner path).
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
