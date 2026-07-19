"""THE agent engine: the Claude Agent SDK/CLI runs the agentic loop natively.

Design contract: docs/reference/SDK_NATIVE_ENGINE.md. Per user turn: build
ClaudeAgentOptions → connect a ClaudeSDKClient (connect-per-turn,
``resume=session.sdk_session_id``; a failed resume falls back once to a fresh SDK session
seeded from the ``session.messages`` mirror) → send the one-shot catalog/env preamble +
user text → consume the stream, translating it to the existing WS events and mirroring into
``session.messages`` (today's shapes) → account usage → disconnect. Tool execution happens
inside the SDK via the in-process ``benchtools`` MCP server (app/tools/mcp_server.py);
``can_use_tool`` is a thin gatekeeper that allows only ``mcp__benchtools__*`` and hands each
call's ``tool_use_id`` to the wrapper.

Steer (spike verdict: mid-turn ``client.query()`` is silently dropped by CLI 2.1.209): user
messages typed while the turn runs are queued app-side — :func:`steer` / the legacy
``ctx.steer_messages`` path — and drained as an immediate follow-up ``query()`` on the same
connected client after the current ResultMessage, so the same app-level turn answers them.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.agent import events
from app.agent.prompt import build_system_prompt, catalog_brief_message
from app.agent.session import Session
from app.llm.sdk_options import (
    effort_option,
    render_assistant_text,
    render_tool_results,
    thinking_options,
)
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

# Sentinel for a cleanly exhausted SDK stream (the watchdog-aware iteration below).
_STREAM_END = object()


class StreamStalledError(RuntimeError):
    """The SDK stream went silent past the watchdog deadline with no tool running — the CLI
    subprocess is wedged. The turn was interrupted; run_turn surfaces this as an ERROR event."""


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
    # Any stream message arrived — distinguishes a dead-on-arrival connection (a resume id
    # whose CLI transcript is gone → fresh-session fallback) from a turn that died mid-flight.
    saw_activity: bool = False
    # >0 while a benchtools handler is executing (mcp_server.execute_tool). The stream is
    # LEGITIMATELY silent then — a long benchmark command, or an approval gate parked for
    # hours — so the stream watchdog must not count that silence as a wedged CLI.
    tool_depth: int = 0
    _steers: list[str] = field(default_factory=list)
    # bare tool name -> queue of tool_use ids stashed by can_use_tool, popped by the wrapper.
    _pending_ids: dict[str, deque[str]] = field(default_factory=dict)
    # tool_use_id -> (name, full result) recorded by the wrapper for the messages mirror.
    _results: dict[str, tuple[str, Any]] = field(default_factory=dict)
    # tool_use_id -> bare name, from the assistant mirror — names denied/unknown tool rows.
    names_by_id: dict[str, str] = field(default_factory=dict)
    # Pulsed each time the consumer finishes processing an AssistantMessage (mirror + text
    # emit) — see wait_mirrored.
    _mirror_advanced: asyncio.Event = field(default_factory=asyncio.Event)

    def steer(self, text: str) -> None:
        self._steers.append(text)

    def drain_steers(self) -> list[str]:
        """All queued steers, in arrival order: the engine-side queue plus the legacy
        ``ctx.steer_messages`` list the WS handler falls back to. Clears both."""
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

    def note_mirrored(self) -> None:
        self._mirror_advanced.set()

    async def wait_mirrored(self, tool_use_id: str) -> None:
        """Block until the consumer has processed the AssistantMessage carrying this tool_use
        (mirror appended, ASSISTANT_TEXT emitted). The SDK dispatches a tool from the control
        stream concurrently with the app-level message consumer, so without this hold the
        TOOL_CALL event could hit the wire BEFORE the assistant text that introduces it —
        breaking the old loop's transcript order (text bubble, then tool row)."""
        while tool_use_id not in self.names_by_id:
            self._mirror_advanced.clear()
            await self._mirror_advanced.wait()

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
    turn is in flight (the caller falls back to ``ctx.steer_messages`` / a fresh turn)."""
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
    """THE agent engine at the main.py seam: the SDK/CLI drives model→tool→model natively,
    while this class bridges its stream onto the app's WS event contract."""

    def __init__(
        self,
        transport_factory: Callable[[], Any] | None = None,
        *,
        stream_watchdog_s: float | None = None,
        watchdog_poll_s: float = 5.0,
    ):
        # Hermetic-test seam: a factory returning a Transport (tests/_sdk_fake.py
        # FakeTransport) drives the SDK's real protocol machinery with no CLI subprocess.
        # None (production) lets the SDK spawn the ``claude`` CLI. Called once per connect,
        # so the resume-fallback's second client gets a fresh transport.
        self._transport_factory = transport_factory
        # Stream watchdog override (tests); None reads settings.agent_stream_watchdog_s.
        self._stream_watchdog_s = stream_watchdog_s
        self._watchdog_poll_s = watchdog_poll_s

    async def run_turn(
        self,
        session: Session,
        user_text: str,
        *,
        emit: EmitFn,
        request_approval: ApproveFn,
        should_continue: ContinueFn | None = None,
    ) -> None:
        from claude_agent_sdk import ClaudeSDKError

        ctx = session.ctx
        ctx.emit = emit
        ctx.request_approval = request_approval
        # Snapshot the pre-turn mirror length BEFORE this turn's appends: the resume-fallback
        # replay must seed the fresh SDK session with the PRIOR transcript only (this turn's
        # preamble + user text ride in the query itself).
        prior_mirror = len(session.messages)
        query_text = self._first_query(session, user_text)
        # Surface a brand-new chat in the sidebar NOW (same early-persist contract as the old
        # loop): persist once and ping the UI to refetch; the end-of-turn persist records the
        # full transcript.
        session.persist()
        await emit(events.SESSION_SAVED, {})
        log.info("turn.start", extra={"session_id": session.id, "user_chars": len(user_text)})

        turn = LiveTurn(session=session, emit=emit)
        LIVE_TURNS[session.id] = turn
        usage = _TurnUsage()
        try:
            try:
                await self._drive_turn(session, turn, usage, query_text, should_continue,
                                       resume=session.sdk_session_id)
            except ClaudeSDKError as exc:
                # Resume fallback (D6): a resume id whose CLI transcript was GC'd/corrupted
                # makes the CLI die before emitting anything (surfaced as CLIConnectionError /
                # ProcessError — both ClaudeSDKError). Retry ONCE on a fresh SDK session seeded
                # from the mirror; the user sees a normal turn, not an error. A failure that
                # arrived mid-stream is a real error and propagates.
                if not session.sdk_session_id or turn.saw_activity:
                    raise
                log.warning("turn.resume_failed", extra={
                    "session_id": session.id, "sdk_session_id": session.sdk_session_id,
                    "error": str(exc)})
                session.sdk_session_id = None
                seeded = _mirror_replay_text(session.messages[:prior_mirror]) + query_text
                await self._drive_turn(session, turn, usage, seeded, should_continue,
                                       resume=None)
        except StreamStalledError as exc:
            await emit(events.ERROR, {"message": str(exc)})
        except Exception as exc:  # noqa: BLE001 — a failed turn ends cleanly, like the old loop
            await emit(events.ERROR, {"message": f"LLM call failed: {exc}"})
        finally:
            if LIVE_TURNS.get(session.id) is turn:
                del LIVE_TURNS[session.id]
            # Steers still queued when the turn ends abnormally (an error result / an exception
            # before their drain point) return to the legacy list so main.py's finally backstop
            # applies the OLD semantics: follow-up turn on error, dropped on cancel.
            if turn._steers:
                session.ctx.steer_messages = turn._steers + session.ctx.steer_messages
                turn._steers = []

        log.info("turn.end", extra={"session_id": session.id})
        session.persist()
        await emit(events.DONE, {})

    @staticmethod
    def _first_query(session: Session, user_text: str) -> str:
        """Mirror this turn's user message — preceded, once per session, by the env-preprobe
        and live-catalog preamble blocks (same tags/content/flags as the old loop) — and
        return the wire text for ``query()``.

        The blocks are SEPARATE user messages in the mirror (today's render shapes: the
        synthetic flag / bracket-tag skip rules keep them out of bubbles and titles) but are
        COALESCED into one wire message: the CLI's streaming input silently drops all but the
        first of a same-role run, so one coalesced user turn is exactly what reaches the
        model."""
        parts: list[str] = []
        if session.env_snapshot is not None and not session.prewarmed:
            block = ("[environment pre-probe — read-only snapshot, already gathered for "
                     "you so you don't need to call probe_environment again this turn]\n"
                     # Same 4k bound as the old loop — the ONE surviving clamp: a real cluster's
                     # snapshot gets a VALID truncation envelope, never JSON sliced mid-structure.
                     + clamp_tool_result_content(session.env_snapshot, 4000))
            session.messages.append({"role": "user", "synthetic": True, "content": block})
            session.prewarmed = True
            parts.append(block)
        if not session.catalog_injected:
            catalog = catalog_brief_message(session.ctx)
            session.messages.append({"role": "user", "content": catalog})
            session.catalog_injected = True
            parts.append(catalog)
        session.messages.append({"role": "user", "content": user_text})
        parts.append(user_text)
        return "\n\n".join(parts)

    def _build_options(self, session: Session, turn: LiveTurn, resume: str | None) -> Any:
        from claude_agent_sdk import (
            ClaudeAgentOptions,
            PermissionResultAllow,
            PermissionResultDeny,
        )

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

        settings = session.ctx.settings
        # The per-session model/effort override (the UI picker) is read fresh on every connect:
        # connect-per-turn makes set_model unnecessary — a picker change lands on the next turn,
        # same semantics as the old loop's capture-once-per-turn.
        return ClaudeAgentOptions(
            model=session.model_override or settings.agent_sdk_model,
            system_prompt=build_system_prompt(session.ctx),
            mcp_servers={SERVER_NAME: build_benchtools_server(turn)},
            tools=[],                    # no built-in tools (Bash/Read/... stay ours)
            allowed_tools=[],            # nothing ever skips the can_use_tool gatekeeper
            can_use_tool=gatekeeper,
            permission_mode="default",
            setting_sources=[],          # never leak ~/.claude/CLAUDE.md or project settings
            resume=resume,
            max_turns=MAX_TURNS,
            include_partial_messages=True,
            cwd=session.ctx.workspace,   # stable per-session transcript home
            env=dict(_CLI_ENV),
            cli_path=settings.claude_cli_path or None,
            **thinking_options(settings.agent_sdk_thinking),
            **effort_option(session.effort_override or settings.agent_sdk_effort),
        )

    async def _drive_turn(
        self,
        session: Session,
        turn: LiveTurn,
        usage: _TurnUsage,
        query_text: str,
        should_continue: ContinueFn | None,
        *,
        resume: str | None,
    ) -> None:
        """Connect one client, send the query, and consume responses (plus any steer
        follow-ups) to completion. Always interrupts an unfinished stream and disconnects."""
        from claude_agent_sdk import ClaudeSDKClient

        transport = self._transport_factory() if self._transport_factory else None
        client = ClaudeSDKClient(options=self._build_options(session, turn, resume),
                                 transport=transport)
        finished = False
        try:
            await client.connect()
            turn.client = client
            await client.query(query_text)
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
        finally:
            turn.client = None
            if not finished:
                # The turn died mid-stream (error/cancel): interrupt so the CLI never leaves a
                # dangling in-flight request behind the disconnect.
                with contextlib.suppress(Exception):
                    await client.interrupt()
            with contextlib.suppress(Exception):
                await client.disconnect()

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
        # suggest_next_steps is TERMINAL (D5): once its chips are offered, any trailing model
        # text is exactly the "use the buttons below" closer the buttons replace — suppress it
        # (emit AND mirror) for the rest of this response. A steer follow-up is a fresh
        # response, so it answers un-suppressed (steer outranks the terminal offer).
        terminal = False

        stream = aiter(client.receive_response())
        while True:
            msg = await self._next_message(stream, turn, client)
            if msg is _STREAM_END:
                break
            turn.saw_activity = True
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
                    if delta.get("type") == "text_delta" and delta.get("text") and not terminal:
                        await emit(events.ASSISTANT_DELTA, {"text": delta["text"]})
                elif ev_type == "message_start":
                    usage.on_message_start((ev.get("message") or {}).get("usage") or {})
                elif ev_type == "message_delta":
                    usage.on_message_delta(ev.get("usage") or {})
            elif isinstance(msg, AssistantMessage):
                self._flush_tool_results(session, pending_results)
                text = "" if terminal else "".join(
                    b.text for b in msg.content if isinstance(b, TextBlock))
                calls: list[dict[str, Any]] = [
                    {"id": b.id, "name": _bare_name(b.name), "input": dict(b.input)}
                    for b in msg.content if isinstance(b, ToolUseBlock)
                ]
                for call in calls:
                    turn.names_by_id[call["id"]] = call["name"]
                if text or calls:
                    session.messages.append(
                        {"role": "assistant", "content": text, "tool_calls": calls})
                if text:
                    await emit(events.ASSISTANT_TEXT, {"text": text})
                # Release any tool call parked in wait_mirrored on this message's ids.
                turn.note_mirrored()
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
                    if name == "suggest_next_steps":
                        terminal = True
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

    async def _next_message(self, stream: Any, turn: LiveTurn, client: Any) -> Any:
        """Next SDK stream message, bounded by the stream watchdog. Silence past the deadline
        WITH NO TOOL RUNNING (``turn.tool_depth`` — a long command or an hours-parked approval
        gate lives inside a tool call and is exempt) means a wedged CLI: interrupt it, allow a
        short grace for the ResultMessage to flush, then raise :class:`StreamStalledError`.
        Returns ``_STREAM_END`` on clean exhaustion."""
        watchdog = (self._stream_watchdog_s if self._stream_watchdog_s is not None
                    else turn.session.ctx.settings.agent_stream_watchdog_s)
        task = asyncio.ensure_future(anext(stream))
        poll = min(self._watchdog_poll_s, watchdog) if watchdog > 0 else None
        silent = 0.0
        nudged = False
        while True:
            done, _pending = await asyncio.wait({task}, timeout=poll)
            if task in done:
                try:
                    return task.result()
                except StopAsyncIteration:
                    return _STREAM_END
            assert poll is not None  # timeout fired ⇒ the watchdog is enabled
            if turn.tool_depth:
                silent = 0.0
                continue
            silent += poll
            if not nudged and silent >= watchdog:
                nudged = True
                log.warning("turn.stream_stalled", extra={
                    "session_id": turn.session.id, "watchdog_s": watchdog})
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(client.interrupt(), timeout=2 * poll)
            elif nudged and silent >= watchdog + 2 * poll:
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
                raise StreamStalledError(
                    f"agent stream stalled: no output for {watchdog:g}s with no tool running; "
                    "interrupted the wedged turn")

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
        # enrichment: unavailable transports (the hermetic fake) and older CLIs simply omit
        # it; the UI chip degrades to context_window.
        with contextlib.suppress(Exception):
            ctx_usage = await client.get_context_usage()
            payload["context"] = {
                "total_tokens": ctx_usage.get("totalTokens"),
                "max_tokens": ctx_usage.get("maxTokens"),
                "percentage": ctx_usage.get("percentage"),
            }
        await turn.emit(events.USAGE, payload)


def _mirror_replay_text(messages: list[dict[str, Any]]) -> str:
    """One-time textual replay of the mirror for the resume-fallback's fresh SDK session —
    the same faithful narration shapes the old provider replayed history with. Empty when
    there is no prior transcript (brand-new chat)."""
    if not messages:
        return ""
    lines = ["[conversation replay — this chat's prior transcript, re-supplied because the "
             "assistant's session state was reset; continue the conversation naturally]"]
    for m in messages:
        role = m.get("role")
        if role == "user":
            lines.append("user: " + str(m.get("content") or ""))
        elif role == "assistant":
            lines.append("assistant: " + render_assistant_text(
                m.get("content") or "", m.get("tool_calls") or []))
        elif role == "tool_results":
            lines.append(render_tool_results(m.get("results") or []))
    return "\n\n".join(lines) + "\n\n"


def _bare_name(name: str) -> str:
    return name[len(TOOL_PREFIX):] if name.startswith(TOOL_PREFIX) else name


# ---- env-preamble clamp (the ONE surviving clamp) -------------------------------------------
# Everything else enters the model context whole (user decision D2: CLI auto-compaction is the
# bound), but the env pre-probe snapshot keeps its 4k bound: a real cluster's snapshot gets a
# VALID truncation envelope, never JSON sliced mid-structure. Relocated from the deleted
# context_mgmt module at the Phase 5 cutover.

_TRUNC_NOTE = (
    "tool result exceeded the feed-back budget and was truncated; the 'preview' field holds "
    "its leading portion. Re-run with a narrower query or request specific fields for the rest."
)

# A short scalar top-level field is kept verbatim in the envelope (it carries the error/status
# signal); anything longer is treated as bulk payload and appears only (clipped) in the preview.
_SIGNAL_STR_MAX = 500


def _is_signal_scalar(value: Any) -> bool:
    """True for small scalars worth preserving intact (bools, numbers, short strings, None)."""
    if value is None or isinstance(value, (bool, int, float)):
        return True
    return isinstance(value, str) and len(value) <= _SIGNAL_STR_MAX


def clamp_tool_result_content(result: Any, budget: int) -> str:
    """Serialize ``result`` to JSON of at most ``budget`` chars that is always valid JSON.

    Fast path: when the full serialization fits, it is returned unchanged (byte-identical to
    ``json.dumps(result)``). Otherwise a valid JSON truncation envelope is returned, sized to
    the budget, preserving small top-level signal fields and a clipped preview of the payload.
    """
    full = json.dumps(result)
    if len(full) <= budget:
        return full

    envelope: dict[str, Any] = {"_truncated": True, "_original_chars": len(full)}
    # Preserve small top-level signal fields so error / rejected markers survive intact.
    if isinstance(result, dict):
        for key, value in result.items():
            if isinstance(key, str) and key not in envelope and _is_signal_scalar(value):
                envelope[key] = value
    envelope["_note"] = _TRUNC_NOTE

    # Budget left for the preview after the envelope's own JSON overhead (keys, braces, the
    # "preview" key and its quoting). Reserve it by measuring the envelope with an empty preview.
    skeleton = dict(envelope)
    skeleton["preview"] = ""
    remaining = budget - len(json.dumps(skeleton))
    if remaining <= 0:
        # Even the signal-only envelope overflows the budget; fall back to the minimal one.
        minimal = {"_truncated": True, "_original_chars": len(full), "_note": _TRUNC_NOTE}
        return json.dumps(minimal)

    # JSON-escaping can expand the preview (a " becomes \", a newline becomes \n), so clip the
    # raw source first, then shrink until the *encoded* envelope fits the budget.
    preview = full[:remaining]
    while preview:
        envelope["preview"] = preview
        encoded = json.dumps(envelope)
        if len(encoded) <= budget:
            return encoded
        preview = preview[: -max(1, len(encoded) - budget)]

    envelope["preview"] = ""
    return json.dumps(envelope)
