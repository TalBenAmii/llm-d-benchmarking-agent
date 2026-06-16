"""Claude Agent SDK provider — runs inference on the user's Claude subscription (e.g. a Max
plan) through the locally-installed ``claude`` CLI, with ALL of the SDK's built-in tools
disabled.

Design (verified against claude-agent-sdk 0.2.x): the host app keeps its OWN agent loop and
its OWN approval-gated tools. This provider only turns one ``(system, messages, tools)`` into
ONE :class:`AssistantTurn`. To do that without letting the SDK *run* anything:

* The app's tools are exposed to the model as in-process **MCP** tools (so the model can call
  them with native tool-calling), but a ``can_use_tool`` callback **denies every call** — the
  SDK never executes a handler. We read the emitted ``tool_use`` blocks off the assistant
  message and hand them back to the loop, which runs them under the app's own
  allowlist/approval gating.
* ``max_turns=1`` stops after exactly one assistant turn. A text-only turn ends cleanly; a
  tool-calling turn raises a terminal ``error_max_turns`` AFTER delivering the assistant
  message — that is the EXPECTED stop, swallowed below.
* All built-in tools are removed (``tools=[]``) and ``setting_sources=[]`` ignores the user's
  ``~/.claude/CLAUDE.md`` and project settings, so only the app's own system prompt is used.

Conversation history is replayed as plain **user/assistant text** turns. The CLI's streaming
input rejects synthetic ``tool_use``/``tool_result`` blocks (HTTP 400), so prior tool calls and
their results are rendered into text; only NEW calls use structured tool-calling. The text is
wrapped in a single ``{"type": "text"}`` block (NOT a bare string): the CLI scans every input
message's ``content`` with ``.some(...)`` to find ``tool_use`` blocks, which throws if
``content`` is a string — so each replayed turn MUST carry a list of content blocks.

Auth: the SDK uses the ``claude`` CLI's logged-in subscription credentials — NO
``ANTHROPIC_API_KEY`` is needed. If one happens to be present in the environment it would
override the subscription and force per-token API billing, so we blank it for the child process.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any

from app.config import Settings
from app.llm.provider import AssistantTurn, LLMProvider, OnText, ProviderError, ToolCall, Usage

# Single in-process MCP server that carries the app's tools. The model sees each tool as
# ``mcp__{_SERVER_NAME}__{tool_name}``; we strip that prefix on the way back out.
_SERVER_NAME = "benchtools"
_TOOL_PREFIX = f"mcp__{_SERVER_NAME}__"

# Result subtypes that are NOT real failures: a clean finish, or our deliberate one-turn cap.
_OK_RESULT_SUBTYPES = frozenset({"success", "error_max_turns"})

# Connection prewarm (latency #1b). Connecting a ``claude`` CLI subprocess costs ~0.5s and sits
# on the critical path before the FIRST token of every turn. After a turn finishes we eagerly
# connect ONE spare client in the background (while the user reads the answer) so the NEXT turn
# adopts an already-connected client instead of paying that ~0.5s itself. A prewarmed connection
# older than this TTL is discarded and reconnected fresh, bounding the risk of adopting a CLI
# subprocess that has since died idle. Single global slot => at most one spare subprocess.
_PREWARM_TTL_S = 120.0

# Blank these for the spawned CLI so a stray key can't bypass the subscription. An empty value
# is treated as "unset" by the CLI (it falls back to the logged-in subscription); we cannot
# DELETE an inherited var via options.env, only override it.
_NEUTRALIZE_ENV = {"ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": ""}

# Shown to the model (via can_use_tool deny) when it calls a tool. The HOST app runs every tool
# under its own allowlist/approval gating, so the SDK must never execute one. On the persistent
# per-turn client (_AgentSdkTurn) this denial stays in the CLI's conversation state and is
# replayed to the model on the next step, so it must clearly tell the model that the REAL result
# arrives as the following user message — otherwise the model could read "denied" as a failure.
_DENY_MESSAGE = (
    "Tool execution is handled by the host application; the tool's result will be delivered to "
    "you in the FOLLOWING user message. Treat that user message as the tool's output."
)


# Effort levels the SDK/CLI accepts. Anything else falls back to None (the CLI's own default),
# so a typo can never crash a turn — it just declines to override the effort.
_EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})


def _thinking_options(thinking: str) -> dict[str, Any]:
    """Translate the ``AGENT_SDK_THINKING`` setting into ClaudeAgentOptions thinking kwargs.

    ``"adaptive"`` → ``thinking={"type": "adaptive"}`` (Claude decides depth — what Sonnet 4.6
    in Claude Code does); a positive integer string → ``thinking={"type": "enabled",
    "budget_tokens": N}`` (a fixed per-turn budget that forces thinking every turn);
    ``"off"``/``"disabled"``/``"none"``/``"0"`` (or anything unrecognized) → ``{}`` (no extended
    thinking — nothing for the loop to capture). Returned as kwargs so an empty dict cleanly
    means "don't set the option at all"."""
    value = (thinking or "").strip().lower()
    if value == "adaptive":
        return {"thinking": {"type": "adaptive"}}
    if value.isdigit() and int(value) > 0:
        return {"thinking": {"type": "enabled", "budget_tokens": int(value)}}
    return {}


def _effort_option(effort: str) -> dict[str, Any]:
    """Translate ``AGENT_SDK_EFFORT`` into an ``effort`` kwarg, or ``{}`` for an unknown value
    (so the CLI keeps its own default rather than erroring)."""
    value = (effort or "").strip().lower()
    return {"effort": value} if value in _EFFORT_LEVELS else {}


def _strip_prefix(name: str) -> str:
    return name[len(_TOOL_PREFIX):] if name.startswith(_TOOL_PREFIX) else name


def _usage_from(usage: dict[str, Any] | None) -> Usage:
    """Normalize the SDK's usage dict to the cross-provider :class:`Usage` contract. Like
    Anthropic's API, ``input_tokens`` already EXCLUDES the cached read/write portions."""
    if not usage:
        return Usage()
    return Usage(
        input_tokens=int(usage.get("input_tokens", 0) or 0),
        output_tokens=int(usage.get("output_tokens", 0) or 0),
        cache_read_tokens=int(usage.get("cache_read_input_tokens", 0) or 0),
        cache_write_tokens=int(usage.get("cache_creation_input_tokens", 0) or 0),
    )


def _render_assistant_text(text: str, tool_calls: list[dict[str, Any]]) -> str:
    """Render a prior assistant turn (its text + the tool calls it made) as plain text. The
    SDK won't accept replayed ``tool_use`` blocks, so the model re-reads its own past actions
    as a short, faithful narration."""
    parts: list[str] = []
    if text:
        parts.append(text)
    for tc in tool_calls:
        parts.append(f"[called tool {tc['name']} with {json.dumps(tc['input'], ensure_ascii=False)}]")
    return "\n".join(parts) or "(no output)"


def _render_tool_results(results: list[dict[str, Any]]) -> str:
    """Render a ``tool_results`` turn as a user-text message — the matching half of
    :func:`_render_assistant_text`, since results also can't be replayed as native blocks."""
    lines = ["[tool results]"]
    for r in results:
        lines.append(f"{r['name']} → {r['content']}")
    return "\n".join(lines)


def _text_blocks(text: str) -> list[dict[str, Any]]:
    """Wrap replayed turn text in a single text block. ``content`` MUST be a list: the CLI
    runs ``content.some(...)`` over every input message to look for ``tool_use`` blocks, and a
    bare string has no ``.some`` (crashes the CLI on the first replayed turn)."""
    return [{"type": "text", "text": text}]


def _user(content: str) -> dict[str, Any]:
    return {"type": "user", "session_id": "", "parent_tool_use_id": None,
            "message": {"role": "user", "content": _text_blocks(content)}}


def _assistant(text: str) -> dict[str, Any]:
    return {"type": "assistant", "session_id": "", "parent_tool_use_id": None,
            "message": {"role": "assistant", "content": _text_blocks(text)}}


def _to_sdk_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert the neutral history into the SDK's streaming-input message dicts, as
    alternating user/assistant TEXT turns (the only multi-turn shape the CLI accepts)."""
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m["role"]
        if role == "user":
            out.append(_user(m["content"]))
        elif role == "assistant":
            out.append(_assistant(_render_assistant_text(m.get("content") or "", m.get("tool_calls") or [])))
        elif role == "tool_results":
            out.append(_user(_render_tool_results(m["results"])))
    return out


async def _astream(items: list[dict[str, Any]]):
    for it in items:
        yield it


def _suppressed_handler(name: str):
    async def _handler(args: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover - never run
        # can_use_tool denies first, so this never executes; present a clear marker just in case.
        return {"content": [{"type": "text", "text": f"tool {name} is executed by the host app"}]}
    return _handler


class AgentSdkProvider(LLMProvider):
    def __init__(self, settings: Settings):
        try:
            import claude_agent_sdk  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise ProviderError("the 'claude-agent-sdk' package is not installed") from exc
        self._model = settings.agent_sdk_model
        self._cli_path = settings.claude_cli_path or None
        # Reasoning-quality + chain-of-thought options, resolved ONCE (stable for the process).
        # Merged into every ClaudeAgentOptions below so the one-shot and persistent-client paths
        # are byte-identical. ``_thinking_active`` tells the loop whether to expect thinking blocks.
        self._reasoning_opts = {**_thinking_options(settings.agent_sdk_thinking),
                                **_effort_option(settings.agent_sdk_effort)}
        self._thinking_active = "thinking" in self._reasoning_opts
        self._server_cache: tuple[tuple[str, ...], Any] | None = None
        # Single-slot connection prewarm (see _PREWARM_TTL_S). Holds a background ``connect()``
        # task for the NEXT turn's client, its (system, tools) fingerprint, and when it started —
        # so a later turn can adopt it only if it matches and is still fresh. The provider is an
        # app-level singleton (app.state.provider), so this slot persists across turns/sessions
        # of the single local user; at most ONE spare subprocess is ever held.
        self._prewarm_task: asyncio.Task | None = None
        self._prewarm_fp: tuple[int, tuple[str, ...]] | None = None
        self._prewarm_at: float = 0.0

    def _server(self, tools: list[dict[str, Any]]) -> Any:
        key = tuple(t["name"] for t in tools)
        if self._server_cache is None or self._server_cache[0] != key:
            from claude_agent_sdk import create_sdk_mcp_server
            from claude_agent_sdk import tool as sdk_tool
            sdk_tools = [
                sdk_tool(t["name"], t["description"], t["input_schema"])(_suppressed_handler(t["name"]))
                for t in tools
            ]
            server = create_sdk_mcp_server(name=_SERVER_NAME, version="1.0.0", tools=sdk_tools)
            self._server_cache = (key, server)
        return self._server_cache[1]

    # ---- persistent-client connection + prewarm pool ---------------------------------------
    async def _connect_client(self, system: str, tools: list[dict[str, Any]]) -> Any:
        """Build the persistent-client options and return a freshly ``connect()``-ed
        ``ClaudeSDKClient``. Shared by the live turn (:meth:`_AgentSdkTurn.__aenter__`) and the
        background prewarm so both produce an IDENTICAL connection — the only difference is WHEN
        the ~0.5s connect is paid. The client comes back empty (no conversation state); the
        turn's first ``chat()`` seeds the full history, so a prewarmed client is interchangeable
        with one connected at turn start."""
        from claude_agent_sdk import (
            ClaudeAgentOptions,
            ClaudeSDKClient,
            PermissionResultDeny,
        )

        async def _deny(tool_name: str, input_data: dict[str, Any], context: Any) -> Any:
            return PermissionResultDeny(message=_DENY_MESSAGE, interrupt=False)

        options = ClaudeAgentOptions(
            model=self._model,
            system_prompt=system,
            mcp_servers={_SERVER_NAME: self._server(tools)},
            tools=[],
            allowed_tools=[],
            setting_sources=[],
            permission_mode="default",
            can_use_tool=_deny,
            max_turns=1,
            include_partial_messages=True,   # stream text deltas to the UI (see _consume)
            env=dict(_NEUTRALIZE_ENV),
            cli_path=self._cli_path,
            **self._reasoning_opts,          # effort + extended-thinking (chain-of-thought capture)
        )
        client = ClaudeSDKClient(options=options)
        await client.connect()
        return client

    @staticmethod
    def _fingerprint(system: str, tools: list[dict[str, Any]]) -> tuple[int, tuple[str, ...]]:
        """A cheap identity for a (system, tools) pair. Both are stable across a session, so a
        prewarmed connection built for one turn is reusable by the next iff this matches."""
        return (hash(system), tuple(t["name"] for t in tools))

    async def acquire_client(self, system: str, tools: list[dict[str, Any]]) -> Any:
        """Return a connected client for a turn: adopt a matching, still-fresh prewarmed
        connection if one is ready (its ~0.5s connect was paid in the background during the
        previous turn's idle gap), else connect a fresh one now. Adopting a prewarmed connect
        that FAILED in the background transparently falls back to a fresh connect — so prewarm
        only ever helps, never regresses correctness."""
        task = self._take_prewarmed(system, tools)
        if task is not None:
            try:
                return await task
            except Exception:  # noqa: BLE001 — background connect failed; just connect fresh
                pass
        return await self._connect_client(system, tools)

    def _take_prewarmed(self, system: str, tools: list[dict[str, Any]]) -> asyncio.Task | None:
        """Hand off the prewarmed connect task iff it matches ``(system, tools)`` and is within
        the freshness TTL; otherwise drop it (disconnecting in the background) and return None."""
        task = self._prewarm_task
        if task is None:
            return None
        matches = self._prewarm_fp == self._fingerprint(system, tools)
        fresh = (time.monotonic() - self._prewarm_at) < _PREWARM_TTL_S
        if matches and fresh:
            self._prewarm_task = None
            self._prewarm_fp = None
            return task
        self._discard_prewarm()  # stale or mismatched — never adopt; reclaim the subprocess
        return None

    def start_prewarm(self, system: str, tools: list[dict[str, Any]]) -> None:
        """Kick off a background connect for the NEXT turn (single global slot). Best-effort and
        non-blocking: any prior unused prewarm is discarded first so at most one spare subprocess
        is ever held. Called at end-of-turn, when the user is reading the answer (idle time)."""
        self._discard_prewarm()
        self._prewarm_fp = self._fingerprint(system, tools)
        self._prewarm_at = time.monotonic()
        self._prewarm_task = asyncio.create_task(self._connect_client(system, tools))

    def _discard_prewarm(self) -> None:
        """Clear the prewarm slot and, if it held a connect task, disconnect the (soon-to-be)
        client in the background so an unadopted spare subprocess is never leaked."""
        task = self._prewarm_task
        self._prewarm_task = None
        self._prewarm_fp = None
        if task is None:
            return

        async def _cleanup() -> None:
            try:
                client = await task
            except Exception:  # noqa: BLE001 — connect never completed; nothing to disconnect
                return
            with contextlib.suppress(Exception):
                await client.disconnect()

        asyncio.create_task(_cleanup())

    async def aclose(self) -> None:
        """Disconnect any prewarmed spare connection (graceful shutdown). Idempotent."""
        self._discard_prewarm()

    async def chat(self, *, system, messages, tools, cache_key=None) -> AssistantTurn:
        # cache_key is accepted for the provider-agnostic interface but ignored — the CLI caches
        # the stable prefix (system + tools) automatically.
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            PermissionResultDeny,
            ResultMessage,
            TextBlock,
            ThinkingBlock,
            ToolUseBlock,
            query,
        )

        async def _deny(tool_name: str, input_data: dict[str, Any], context: Any) -> Any:
            # Deny EVERY tool: the host app executes tools itself. We read the tool_use blocks
            # off the assistant message, so nothing is lost by refusing execution here.
            return PermissionResultDeny(message=_DENY_MESSAGE, interrupt=False)

        options = ClaudeAgentOptions(
            model=self._model,
            system_prompt=system,                 # string => the app's prompt ONLY (no CC preset)
            mcp_servers={_SERVER_NAME: self._server(tools)},
            tools=[],                              # expose NO built-in tools to the model
            allowed_tools=[],                      # auto-approve nothing
            setting_sources=[],                    # ignore ~/.claude/CLAUDE.md + project/local settings
            permission_mode="default",
            can_use_tool=_deny,
            max_turns=1,                           # exactly one assistant turn
            env=dict(_NEUTRALIZE_ENV),
            cli_path=self._cli_path,               # None => the SDK auto-discovers `claude` on PATH
            **self._reasoning_opts,                # effort + extended-thinking (chain-of-thought capture)
        )

        text_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        usage = Usage()
        assistant_error: str | None = None
        fatal: str | None = None
        try:
            async for msg in query(prompt=_astream(_to_sdk_messages(messages)), options=options):
                if isinstance(msg, AssistantMessage):
                    if msg.error:
                        assistant_error = str(msg.error)
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            text_parts.append(block.text)
                        elif isinstance(block, ThinkingBlock):
                            thinking_parts.append(block.thinking)
                        elif isinstance(block, ToolUseBlock):
                            tool_calls.append(
                                ToolCall(id=block.id, name=_strip_prefix(block.name), input=dict(block.input))
                            )
                elif isinstance(msg, ResultMessage):
                    usage = _usage_from(msg.usage)
                    if msg.api_error_status:
                        fatal = f"HTTP {msg.api_error_status} (subtype={msg.subtype})"
                    elif msg.is_error and msg.subtype not in _OK_RESULT_SUBTYPES:
                        fatal = f"result error: {msg.subtype}"
        except Exception as exc:  # noqa: BLE001
            # max_turns=1 raises AFTER delivering a tool-calling assistant turn — the EXPECTED
            # stop. Only surface it when nothing usable came back (e.g. CLI missing / not logged in).
            if not text_parts and not tool_calls:
                raise ProviderError(f"Agent SDK query failed: {exc}") from exc

        if assistant_error:
            raise ProviderError(f"Agent SDK error: {assistant_error}")
        if fatal:
            raise ProviderError(f"Agent SDK {fatal}")

        return AssistantTurn(
            text="".join(text_parts) or None,
            tool_calls=tool_calls,
            stop_reason="tool_use" if tool_calls else "end_turn",
            usage=usage,
            thinking="".join(thinking_parts) or None,
        )

    def open_turn(self, *, system, tools, cache_key=None) -> _AgentSdkTurn:
        """Open a turn-scoped handle that keeps ONE warm ``claude`` CLI subprocess alive across
        every step of a single user turn, instead of spawning a fresh one per ``chat()`` (the
        one-shot ``query()`` path above pays ~3s of subprocess + CLI init on EVERY call). See
        :class:`_AgentSdkTurn`."""
        return _AgentSdkTurn(self, system=system, tools=tools, cache_key=cache_key)


async def _consume(
    stream: Any, on_text: OnText | None
) -> tuple[str, list[ToolCall], Usage, str]:
    """Read an SDK message stream into ``(text, tool_calls, usage, thinking)``, forwarding text
    deltas to ``on_text`` as they arrive when partial-message streaming is enabled. Mirrors the
    parsing in :meth:`AgentSdkProvider.chat`; used by the persistent per-turn path
    (:class:`_AgentSdkTurn`), whose ``receive_response()`` iterator terminates cleanly on the
    ``ResultMessage`` (it does NOT raise ``error_max_turns`` the way the one-shot ``query()``
    iterator does). Thinking is collected from the authoritative ``ThinkingBlock``(s) — never
    forwarded to ``on_text``, so the model's chain-of-thought never leaks into the UI text bubble;
    the loop persists it to the per-session debug trace instead."""
    from claude_agent_sdk import (
        AssistantMessage,
        ResultMessage,
        StreamEvent,
        TextBlock,
        ThinkingBlock,
        ToolUseBlock,
    )

    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    usage = Usage()
    assistant_error: str | None = None
    fatal: str | None = None
    async for msg in stream:
        if isinstance(msg, StreamEvent):
            # include_partial_messages=True surfaces the raw Anthropic SSE events. Forward only
            # text deltas to the UI; the authoritative full text is taken from the AssistantMessage
            # TextBlock(s) below, so a dropped/duplicated delta never corrupts the recorded turn.
            # thinking_delta events are intentionally NOT forwarded — reasoning is captured from
            # the ThinkingBlock(s) below for the trace, never streamed into the visible answer.
            ev = msg.event or {}
            if ev.get("type") == "content_block_delta" and on_text is not None:
                delta = ev.get("delta") or {}
                if delta.get("type") == "text_delta":
                    chunk = delta.get("text") or ""
                    if chunk:
                        await on_text(chunk)
        elif isinstance(msg, AssistantMessage):
            if msg.error:
                assistant_error = str(msg.error)
            for block in msg.content:
                if isinstance(block, TextBlock):
                    text_parts.append(block.text)
                elif isinstance(block, ThinkingBlock):
                    thinking_parts.append(block.thinking)
                elif isinstance(block, ToolUseBlock):
                    tool_calls.append(
                        ToolCall(id=block.id, name=_strip_prefix(block.name), input=dict(block.input))
                    )
        elif isinstance(msg, ResultMessage):
            usage = _usage_from(msg.usage)
            if msg.api_error_status:
                fatal = f"HTTP {msg.api_error_status} (subtype={msg.subtype})"
            elif msg.is_error and msg.subtype not in _OK_RESULT_SUBTYPES:
                fatal = f"result error: {msg.subtype}"
    if assistant_error:
        raise ProviderError(f"Agent SDK error: {assistant_error}")
    if fatal:
        raise ProviderError(f"Agent SDK {fatal}")
    return "".join(text_parts), tool_calls, usage, "".join(thinking_parts)


class _AgentSdkTurn:
    """One connected :class:`ClaudeSDKClient` reused across a single user turn's steps (#1a).

    The one-shot ``query()`` path spawns + initializes a fresh ``claude`` CLI subprocess on
    EVERY step (~3s of fixed overhead per call). A user turn runs several steps (one per round of
    tool calls), so that cost is paid 4-8x. Here we ``connect()`` ONE persistent client at the
    start of the turn (paying init once, ~0.8s) and send each step over it (~1.5s warm).

    The client is STATEFUL — it remembers prior messages on the same connection — so within a
    turn we send INCREMENTALLY: the first ``chat()`` seeds the full prior history + new user
    message; each later ``chat()`` sends ONLY the messages appended since (the tool results) and
    never re-sends the assistant turns the CLI itself produced. This reconstructs the exact
    conversation the one-shot path would replay, without re-sending it or re-initializing.

    State lives for ONE turn only: a fresh connection is opened per turn and seeded from
    ``session.messages`` (still the source of truth), so there is no cross-turn drift and the
    app's compaction / resume / persistence are unaffected.

    Graceful degradation: if the persistent client can't connect, the turn transparently falls
    back to the provider's one-shot ``chat()`` per step (current behavior), so reliability never
    regresses — only the latency win is forgone.
    """

    def __init__(self, provider: AgentSdkProvider, *, system, tools, cache_key=None):
        self._provider = provider
        self._system = system
        self._tools = tools
        self._cache_key = cache_key
        self._client: Any = None
        self._sent = 0          # number of session.messages already streamed to the live client
        self._degraded = False  # True => connect failed; fall back to one-shot chat() per step

    async def __aenter__(self) -> _AgentSdkTurn:
        try:
            # Adopt a prewarmed connection if one is ready (its ~0.5s connect was paid in the
            # background during the previous turn's idle gap), else connect fresh. The options +
            # connect live on the provider (_connect_client) so the prewarm and the live path are
            # byte-identical — only the timing differs.
            self._client = await self._provider.acquire_client(self._system, self._tools)
        except Exception:  # noqa: BLE001 — any connect failure degrades to one-shot, never fatal
            self._client = None
            self._degraded = True
        return self

    async def chat(self, messages: list[dict[str, Any]], *, on_text: OnText | None = None) -> AssistantTurn:
        # Seed the full history on the first step; afterwards send only the newly-appended
        # NON-assistant messages (tool results). The CLI already holds the assistant turns it
        # generated this connection, so re-sending them would duplicate them in its state.
        if self._sent == 0:
            to_send: list[dict[str, Any]] = list(messages)
        else:
            to_send = [m for m in messages[self._sent:] if m.get("role") != "assistant"]
        self._sent = len(messages)

        # Degraded, or (defensively) nothing new to send incrementally: one-shot full replay.
        if self._degraded or self._client is None or not to_send:
            return await self._provider.chat(
                system=self._system, messages=messages, tools=self._tools, cache_key=self._cache_key
            )

        try:
            await self._client.query(_astream(_to_sdk_messages(to_send)))
            text, tool_calls, usage, thinking = await _consume(self._client.receive_response(), on_text)
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"Agent SDK turn query failed: {exc}") from exc

        return AssistantTurn(
            text=text or None,
            thinking=thinking or None,
            tool_calls=tool_calls,
            stop_reason="tool_use" if tool_calls else "end_turn",
            usage=usage,
        )

    async def __aexit__(self, *exc: Any) -> bool:
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.disconnect()
            self._client = None
        # Prewarm the NEXT turn's connection now, while the user reads this answer, so its ~0.5s
        # connect is off the critical path. Skip when this turn ran degraded (the persistent path
        # is unavailable in this environment, so a prewarm would just fail every turn). Best-effort
        # — start_prewarm never raises into the turn teardown.
        if not self._degraded:
            with contextlib.suppress(Exception):
                self._provider.start_prewarm(self._system, self._tools)
        return False
