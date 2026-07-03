"""The command-execution engine for :class:`~app.tools.context.ToolContext`.

ToolContext is the dependency-injection hub every tool receives (settings, the security
allowlist, the command runner, the per-session workspace + quota counter, and the emit /
approval callbacks the agent loop wires in per dispatch). The logic that actually VALIDATES a
command against the allowlist, GATES it (approval when mutating), runs + times it, enforces the
usage quota, announces it to the UI, and records its metric/log used to live on ToolContext
itself — mixing a state container with an execution engine.

This module holds that execution concern as a :class:`CommandExecutor` collaborator. ToolContext
keeps thin ``run_readonly`` / ``run_command`` delegators, so every existing
``ctx.run_command(...)`` / ``ctx.run_readonly(...)`` call site is unchanged. The executor reads
its dependencies LIVE off the owning context (``emit`` / ``request_approval`` / ``run_semaphore``
are wired by the agent loop AFTER the context is built), so it holds the context rather than a
snapshot of its fields.
"""
from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from app.observability import metrics as instrument
from app.security.allowlist import MUTATING, READ_ONLY, Decision
from app.security.quota import QuotaExceeded
from app.security.runner import RunResult, simulated_run_result
from app.tools import gated_access
from app.tools.context import ApprovalRejected, QuotaError, ToolError

if TYPE_CHECKING:
    from app.tools.context import ToolContext

# Log under the ToolContext channel (not this module's name) so the executed-command record
# stays on its established "app.tools.context" logger — ops log filters and the corr_id
# acceptance test key on that channel; the execution engine moving modules must not move it.
log = logging.getLogger("app.tools.context")


class CommandExecutor:
    """Validates, gates, runs, times, quota-counts, announces, and records commands on behalf
    of a :class:`~app.tools.context.ToolContext`. Stateless apart from a back-reference to the
    owning context, whose wired callbacks/fields it reads live at call time."""

    def __init__(self, ctx: ToolContext) -> None:
        self._ctx = ctx

    async def _emit_line(self, line: str) -> None:
        ctx = self._ctx
        if ctx.emit is not None:
            await ctx.emit("output", {"line": line})

    async def _emit_command(self, decision: Decision, *, auto_run: bool) -> None:
        """Announce a command the instant before it runs — for EVERY execution, not just
        the approval-gated ones, so the UI can show the full executed-command trail inline
        in the chat (the debug view). ``auto_run`` is True for read-only commands that ran
        without a prompt. ``tool_call_id`` ties the command to the tool call that issued it
        (None for the pre-turn environment probe), so a resumed chat replays each command
        inline in its original transcript position — right after its tool call."""
        ctx = self._ctx
        if ctx.emit is not None:
            await ctx.emit("command", {
                "argv": list(decision.argv),
                "text": " ".join(decision.argv),
                "mode": decision.mode,
                "auto_run": auto_run,
                # True only for a command that was a SIMULATED no-op (mutating-under-SIMULATE). A
                # read-only command runs for real even under SIMULATE, so it is NOT flagged — the
                # UI must not badge a genuinely-executed probe/grep as "SIMULATED".
                "simulated": ctx.settings.simulate and decision.mode == MUTATING,
                "tool_call_id": ctx.current_tool_call_id,
            })

    def _record_metric(self, decision: Decision, *, auto_run: bool, result: RunResult) -> None:
        """File the executed-command fact into the metrics registry AND the structured log.
        Best-effort: observability must never break command execution, so any error here is
        swallowed. ``exe`` is argv[0] only (bounded cardinality — never the full argv, which
        would explode the metric label space and bloat the log line). The log line carries
        mode + exe + duration + exit code (per Phase 11) and — via the contextvars filter —
        the turn's corr_id/session_id/tool automatically."""
        exe = decision.argv[0] if decision.argv else ""
        with contextlib.suppress(Exception):  # metrics must not affect the run
            instrument.record_command(
                exe=exe,
                mode=decision.mode,
                auto_run=auto_run,
                duration_s=result.duration_s,
            )
        with contextlib.suppress(Exception):  # logging must not affect the run
            log.info("command.exec", extra={
                "exe": exe,
                "mode": decision.mode,
                "auto_run": auto_run,
                "duration_s": result.duration_s,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
            })

    def _enforce_quota(self, decision: Decision) -> None:
        """Refuse, BEFORE execution, if this command would exceed its allowlist-declared
        usage quota. The caps are DATA (on the Decision, sourced from the YAML); the
        counting is mechanism (the per-session QuotaCounter). No per-command Python."""
        ctx = self._ctx
        if decision.quota_key is None:
            return  # no quota declared in the policy for this command
        try:
            ctx.quota.check(
                decision.quota_key,
                per_session=decision.quota_per_session,
                per_day=decision.quota_per_day,
            )
        except QuotaExceeded as exc:
            raise QuotaError(exc) from exc

    @staticmethod
    def _effective_timeout(decision: Decision, fallback: float | None) -> float | None:
        """The deadline for this command. The policy's ``timeout_s`` (DATA) wins when the
        command declares one; otherwise the caller's ``fallback`` applies (e.g. a probe's
        short 20s bound, or the per-subcommand budget the run tools pass). When neither is
        set, return None so the runner applies its own sane global default."""
        if decision.timeout_s is not None:
            return float(decision.timeout_s)
        return fallback

    async def run_readonly(
        self, argv: list[str], *, timeout: float | None = 20.0, quiet: bool = False,
        cwd: str | Path | None = None,
    ) -> RunResult:
        """Validate + run a command that MUST be read-only (see ToolContext.run_readonly for
        the public contract). Raises if the allowlist would not classify it read-only.

        ``cwd`` overrides the resolved working directory for the read-only probe (e.g. a
        ``git rev-parse`` against a specific repo for provenance capture). It can ONLY narrow
        where the probe reads — every gate (allowlist, read-only classification, quota) still
        applies — and an allowlist entry's own pinned ``cwd_must_be`` still takes precedence."""
        ctx = self._ctx
        decision = ctx.allowlist.validate(argv, catalog=ctx.catalog_for_allowlist())
        if not decision.allowed:
            raise ToolError(f"probe command denied by allowlist: {decision.reason}")
        if decision.mode != READ_ONLY:
            raise ToolError(f"probe command is not read-only: {' '.join(argv)}")
        self._enforce_quota(decision)  # pre-exec refusal (data-driven cap, counter mechanism)
        entry = ctx.allowlist.executable(argv[0])
        if not quiet:
            await self._emit_command(decision, auto_run=True)
        # An entry that pins its own cwd (cwd_must_be) wins; otherwise honor the caller's cwd.
        cwd_arg = None if (entry and entry.get("cwd_must_be")) else cwd
        result = await ctx.runner.execute(
            argv, entry, timeout=self._effective_timeout(decision, timeout), cwd=cwd_arg
        )
        if decision.quota_key is not None:
            ctx.quota.record(decision.quota_key)
        self._record_metric(decision, auto_run=True, result=result)
        return result

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
        """Validate, gate (approval if mutating), then run a command (see
        ToolContext.run_command for the public contract — streaming, ``on_line``, ``env``)."""
        ctx = self._ctx
        decision = ctx.allowlist.validate(argv, catalog=ctx.catalog_for_allowlist())
        if not decision.allowed:
            raise ToolError(f"command denied by allowlist: {decision.reason}")
        # Gated-model access guardrail (a SAFETY gate, like the approval gate below): refuse to
        # stand up / run / smoketest a model the backend HF token can't pull, once check_capacity
        # has reported it gated+unauthorized. Mechanism enforcing a stated boundary on the
        # bridge's own facts — see app/tools/gated_access.py. Fires before quota/approval so a
        # blocked deploy never counts usage or prompts; only deploy subcommands are affected.
        if decision.mode == MUTATING:
            block = gated_access.gated_block(ctx, decision.argv)
            if block is not None:
                raise ToolError(gated_access.gated_block_message(*block))
        # Quota refusal happens BEFORE the approval prompt and before any execution, so an
        # over-quota command never even asks the user. Cap = DATA; counter = mechanism.
        self._enforce_quota(decision)
        # SIMULATE: a MUTATING command must not actually run. When the wired runner spawns real
        # subprocesses (production), pre-empt it with a synthetic no-op — ANNOUNCED (so the UI's
        # command trail shows exactly what WOULD run) but never executed. READ-ONLY commands are
        # NOT pre-empted: they fall through and run for real, so the agent still gathers genuine
        # context under SIMULATE. (No-op test fakes set runs_real_subprocess=False and are called
        # unchanged — they already make every command safe and record it for assertions.)
        if ctx.settings.simulate and decision.mode == MUTATING and ctx.runner.runs_real_subprocess:
            await self._emit_command(decision, auto_run=False)
            result = simulated_run_result(
                decision.argv, timeout=self._effective_timeout(decision, timeout)
            )
            if decision.quota_key is not None:
                ctx.quota.record(decision.quota_key)
            self._record_metric(decision, auto_run=False, result=result)
            return result
        # Mutating commands need approval BEFORE running — but NOT in simulate (a simulated
        # mutation never executes, so prompting would only stall the dry-run walk).
        if decision.requires_approval and not ctx.settings.simulate:
            if ctx.request_approval is None:
                raise ToolError("approval required but no approver is wired")
            payload = {"command": " ".join(decision.argv), "argv": decision.argv, "mode": decision.mode}
            if not await ctx.request_approval("command", payload):
                raise ApprovalRejected(argv)
        entry = ctx.allowlist.executable(argv[0])
        # Announce the command for the full executed-command trail / debug view. For a
        # mutating command this fires only after approval, so it records what truly ran.
        await self._emit_command(decision, auto_run=not decision.requires_approval)
        if on_line is None:
            on_line = self._emit_line if (stream and ctx.emit is not None) else None
        auto_run = not decision.requires_approval
        deadline = self._effective_timeout(decision, timeout)
        # Bound concurrent heavy runs across sessions (read-only commands run uncapped).
        if ctx.run_semaphore is not None and decision.mode == MUTATING:
            async with ctx.run_semaphore:
                result = await ctx.runner.execute(
                    argv, entry, on_line=on_line, timeout=deadline, cwd=cwd, extra_env=env
                )
        else:
            result = await ctx.runner.execute(
                argv, entry, on_line=on_line, timeout=deadline, cwd=cwd, extra_env=env
            )
        # Tally the use only after it actually ran (an approved, executed command).
        if decision.quota_key is not None:
            ctx.quota.record(decision.quota_key)
        self._record_metric(decision, auto_run=auto_run, result=result)
        return result
