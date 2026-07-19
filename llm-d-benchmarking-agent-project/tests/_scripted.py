"""Shared scripted-turn vocabulary for hermetic tests of the SDK-native engine.

The old provider seam scripted the agent by replaying ``AssistantTurn`` objects through a fake
``LLMProvider``. The engine has no provider — it speaks the SDK wire protocol over a Transport —
so scripting now means rendering the SAME golden-transcript shape as FakeTransport wire turns
(tests/_sdk_fake.py) and installing a transport factory on ``app.state.sdk_transport_factory``
(the /ws handler's hermetic seam; ``run_flow`` passes it straight to the engine).

This module keeps the ergonomic authoring shape: tests/flows still declare
``AssistantTurn(text, tool_calls=[ToolCall(...)])`` scripts (pure data, wire-format-free), and
``sdk_script`` renders one user turn's transcript into one FakeTransport scripted turn. The
dataclasses were relocated here from the deleted ``app/llm/provider.py`` — they are TEST
vocabulary now, not app code.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.tools.mcp_server import TOOL_PREFIX
from tests._sdk_fake import FakeTransport, assistant, result, text, tool_use


@dataclass
class ToolCall:
    """One scripted tool call: the model's tool_use id, the BARE tool name (no MCP prefix —
    ``sdk_script`` adds it), and the args dict."""
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class AssistantTurn:
    """One scripted assistant step: its text plus the tool calls it makes. A step with tool
    calls is followed by the fake auto-executing them through the real in-process MCP handlers;
    a final text-only step ends the turn."""
    text: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)


def sdk_script(turns: list[AssistantTurn]) -> list[list[dict[str, Any]]]:
    """Render one user turn's golden transcript as ONE FakeTransport scripted turn.

    Each :class:`AssistantTurn` becomes one wire ``assistant`` message (text block + a
    ``mcp__benchtools__``-prefixed tool_use per ToolCall); the fake auto-executes every tool_use
    through the real in-process MCP handlers and feeds the result back between assistant
    messages — the same model→tool→model alternation the old scripted provider replayed one
    ``chat()`` at a time."""
    messages: list[dict[str, Any]] = []
    for turn in turns:
        blocks = [text(turn.text)] if turn.text else []
        blocks += [tool_use(tc.id, TOOL_PREFIX + tc.name, tc.input) for tc in turn.tool_calls]
        if blocks:
            messages.append(assistant(*blocks))
    messages.append(result())
    return [messages]


class ScriptedTransports:
    """FIFO of per-user-turn scripts behind ``app.state.sdk_transport_factory``.

    The engine connects one transport per user turn, so ``next_transport`` pops exactly one
    primed script per turn; an unprimed turn gets a clean empty reply (mirrors an exhausted
    scripted provider). Each transport also carries spare empty turns so a stray follow-up
    query (e.g. a raced steer) never trips the fake's script-exhausted assertion.

    Prime with :meth:`add_turns` (AssistantTurn authoring shape) or :meth:`add_script`
    (raw FakeTransport wire turns, for tests that need exotic wire shapes)."""

    def __init__(self, *, response_timeout: float = 10.0):
        self._scripts: list[list[list[dict[str, Any]]]] = []
        self._response_timeout = response_timeout

    def add_turns(self, *turns: AssistantTurn) -> None:
        """Queue ONE user turn whose transcript is ``turns`` (rendered via sdk_script)."""
        self._scripts.append(sdk_script(list(turns)))

    def add_script(self, script: list[list[dict[str, Any]]]) -> None:
        """Queue ONE user turn's raw FakeTransport script (already wire-shaped)."""
        self._scripts.append(script)

    def next_transport(self) -> FakeTransport:
        script = self._scripts.pop(0) if self._scripts else [[result()]]
        return FakeTransport(script + [[result()], [result()]],
                             response_timeout=self._response_timeout)

    def install(self, app: Any) -> ScriptedTransports:
        """Install this queue as the app's transport factory; returns self for chaining."""
        app.state.sdk_transport_factory = self.next_transport
        return self
