"""Provider turn abstraction + streaming (#2) and the persistent per-turn client (#1a).

Hermetic — no CLI subprocess, no network. We exercise:
  * StatelessTurn / open_provider_turn (the default path every non-SDK provider + the test fakes
    get): each step is a one-shot chat() over the FULL message list, on_text ignored.
  * _consume(): parses an SDK message stream into (text, tool_calls, usage, thinking) and forwards
    text deltas to on_text (thinking is captured but NOT streamed) — built from real
    ``claude_agent_sdk`` message objects.
  * _AgentSdkTurn incremental send logic (with a fake connected client) + graceful degradation.
  * The agent loop streaming end-to-end: a provider that streams via open_turn makes the loop
    emit ASSISTANT_DELTA fragments live, then a final ASSISTANT_TEXT.
"""
from __future__ import annotations

import claude_agent_sdk as sdk

from app.agent import events
from app.agent.loop import AgentLoop
from app.agent.session import Session
from app.llm.agent_sdk_provider import _AgentSdkTurn, _consume
from app.llm.provider import (
    AssistantTurn,
    ProviderTurn,
    StatelessTurn,
    Usage,
    open_provider_turn,
)
from tests._helpers import _session as _base_session

# ---- StatelessTurn / open_provider_turn ----------------------------------------------------

class _RecordingProvider:
    """A bare fake (no open_turn) — exactly the shape of the test FakeProviders. Records the
    messages passed to each chat() so we can prove StatelessTurn forwards the FULL list."""

    def __init__(self) -> None:
        self.calls: list[list[dict]] = []

    async def chat(self, *, system, messages, tools, cache_key=None) -> AssistantTurn:
        self.calls.append(list(messages))
        return AssistantTurn(text="ok", tool_calls=[], usage=Usage(output_tokens=1))


async def test_open_provider_turn_falls_back_to_stateless_for_bare_provider():
    prov = _RecordingProvider()
    turn = open_provider_turn(prov, system="sys", tools=[], cache_key="s1")
    assert isinstance(turn, StatelessTurn)
    async with turn as t:
        # on_text is accepted but ignored on the stateless path (no streaming) — must not raise.
        out = await t.chat([{"role": "user", "content": "a"}], on_text=lambda _x: None)
        assert out.text == "ok"
        # full message list forwarded verbatim, plus system/tools/cache_key from the turn.
        await t.chat([{"role": "user", "content": "a"}, {"role": "user", "content": "b"}])
    assert [len(c) for c in prov.calls] == [1, 2]


def test_open_provider_turn_uses_provider_open_turn_when_present():
    """A provider that implements open_turn gets its own turn, not the stateless wrapper."""
    sentinel = object()

    class _P:
        def open_turn(self, *, system, tools, cache_key=None, model=None, effort=None):
            return sentinel

    assert open_provider_turn(_P(), system="s", tools=[], cache_key="k") is sentinel


# ---- _consume(): stream parsing + delta forwarding -----------------------------------------

async def _stream(items):
    for it in items:
        yield it


def _delta(text: str) -> sdk.StreamEvent:
    return sdk.StreamEvent(
        uuid="u", session_id="s",
        event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}},
    )


def _thinking_delta(text: str) -> sdk.StreamEvent:
    return sdk.StreamEvent(
        uuid="u", session_id="s",
        event={"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": text}},
    )


async def test_consume_collects_text_tool_calls_usage_and_streams_deltas():
    deltas: list[str] = []

    async def on_text(t: str) -> None:
        deltas.append(t)

    msgs = [
        _delta("Hel"),
        _delta("lo"),
        sdk.AssistantMessage(
            content=[
                sdk.ThinkingBlock(thinking="Let me probe the cluster first.", signature="sig"),
                sdk.TextBlock(text="Hello"),
                sdk.ToolUseBlock(id="t1", name="mcp__benchtools__probe_environment",
                                 input={"namespace": "llm-d"}),
            ],
            model="claude-haiku-4-5",
        ),
        sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                          num_turns=1, session_id="s",
                          usage={"input_tokens": 5, "output_tokens": 9,
                                 "cache_read_input_tokens": 100, "cache_creation_input_tokens": 0}),
    ]
    text, tool_calls, usage, thinking = await _consume(_stream(msgs), on_text)

    assert text == "Hello"                       # authoritative text from the TextBlock
    assert thinking == "Let me probe the cluster first."  # CoT captured from the ThinkingBlock...
    assert "thinking" not in "".join(deltas).lower()      # ...but NEVER streamed into the UI text
    assert deltas == ["Hel", "lo"]               # live deltas forwarded in order...
    assert "".join(deltas) == text               # ...and they reconstruct the full text exactly
    assert len(tool_calls) == 1
    assert (tool_calls[0].name, tool_calls[0].input) == ("probe_environment", {"namespace": "llm-d"})
    assert (usage.output_tokens, usage.cache_read_tokens) == (9, 100)


async def test_consume_captures_thinking_from_deltas_when_no_thinking_block():
    """The persistent per-turn path runs include_partial_messages=True and the CLI delivers the
    chain-of-thought ONLY as thinking_delta stream events — the final AssistantMessage carries NO
    ThinkingBlock. _consume must accumulate those deltas so the reasoning still reaches the trace
    (the cot_trace-lost-thinking bug), and must never stream them into the visible answer."""
    deltas: list[str] = []

    async def on_text(t: str) -> None:
        deltas.append(t)

    msgs = [
        _thinking_delta("Let me probe "),
        _thinking_delta("the cluster first."),
        _delta("Hel"),
        _delta("lo"),
        # No ThinkingBlock in the final assistant message on this streaming path.
        sdk.AssistantMessage(content=[sdk.TextBlock(text="Hello")], model="m"),
        sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                          num_turns=1, session_id="s", usage=None),
    ]
    text, tool_calls, usage, thinking = await _consume(_stream(msgs), on_text)

    assert text == "Hello"                                       # authoritative text unchanged
    assert thinking == "Let me probe the cluster first."         # reasoning recovered from deltas
    assert deltas == ["Hel", "lo"]                               # only text streamed to the UI...
    assert "probe" not in "".join(deltas)                        # ...thinking never leaks into it


async def test_consume_prefers_thinking_block_over_deltas_when_both_present():
    """If the CLI DOES send a populated ThinkingBlock, it is authoritative and the deltas are not
    appended on top of it — no double-counting."""
    msgs = [
        _thinking_delta("partial reasoning"),
        sdk.AssistantMessage(
            content=[sdk.ThinkingBlock(thinking="full reasoning", signature="sig"),
                     sdk.TextBlock(text="ok")],
            model="m",
        ),
        sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                          num_turns=1, session_id="s", usage=None),
    ]
    _text, _tc, _usage, thinking = await _consume(_stream(msgs), None)
    assert thinking == "full reasoning"


async def test_consume_without_on_text_still_parses():
    msgs = [
        _delta("ignored when on_text is None"),
        sdk.AssistantMessage(content=[sdk.TextBlock(text="done")], model="m"),
        sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                          num_turns=1, session_id="s", usage=None),
    ]
    text, tool_calls, usage, thinking = await _consume(_stream(msgs), None)
    assert text == "done"
    assert tool_calls == []
    assert thinking == ""  # no ThinkingBlock in the stream → empty (the loop stores None)


# ---- _AgentSdkTurn: incremental send + degradation -----------------------------------------

class _FakeClient:
    """Stands in for a connected ClaudeSDKClient: records each query()'s rendered messages and
    replays a canned response (one text turn) on receive_response()."""

    def __init__(self) -> None:
        self.sent: list[list[dict]] = []

    async def query(self, prompt, session_id="default"):
        self.sent.append([m async for m in prompt])

    async def receive_response(self):
        yield sdk.AssistantMessage(content=[sdk.TextBlock(text="ack")], model="m")
        yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                                num_turns=1, session_id="s", usage=None)

    async def disconnect(self) -> None:
        pass


async def test_agent_sdk_turn_sends_full_seed_then_only_new_non_assistant_tail():
    fake = _FakeClient()
    turn = _AgentSdkTurn(provider=object(), system="sys", tools=[], cache_key="s1")
    turn._client = fake  # simulate a successful connect

    # Step 1: only the user message exists → full seed.
    messages = [{"role": "user", "content": "go"}]
    await turn.chat(messages)
    # Loop appends the assistant turn it just produced + the tool results.
    messages += [
        {"role": "assistant", "content": "ack", "tool_calls": [{"id": "t1", "name": "x", "input": {}}]},
        {"role": "tool_results", "results": [{"tool_call_id": "t1", "name": "x", "content": "{}"}]},
    ]
    # Step 2: send ONLY the new tool_results — never re-send the assistant turn the CLI generated.
    await turn.chat(messages)
    # Another round.
    messages += [
        {"role": "assistant", "content": "more", "tool_calls": [{"id": "t2", "name": "y", "input": {}}]},
        {"role": "tool_results", "results": [{"tool_call_id": "t2", "name": "y", "content": "{}"}]},
    ]
    await turn.chat(messages)

    # _to_sdk_messages renders 1:1, so the rendered-message counts reveal exactly what was sent.
    assert [len(s) for s in fake.sent] == [1, 1, 1]
    # The seed carried the user turn; the later sends are the tool_results rendered as user text.
    assert "go" in fake.sent[0][0]["message"]["content"][0]["text"]
    assert "[tool results]" in fake.sent[1][0]["message"]["content"][0]["text"]
    assert "[tool results]" in fake.sent[2][0]["message"]["content"][0]["text"]


async def test_agent_sdk_turn_degrades_to_one_shot_chat_when_not_connected():
    """If connect() failed (degraded) the turn must transparently use the one-shot chat()."""
    recorded: list[list[dict]] = []

    class _Prov:
        async def chat(self, *, system, messages, tools, cache_key=None):
            recorded.append(list(messages))
            return AssistantTurn(text="fallback", tool_calls=[], usage=Usage())

    turn = _AgentSdkTurn(provider=_Prov(), system="sys", tools=[], cache_key="s1")
    turn._degraded = True
    turn._client = None
    out = await turn.chat([{"role": "user", "content": "hi"}])
    assert out.text == "fallback"
    assert recorded == [[{"role": "user", "content": "hi"}]]


# ---- loop streaming end-to-end -------------------------------------------------------------

class _StreamingTurn(ProviderTurn):
    """A turn that streams two text deltas then returns the assembled reply — proves the loop
    forwards on_text live AND still emits the final ASSISTANT_TEXT."""

    async def chat(self, messages, *, on_text=None):
        if on_text is not None:
            await on_text("Hel")
            await on_text("lo")
        return AssistantTurn(text="Hello", tool_calls=[], usage=Usage(output_tokens=2))


class _StreamingProvider:
    def open_turn(self, *, system, tools, cache_key=None, model=None, effort=None):
        return _StreamingTurn()

    async def chat(self, *, system, messages, tools, cache_key=None):  # pragma: no cover
        raise AssertionError("loop must use open_turn, not one-shot chat()")


def _session(tmp_path) -> Session:
    sess = _base_session(tmp_path)
    sess.catalog_injected = True   # skip the repo-dependent live-catalog injection
    return sess


async def test_loop_emits_assistant_deltas_then_final_text(tmp_path):
    events_seen: list[tuple[str, dict]] = []

    async def emit(event_type, payload):
        events_seen.append((event_type, payload))

    async def approve(kind, payload):
        return True

    sess = _session(tmp_path)
    await AgentLoop(_StreamingProvider()).run_turn(sess, "hi", emit=emit, request_approval=approve)

    deltas = [p["text"] for (t, p) in events_seen if t == events.ASSISTANT_DELTA]
    finals = [p["text"] for (t, p) in events_seen if t == events.ASSISTANT_TEXT]
    assert deltas == ["Hel", "lo"]          # streamed live, in order
    assert finals == ["Hello"]              # the authoritative final text still emitted once
    assert events_seen[-1][0] == events.DONE


def test_assistant_delta_is_unbuffered_non_turn_event():
    # Deltas are high-frequency + transient: they must never enter the per-turn replay ring,
    # or they'd evict the real turn events a mid-turn reconnect needs.
    assert events.ASSISTANT_DELTA in events.NON_TURN_EVENTS
