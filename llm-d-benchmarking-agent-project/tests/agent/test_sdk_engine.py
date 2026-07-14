"""Phase-1 tests for the SDK-native engine over the hermetic FakeTransport.

Every test drives ``SdkNativeEngine.run_turn`` through the SDK's REAL protocol machinery
(ClaudeSDKClient + Query parse the script, bridge can_use_tool, and execute the real
in-process ``benchtools`` MCP handlers, which run the real ``registry.dispatch``); only
the transport is scripted (tests/_sdk_fake.py).
"""
from __future__ import annotations

import json

from pydantic import BaseModel

from app.agent.engine import MAX_TURNS, SdkNativeEngine, steer
from app.agent.session import Session
from app.tools.registry import REGISTRY, ToolSpec
from tests._helpers import _capture_ctx
from tests._sdk_fake import FakeTransport, assistant, result, stream_event, text, tool_use

BT = "mcp__benchtools__"


def _collector():
    seen: list[tuple[str, dict]] = []

    async def emit(event_type, payload):
        seen.append((event_type, payload))

    return seen, emit


async def _approve(kind, payload):
    return True


async def _decline(kind, payload):
    return False


def _make_session(tmp_path) -> Session:
    ctx, _runner = _capture_ctx(tmp_path)
    return Session(id="sdk-t", ctx=ctx)


def _engine(script):
    fakes: list[FakeTransport] = []

    def factory():
        fake = FakeTransport(script)
        fakes.append(fake)
        return fake

    return SdkNativeEngine(transport_factory=factory), fakes


def _delta(chunk: str) -> dict:
    return {"type": "content_block_delta", "delta": {"type": "text_delta", "text": chunk}}


class _EmptyInput(BaseModel):
    pass


async def test_bridge_golden_mapping(tmp_path):
    """One scripted turn with streamed text + a real tool call maps to the expected WS
    event sequence, and the card-rendering tool's full result is persisted."""
    session = _make_session(tmp_path)
    chips = {"suggestions": [{"label": "Analyze", "prompt": "analyze the results"}]}
    script = [[
        stream_event({"type": "message_start", "message": {"usage": {
            "input_tokens": 7, "cache_read_input_tokens": 3,
            "cache_creation_input_tokens": 1, "output_tokens": 0}}}),
        stream_event(_delta("Hel")),
        stream_event(_delta("lo")),
        stream_event({"type": "content_block_delta",
                      "delta": {"type": "thinking_delta", "thinking": "SECRET-REASONING"}}),
        stream_event({"type": "message_delta", "usage": {"output_tokens": 5}}),
        assistant(text("Hello"), tool_use("tu_1", BT + "suggest_next_steps", chips)),
        assistant(text("done")),
        result(usage={"input_tokens": 7, "output_tokens": 5,
                      "cache_read_input_tokens": 3, "cache_creation_input_tokens": 1}),
    ]]
    engine, _fakes = _engine(script)
    seen, emit = _collector()

    await engine.run_turn(session, "hi", emit=emit, request_approval=_approve)

    assert [t for t, _ in seen] == [
        "session_saved",
        "assistant_delta", "assistant_delta",
        "assistant_text",
        "tool_call", "tool_result",
        "assistant_text",
        "usage",
        "done",
    ]
    by_type = {}
    for t, p in seen:
        by_type.setdefault(t, []).append(p)
    assert [p["text"] for p in by_type["assistant_delta"]] == ["Hel", "lo"]
    assert [p["text"] for p in by_type["assistant_text"]] == ["Hello", "done"]
    # thinking never leaks into any client-facing payload
    assert "SECRET-REASONING" not in json.dumps([p for _, p in seen])
    tc = by_type["tool_call"][0]
    assert tc == {"id": "tu_1", "name": "suggest_next_steps", "input": chips}
    tr = by_type["tool_result"][0]
    assert tr["id"] == "tu_1" and tr["name"] == "suggest_next_steps"
    assert tr["result"]["suggestions"] == chips["suggestions"]
    # suggest_next_steps is a card-result tool: its full result is persisted for replay
    assert session.card_results[0]["tool_call_id"] == "tu_1"
    # per-call duration recorded under the model's tool_use id
    assert "tu_1" in session.tool_durations
    # usage: turn accumulation from the stream, session totals from the ResultMessage
    u = by_type["usage"][0]
    assert u["turn"] == {"input": 7, "output": 5, "cache_read": 3, "cache_write": 1,
                         "calls": 1, "total": 16}
    assert u["session"]["total"] == 16
    assert u["context_window"] == {"tokens": 11, "input": 7, "cache_read": 3, "cache_write": 1}
    assert session.total_input_tokens == 7 and session.total_output_tokens == 5
    # the SDK conversation id is persisted for the next turn's resume
    assert session.sdk_session_id == "default"


async def test_approval_rejected_becomes_rejected_result(tmp_path):
    """A declined approval gate inside a real handler (run_shell, mutating) surfaces as the
    verbatim {"rejected": true, note} result — reaching both the UI and the model."""
    session = _make_session(tmp_path)
    script = [[
        assistant(tool_use("tu_1", BT + "run_shell", {"command": "rm -rf /tmp/scratch"})),
        assistant(text("understood")),
        result(),
    ]]
    engine, _fakes = _engine(script)
    seen, emit = _collector()

    await engine.run_turn(session, "clean up", emit=emit, request_approval=_decline)

    tr = next(p for t, p in seen if t == "tool_result")
    assert tr["result"]["rejected"] is True
    assert tr["result"]["reason"].startswith("user rejected the command")
    assert tr["result"]["note"].startswith("the user declined this action")
    # the model-facing mirror carries the same full rejection result
    results_msg = next(m for m in session.messages if m.get("role") == "tool_results")
    assert results_msg["results"][0]["content"] == tr["result"]
    assert results_msg["results"][0]["tool_call_id"] == "tu_1"


async def test_error_max_turns_maps_to_step_limit_error(tmp_path):
    session = _make_session(tmp_path)
    script = [[
        assistant(text("partial progress")),
        result(subtype="error_max_turns", is_error=True),
    ]]
    engine, _fakes = _engine(script)
    seen, emit = _collector()

    await engine.run_turn(session, "go", emit=emit, request_approval=_approve)

    err = next(p for t, p in seen if t == "error")
    assert err == {"message": f"reached the step limit ({MAX_TURNS}); pausing."}
    assert [t for t, _ in seen][-1] == "done"


async def test_full_tool_result_passes_through_unclamped(tmp_path, monkeypatch):
    """A huge tool result reaches the UI event, the mirror, and (via the wrapper's JSON
    serialization) the model — whole, with no truncation envelope anywhere."""
    blob = "x" * 50_000

    async def echo_blob(ctx):
        return {"blob": blob}

    monkeypatch.setitem(
        REGISTRY, "echo_blob", ToolSpec("echo_blob", "returns a huge blob", _EmptyInput, echo_blob))
    session = _make_session(tmp_path)
    script = [[
        assistant(tool_use("tu_1", BT + "echo_blob", {})),
        assistant(text("got it")),
        result(),
    ]]
    engine, _fakes = _engine(script)
    seen, emit = _collector()

    await engine.run_turn(session, "fetch", emit=emit, request_approval=_approve)

    tr = next(p for t, p in seen if t == "tool_result")
    assert tr["result"] == {"blob": blob}
    results_msg = next(m for m in session.messages if m.get("role") == "tool_results")
    assert results_msg["results"][0]["content"] == {"blob": blob}


async def test_can_use_tool_denies_non_benchtools(tmp_path):
    """Defense in depth: any tool outside mcp__benchtools__* is denied by the gatekeeper —
    no handler runs, no tool_call event fires, and the model sees the denial."""
    session = _make_session(tmp_path)
    script = [[
        assistant(tool_use("tu_1", "mcp__other__evil", {"x": 1})),
        assistant(text("moving on")),
        result(),
    ]]
    engine, fakes = _engine(script)
    seen, emit = _collector()

    await engine.run_turn(session, "hi", emit=emit, request_approval=_approve)

    decision = fakes[0].permission_responses[0]
    assert decision["behavior"] == "deny"
    assert "benchtools" in decision["message"]
    assert not any(t == "tool_call" for t, _ in seen)
    # the mirror records the SDK's error feedback for the denied call
    results_msg = next(m for m in session.messages if m.get("role") == "tool_results")
    assert results_msg["results"][0]["tool_call_id"] == "tu_1"
    assert "benchtools" in results_msg["results"][0]["content"]


async def test_steer_delivered_as_follow_up_query(tmp_path, monkeypatch):
    """A steer queued while the turn runs is sent as a follow-up query() on the same client
    after the ResultMessage — one app-level turn, one done event."""
    session = _make_session(tmp_path)

    async def steer_probe(ctx):
        # Simulates the WS handler steering while this turn is mid-tool.
        assert steer(session.id, "actually do B") is True
        return {"ok": True}

    monkeypatch.setitem(
        REGISTRY, "steer_probe",
        ToolSpec("steer_probe", "queues a steer", _EmptyInput, steer_probe))
    script = [
        [
            assistant(tool_use("tu_1", BT + "steer_probe", {})),
            assistant(text("first answer")),
            result(),
        ],
        [assistant(text("steered answer")), result()],
    ]
    engine, fakes = _engine(script)
    seen, emit = _collector()

    await engine.run_turn(session, "do A", emit=emit, request_approval=_approve)

    assert [m["message"]["content"] for m in fakes[0].user_messages] == ["do A", "actually do B"]
    assert [p["text"] for t, p in seen if t == "assistant_text"] == [
        "first answer", "steered answer"]
    assert [t for t, _ in seen].count("done") == 1
    assert {"role": "user", "content": "actually do B"} in session.messages
    # a steer with no live turn is refused (the WS handler then starts a fresh turn)
    assert steer(session.id, "too late") is False


async def test_mirror_matches_todays_message_shapes(tmp_path):
    """session.messages stays a render mirror in exactly the old loop's shapes: user /
    assistant(+tool_calls) / one tool_results block per step."""
    session = _make_session(tmp_path)
    chips = {"suggestions": [{"label": "Next", "prompt": "go next"}]}
    script = [[
        assistant(text("Working on it"), tool_use("tu_1", BT + "suggest_next_steps", chips)),
        assistant(text("all set")),
        result(),
    ]]
    engine, _fakes = _engine(script)
    _seen, emit = _collector()

    await engine.run_turn(session, "hi", emit=emit, request_approval=_approve)

    assert session.messages[0] == {"role": "user", "content": "hi"}
    step = session.messages[1]
    assert step["role"] == "assistant" and step["content"] == "Working on it"
    assert step["tool_calls"] == [
        {"id": "tu_1", "name": "suggest_next_steps", "input": chips}]
    results_msg = session.messages[2]
    assert results_msg["role"] == "tool_results"
    row = results_msg["results"][0]
    assert row["tool_call_id"] == "tu_1" and row["name"] == "suggest_next_steps"
    assert row["content"]["suggestions"] == chips["suggestions"]
    assert session.messages[3] == {"role": "assistant", "content": "all set", "tool_calls": []}
