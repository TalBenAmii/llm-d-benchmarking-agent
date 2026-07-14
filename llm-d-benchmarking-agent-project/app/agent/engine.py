"""SDK-native agent engine: the Claude Agent SDK/CLI runs the agentic loop.

Phase 1 of the SDK-native refactor (design contract: docs/reference/SDK_NATIVE_ENGINE.md).
Per user turn: build ClaudeAgentOptions → connect a ClaudeSDKClient (connect-per-turn,
``resume=session.sdk_session_id``) → send the user text → consume the stream, translating
it to the existing WS events and mirroring into ``session.messages`` (today's shapes) →
account usage → disconnect. Tool execution happens inside the SDK via the in-process
``benchtools`` MCP server (app/tools/mcp_server.py); ``can_use_tool`` is a thin gatekeeper
that allows only ``mcp__benchtools__*`` and hands each call's ``tool_use_id`` to the wrapper.

Selected at the main.py seam by the branch-only ``AGENT_ENGINE=sdk-native`` flag; the old
``AgentLoop`` stays the default until the Phase 5 cutover deletes it (and this flag).

Steer (spike verdict: mid-turn ``client.query()`` is silently dropped by CLI 2.1.209): user
messages typed while the turn runs are queued app-side — :func:`steer` / the legacy
``ctx.steer_messages`` path — and drained as an immediate follow-up ``query()`` on the same
connected client after the current ResultMessage, so the same app-level turn answers them.

Phase 2 seams (deliberately not built here): catalog/env preamble injection, resume-fallback
(fresh session seeded from the mirror), terminal ``suggest_next_steps`` text suppression,
USAGE payload redesign for the context chip.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.agent import events
from app.agent.prompt import build_system_prompt
from app.agent.session import Session
from app.llm.agent_sdk_provider import _effort_option, _thinking_options
from app.observability.logging import bind as log_bind
from app.tools.context import ApproveFn, EmitFn
from app.tools.mcp_server import SERVER_NAME, TOOL_PREFIX, build_benchtools_server

log = logging.getLogger("app.agent.engine")

# Replaces the old loop's MAX_STEPS=24 (observed quality damage: long flows paused mid-work).
# The bridge maps the CLI's ``error_max_turns`` to the same "step limit; pausing" ERROR text.
MAX_TURNS = 60
_STEP_LIMIT_MESSAGE = f"reached the step limit ({MAX_TURNS}); pausing."

# Child-CLI env: blank the API keys so a stray key can't bypass the logged-in subscription
# (an empty value reads as "unset"), and neutralize the MCP tool timeout so an approval gate
# parked inside a handler can hold the tool open indefinitely (spike V1: a 15-min park
# survives with this set).
_CLI_ENV = {
    "ANTHROPIC_API_KEY": "",
    "ANTHROPIC_AUTH_TOKEN": "",
    "MCP_TOOL_TIMEOUT": "86400000",
    "MCP_TIMEOUT": "86400000",
}

ContinueFn = Callable[[], bool]


@dataclass
class LiveTurn:
    """The live handle for one in-flight SDK-native turn.

    Registered in :data:`LIVE_TURNS` so the WS handler can steer/interrupt a running turn,
    and handed to the MCP wrapper as its binding to the per-turn context (session, emit,
    the pending ``tool_use_id`` from ``can_use_tool``, the serializing tool lock)."""

    session: Session
    emit: EmitFn
    # Serializes tool execution: ToolContext assumes sequential dispatch (single-slot
    # ``current_tool_call_id`` + approval bookkeeping).
    tool_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    client: Any = None
    # A compact_boundary SystemMessage was seen this turn (CLI auto-compacted the context).
    compacted: bool = False
    _steers: list[str] = field(default_factory=list)
    # bare tool name -> queue of tool_use ids stashed by can_use_tool, popped by the wrapper.
    _pending_ids: dict[str, deque[str]] = field(default_factory=dict)
    # tool_use_id -> (name, full result) recorded by the wrapper for the messages mirror.
    _results: dict[str, tuple[str, Any]] = field(default_factory=dict)
    # tool_use_id -> bare name, from the assistant mirror — names denied/unknown tool rows.
    names_by_id: dict[str, str] = field(default_factory=dict)

    def steer(self, text: str) -> None:
        self._steers.append(text)

    def drain_steers(self) -> list[str]:
        """All queued steers, in arrival order: the engine-side queue plus the legacy
        ``ctx.steer_messages`` list the WS handler still appends to. Clears both."""
        out = self._steers + self.session.ctx.steer_messages
        self._steers = []
        self.session.ctx.steer_messages = []
        return out

    def stash_tool_use(self, name: str, tool_use_id: str | None) -> None:
        self._pending_ids.setdefault(name, deque()).append(tool_use_id or "")

    def take_tool_use_id(self, name: str) -> str:
        pending = self._pending_ids.get(name)
        if not pending:
            raise RuntimeError(
                f"no pending tool_use id for {name!r} — can_use_tool never saw this call")
        return pending.popleft()

    def record_result(self, tool_use_id: str, name: str, result: Any) -> None:
        self._results[tool_use_id] = (name, result)

    def take_result(self, tool_use_id: str) -> tuple[str, Any] | None:
        return self._results.pop(tool_use_id, None)

    async def interrupt(self) -> None:
        if self.client is not None:
            await self.client.interrupt()


# session_id -> the in-flight turn, so the WS handler can steer/interrupt it.
LIVE_TURNS: dict[str, LiveTurn] = {}


def steer(session_id: str, text: str) -> bool:
    """Queue a mid-turn user message onto the session's live turn. Returns False when no
    turn is in flight (the caller starts a fresh turn instead)."""
    turn = LIVE_TURNS.get(session_id)
    if turn is None:
        return False
    turn.steer(text)
    return True


class _TurnUsage:
    """Running per-turn token accounting from the raw stream events.

    ``message_start`` carries the call's input/cache tokens (and opens a message whose
    output ticks up via cumulative ``message_delta`` counts); the latest message_start's
    totals are the CURRENT context-window occupancy, same semantics as the old loop."""

    def __init__(self) -> None:
        self.input = 0
        self.output = 0
        self.cache_read = 0
        self.cache_write = 0
        self.calls = 0
        self.context_window: dict[str, int] = {}
        self._msg_output = 0

    def on_message_start(self, usage: dict[str, Any]) -> None:
        self.flush_output()
        self.calls += 1
        inp = int(usage.get("input_tokens", 0) or 0)
        cr = int(usage.get("cache_read_input_tokens", 0) or 0)
        cw = int(usage.get("cache_creation_input_tokens", 0) or 0)
        self.input += inp
        self.cache_read += cr
        self.cache_write += cw
        self._msg_output = int(usage.get("output_tokens", 0) or 0)
        self.context_window = {
            "tokens": inp + cr + cw, "input": inp, "cache_read": cr, "cache_write": cw,
        }

    def on_message_delta(self, usage: dict[str, Any]) -> None:
        # message_delta reports the CUMULATIVE output tokens of the current message.
        out = int(usage.get("output_tokens", 0) or 0)
        if out:
            self._msg_output = out

    def flush_output(self) -> None:
        self.output += self._msg_output
        self._msg_output = 0


class SdkNativeEngine:
    """Drop-in replacement for ``AgentLoop`` at the main.py seam: same ``run_turn``
    signature, but the SDK/CLI drives model→tool→model natively."""

    def __init__(self, transport_factory: Callable[[], Any] | None = None):
        # Hermetic-test seam: a factory returning a Transport (tests/_sdk_fake.py
        # FakeTransport) drives the SDK's real protocol machinery with no CLI subprocess.
        # None (production) lets the SDK spawn the ``claude`` CLI.
        self._transport_factory = transport_factory

    async def run_turn(
        self,
        session: Session,
        user_text: str,
        *,
        emit: EmitFn,
        request_approval: ApproveFn,
        should_continue: ContinueFn | None = None,
    ) -> None:
        from claude_agent_sdk import (
            ClaudeAgentOptions,
            ClaudeSDKClient,
            PermissionResultAllow,
            PermissionResultDeny,
        )

        ctx = session.ctx
        ctx.emit = emit
        ctx.request_approval = request_approval
        session.messages.append({"role": "user", "content": user_text})
        # Surface a brand-new chat in the sidebar NOW (same early-persist contract as the old
        # loop): persist once and ping the UI to refetch; the end-of-turn persist records the
        # full transcript.
        session.persist()
        await emit(events.SESSION_SAVED, {})
        log.info("turn.start", extra={"session_id": session.id, "user_chars": len(user_text)})

        turn = LiveTurn(session=session, emit=emit)
        LIVE_TURNS[session.id] = turn

        async def gatekeeper(tool_name: str, input_data: dict[str, Any], context: Any) -> Any:
            # Defense in depth: only our benchtools MCP tools may run — approval gates and the
            # command policy live INSIDE the handlers; this callback never replaces them. The
            # allow hands the call's tool_use_id to the wrapper (verified present, spike V3).
            if tool_name.startswith(TOOL_PREFIX):
                turn.stash_tool_use(
                    tool_name[len(TOOL_PREFIX):], getattr(context, "tool_use_id", None))
                return PermissionResultAllow()
            return PermissionResultDeny(
                message=f"only {SERVER_NAME} tools are available to this agent", interrupt=False)

        settings = ctx.settings
        options = ClaudeAgentOptions(
            model=session.model_override or settings.agent_sdk_model,
            system_prompt=build_system_prompt(ctx),
            mcp_servers={SERVER_NAME: build_benchtools_server(turn)},
            tools=[],                    # no built-in tools (Bash/Read/... stay ours)
            allowed_tools=[],            # nothing ever skips the can_use_tool gatekeeper
            can_use_tool=gatekeeper,
            permission_mode="default",
            setting_sources=[],          # never leak ~/.claude/CLAUDE.md or project settings
            resume=session.sdk_session_id,
            max_turns=MAX_TURNS,
            include_partial_messages=True,
            cwd=ctx.workspace,           # stable per-session transcript home
            env=dict(_CLI_ENV),
            cli_path=settings.claude_cli_path or None,
            **_thinking_options(settings.agent_sdk_thinking),
            **_effort_option(session.effort_override or settings.agent_sdk_effort),
        )
        transport = self._transport_factory() if self._transport_factory else None
        client = ClaudeSDKClient(options=options, transport=transport)
        usage = _TurnUsage()
        finished = False
        try:
            await client.connect()
            turn.client = client
            await client.query(user_text)
            while True:
                ok = await self._consume_response(client, turn, usage, should_continue)
                # Queued steers keep the SAME app-level turn alive: send them as an immediate
                # follow-up query on the same connected client (mid-turn query() is silently
                # dropped by the CLI, so this post-result drain is the delivery point).
                steers = turn.drain_steers() if ok else []
                if not steers:
                    break
                followup = "\n\n".join(steers)
                session.messages.append({"role": "user", "content": followup})
                await client.query(followup)
            finished = True
        except Exception as exc:  # noqa: BLE001 — a failed turn ends cleanly, like the old loop
            await emit(events.ERROR, {"message": f"LLM call failed: {exc}"})
        finally:
            if LIVE_TURNS.get(session.id) is turn:
                del LIVE_TURNS[session.id]
            turn.client = None
            if not finished:
                # The turn died mid-stream (error/cancel): interrupt so the CLI never leaves a
                # dangling in-flight request behind the disconnect.
                with contextlib.suppress(Exception):
                    await client.interrupt()
            with contextlib.suppress(Exception):
                await client.disconnect()

        log.info("turn.end", extra={"session_id": session.id})
        session.persist()
        await emit(events.DONE, {})

    async def _consume_response(
        self,
        client: Any,
        turn: LiveTurn,
        usage: _TurnUsage,
        should_continue: ContinueFn | None,
    ) -> bool:
        """Consume one SDK response stream (through its ResultMessage), translating to WS
        events and mirroring into ``session.messages``. Returns True when the turn ended
        cleanly (a follow-up steer query may then be sent), False on an error result."""
        from claude_agent_sdk import (
            AssistantMessage,
            ResultMessage,
            StreamEvent,
            SystemMessage,
            TextBlock,
            ToolResultBlock,
            ToolUseBlock,
            UserMessage,
        )

        session, emit = turn.session, turn.emit
        # tool_result mirror rows buffered until the step ends, so one assistant step's
        # results land as ONE ``tool_results`` message — today's shape.
        pending_results: list[dict[str, Any]] = []
        result_msg = None
        interrupted = False

        async for msg in client.receive_response():
            # Abandoned-turn guard, honored at stream-message boundaries (never mid-tool):
            # no recipient and no reason to keep running → interrupt; the stream still plays
            # out to its ResultMessage below.
            if not interrupted and should_continue is not None and not should_continue():
                log.info("turn.abandoned", extra={"session_id": session.id})
                interrupted = True
                with contextlib.suppress(Exception):
                    await client.interrupt()
            if isinstance(msg, StreamEvent):
                ev = msg.event or {}
                ev_type = ev.get("type")
                if ev_type == "content_block_delta":
                    delta = ev.get("delta") or {}
                    # thinking_delta is deliberately dropped — reasoning never reaches the client.
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        await emit(events.ASSISTANT_DELTA, {"text": delta["text"]})
                elif ev_type == "message_start":
                    usage.on_message_start((ev.get("message") or {}).get("usage") or {})
                elif ev_type == "message_delta":
                    usage.on_message_delta(ev.get("usage") or {})
            elif isinstance(msg, AssistantMessage):
                self._flush_tool_results(session, pending_results)
                text = "".join(b.text for b in msg.content if isinstance(b, TextBlock))
                calls: list[dict[str, Any]] = [
                    {"id": b.id, "name": _bare_name(b.name), "input": dict(b.input)}
                    for b in msg.content if isinstance(b, ToolUseBlock)
                ]
                for call in calls:
                    turn.names_by_id[call["id"]] = call["name"]
                session.messages.append(
                    {"role": "assistant", "content": text or "", "tool_calls": calls})
                if text:
                    await emit(events.ASSISTANT_TEXT, {"text": text})
            elif isinstance(msg, UserMessage):
                content = msg.content if isinstance(msg.content, list) else []
                for block in content:
                    if not isinstance(block, ToolResultBlock):
                        continue
                    # Mirror the FULL result the wrapper recorded; a call that never reached the
                    # wrapper (gatekeeper deny) mirrors the SDK's error content instead.
                    recorded = turn.take_result(block.tool_use_id)
                    name, result = recorded if recorded is not None else (
                        turn.names_by_id.get(block.tool_use_id), block.content)
                    pending_results.append(
                        {"tool_call_id": block.tool_use_id, "name": name, "content": result})
            elif isinstance(msg, SystemMessage):
                if msg.subtype == "compact_boundary":
                    turn.compacted = True
            elif isinstance(msg, ResultMessage):
                result_msg = msg

        self._flush_tool_results(session, pending_results)
        usage.flush_output()
        if result_msg is None:
            await emit(events.ERROR, {"message": "agent turn ended without a result"})
            return False

        if result_msg.session_id:
            # Persisted so the next turn resumes this CLI conversation (id is stable across
            # resume — spike V4).
            session.sdk_session_id = result_msg.session_id
        await self._emit_usage(client, turn, usage, result_msg.usage or {})

        if result_msg.subtype == "error_max_turns":
            await emit(events.ERROR, {"message": _STEP_LIMIT_MESSAGE})
            log.warning("turn.step_limit", extra={"max_turns": MAX_TURNS})
            return False
        if result_msg.is_error:
            await emit(events.ERROR, {"message": f"agent turn failed: {result_msg.subtype}"})
            return False
        return not interrupted

    @staticmethod
    def _flush_tool_results(session: Session, pending: list[dict[str, Any]]) -> None:
        if pending:
            session.messages.append({"role": "tool_results", "results": list(pending)})
            pending.clear()

    @staticmethod
    async def _emit_usage(
        client: Any, turn: LiveTurn, usage: _TurnUsage, result_usage: dict[str, Any],
    ) -> None:
        """Account the ResultMessage's authoritative totals onto the session and emit one
        USAGE event for the turn (running totals across steer follow-ups)."""
        session = turn.session
        session.total_input_tokens += int(result_usage.get("input_tokens", 0) or 0)
        session.total_output_tokens += int(result_usage.get("output_tokens", 0) or 0)
        session.total_cache_read_tokens += int(result_usage.get("cache_read_input_tokens", 0) or 0)
        session.total_cache_write_tokens += int(
            result_usage.get("cache_creation_input_tokens", 0) or 0)
        if usage.context_window:
            session.last_context_tokens = usage.context_window.get("tokens", 0)
        payload: dict[str, Any] = {
            "turn": {
                "input": usage.input, "output": usage.output,
                "cache_read": usage.cache_read, "cache_write": usage.cache_write,
                "calls": usage.calls,
                "total": usage.input + usage.cache_read + usage.cache_write + usage.output,
            },
            "session": {
                "input": session.total_input_tokens,
                "output": session.total_output_tokens,
                "cache_read": session.total_cache_read_tokens,
                "total": session.session_total,
            },
            "context_window": usage.context_window,
        }
        if turn.compacted:
            payload["compacted"] = True
        # Real CLI-side context occupancy (replaces the old char/4 estimator). Optional
        # enrichment: unavailable transports (the hermetic fake) and older CLIs simply omit it.
        with contextlib.suppress(Exception):
            ctx_usage = await client.get_context_usage()
            payload["context"] = {
                "total_tokens": ctx_usage.get("totalTokens"),
                "max_tokens": ctx_usage.get("maxTokens"),
                "percentage": ctx_usage.get("percentage"),
            }
        await turn.emit(events.USAGE, payload)


def _bare_name(name: str) -> str:
    return name[len(TOOL_PREFIX):] if name.startswith(TOOL_PREFIX) else name
