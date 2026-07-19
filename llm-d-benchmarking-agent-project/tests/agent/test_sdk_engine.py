"""Phase-1 tests for the SDK-native engine over the hermetic FakeTransport.

Every test drives ``SdkNativeEngine.run_turn`` through the SDK's REAL protocol machinery
(ClaudeSDKClient + Query parse the script, bridge can_use_tool, and execute the real
in-process ``benchtools`` MCP handlers, which run the real ``registry.dispatch``); only
the transport is scripted (tests/_sdk_fake.py).
"""
from __future__ import annotations

import json

import claude_agent_sdk
from claude_agent_sdk import CLIConnectionError
from pydantic import BaseModel

from app.agent.engine import MAX_TURNS, SdkNativeEngine, steer
from app.agent.session import Session
from app.config import Settings
from app.security.policy import CommandPolicy
from app.security.runner import CommandRunner
from app.tools.context import ToolContext
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


def _make_session(tmp_path, *, catalog_injected: bool = True) -> Session:
    """A session on a captured ToolContext. Defaults to an ESTABLISHED session (the one-shot
    catalog preamble already injected) so each test exercises only its own concern; the
    preamble test passes ``catalog_injected=False`` to see the first-turn injection."""
    ctx, _runner = _capture_ctx(tmp_path)
    return Session(id="sdk-t", ctx=ctx, catalog_injected=catalog_injected)


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
        assistant(text("Hello")),
        # terminal step: the suggest chips end the turn (any trailing text would be suppressed)
        assistant(tool_use("tu_1", BT + "suggest_next_steps", chips)),
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
        "usage",
        "done",
    ]
    by_type = {}
    for t, p in seen:
        by_type.setdefault(t, []).append(p)
    assert [p["text"] for p in by_type["assistant_delta"]] == ["Hel", "lo"]
    assert [p["text"] for p in by_type["assistant_text"]] == ["Hello"]
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


async def test_mirror_matches_todays_message_shapes(tmp_path, monkeypatch):
    """session.messages stays a render mirror in exactly the old loop's shapes: user /
    assistant(+tool_calls) / one tool_results block per step."""

    async def echo_probe(ctx):
        return {"ok": True}

    monkeypatch.setitem(
        REGISTRY, "echo_probe", ToolSpec("echo_probe", "test probe", _EmptyInput, echo_probe))
    session = _make_session(tmp_path)
    script = [[
        assistant(text("Working on it"), tool_use("tu_1", BT + "echo_probe", {})),
        assistant(text("all set")),
        result(),
    ]]
    engine, _fakes = _engine(script)
    _seen, emit = _collector()

    await engine.run_turn(session, "hi", emit=emit, request_approval=_approve)

    assert session.messages[0] == {"role": "user", "content": "hi"}
    step = session.messages[1]
    assert step["role"] == "assistant" and step["content"] == "Working on it"
    assert step["tool_calls"] == [{"id": "tu_1", "name": "echo_probe", "input": {}}]
    results_msg = session.messages[2]
    assert results_msg["role"] == "tool_results"
    row = results_msg["results"][0]
    assert row["tool_call_id"] == "tu_1" and row["name"] == "echo_probe"
    assert row["content"] == {"ok": True}
    assert session.messages[3] == {"role": "assistant", "content": "all set", "tool_calls": []}


async def test_preamble_injected_once_then_never_again(tmp_path):
    """First turn of a session carries the env-preprobe + live-catalog blocks (mirrored as
    separate messages, coalesced on the wire); later turns carry just the user text."""
    session = _make_session(tmp_path, catalog_injected=False)
    session.env_snapshot = {"kube_context": "kind-x"}

    engine1, fakes1 = _engine([[assistant(text("hello")), result()]])
    seen1, emit1 = _collector()
    await engine1.run_turn(session, "hi", emit=emit1, request_approval=_approve)

    wire = fakes1[0].user_messages[0]["message"]["content"]
    assert wire.startswith("[environment pre-probe — read-only snapshot")
    assert "kind-x" in wire
    assert "[live catalog snapshot" in wire
    assert wire.endswith("hi")
    env_msg, catalog_msg, user_msg = session.messages[0:3]
    assert env_msg["synthetic"] is True and env_msg["content"].startswith("[environment pre-probe")
    assert catalog_msg["content"].startswith("[live catalog snapshot")
    assert user_msg == {"role": "user", "content": "hi"}
    assert session.prewarmed is True and session.catalog_injected is True

    engine2, fakes2 = _engine([[assistant(text("again!")), result()]])
    seen2, emit2 = _collector()
    await engine2.run_turn(session, "again", emit=emit2, request_approval=_approve)
    assert fakes2[0].user_messages[0]["message"]["content"] == "again"


class _DeadTransport(FakeTransport):
    """Connect fails the way a GC'd/corrupt --resume transcript kills the CLI at startup."""

    async def connect(self):
        raise CLIConnectionError("Failed to start Claude Code: exited with code 1")


async def test_resume_failure_falls_back_to_fresh_seeded_session(tmp_path):
    session = _make_session(tmp_path)
    session.sdk_session_id = "stale-id"
    session.messages = [
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier answer", "tool_calls": []},
    ]
    transports: list[FakeTransport] = []

    def factory():
        fake = (_DeadTransport if not transports else FakeTransport)(
            [[assistant(text("recovered")), result()]])
        transports.append(fake)
        return fake

    engine = SdkNativeEngine(transport_factory=factory)
    seen, emit = _collector()
    await engine.run_turn(session, "what now?", emit=emit, request_approval=_approve)

    # nothing scary surfaced: a normal turn, no error event
    assert not any(t == "error" for t, _ in seen)
    assert [p["text"] for t, p in seen if t == "assistant_text"] == ["recovered"]
    # the fresh session was seeded ONCE from the prior mirror, then asked the real question
    assert len(transports) == 2
    wire = transports[1].user_messages[0]["message"]["content"]
    assert wire.startswith("[conversation replay")
    assert "earlier question" in wire and "earlier answer" in wire
    assert wire.endswith("what now?")
    # the id was re-minted from the fresh session's ResultMessage
    assert session.sdk_session_id == "default"


async def test_steer_then_decline_keeps_order(tmp_path):
    """Type-instead-of-approve: the steer is queued BEFORE the gate is declined, so the model
    sees the rejected result first, then answers the steer as a follow-up on the same turn."""
    session = _make_session(tmp_path)

    async def steer_then_decline(kind, payload):
        assert steer(session.id, "use /tmp/other instead") is True
        return False

    script = [
        [
            assistant(tool_use("tu_1", BT + "run_shell", {"command": "rm -rf /tmp/x"})),
            assistant(text("acknowledged")),
            result(),
        ],
        [assistant(text("switching to /tmp/other")), result()],
    ]
    engine, fakes = _engine(script)
    seen, emit = _collector()
    await engine.run_turn(session, "clean /tmp/x", emit=emit, request_approval=steer_then_decline)

    types = [t for t, _ in seen]
    tr_idx = types.index("tool_result")
    assert seen[tr_idx][1]["result"]["rejected"] is True
    steered_idx = next(i for i, (t, p) in enumerate(seen)
                       if t == "assistant_text" and p["text"] == "switching to /tmp/other")
    assert tr_idx < steered_idx
    assert [m["message"]["content"] for m in fakes[0].user_messages] == [
        "clean /tmp/x", "use /tmp/other instead"]
    assert types.count("done") == 1


async def test_terminal_suggest_suppresses_trailing_text(tmp_path):
    """After the suggest_next_steps chips are offered, trailing model text (deltas + final)
    is suppressed — the buttons ARE the end of the turn, matching the old loop."""
    session = _make_session(tmp_path)
    chips = {"suggestions": [{"label": "Compare", "prompt": "compare runs"}]}
    script = [[
        assistant(tool_use("tu_1", BT + "suggest_next_steps", chips)),
        stream_event(_delta("Use the")),
        assistant(text("Use the buttons below to choose your next step.")),
        result(),
    ]]
    engine, _fakes = _engine(script)
    seen, emit = _collector()
    await engine.run_turn(session, "done?", emit=emit, request_approval=_approve)

    types = [t for t, _ in seen]
    assert "assistant_delta" not in types and "assistant_text" not in types
    assert "tool_result" in types  # the chips themselves still flowed
    assert session.card_results[0]["tool_call_id"] == "tu_1"
    # the suppressed closer never reaches the mirror either
    assert [m for m in session.messages if m.get("role") == "assistant"
            and "buttons" in (m.get("content") or "")] == []


async def test_usage_payload_carries_compacted_marker(tmp_path):
    session = _make_session(tmp_path)
    script = [[
        {"type": "system", "subtype": "compact_boundary", "session_id": "default"},
        assistant(text("ok")),
        result(usage={"input_tokens": 2, "output_tokens": 1}),
    ]]
    engine, _fakes = _engine(script)
    seen, emit = _collector()
    await engine.run_turn(session, "hi", emit=emit, request_approval=_approve)

    usage = next(p for t, p in seen if t == "usage")
    assert usage["compacted"] is True
    assert set(usage) >= {"turn", "session", "context_window"}
    # get_context_usage is unavailable over the fake transport → gracefully omitted
    assert "context" not in usage


async def test_options_carry_session_overrides(tmp_path, monkeypatch):
    """The per-session model/effort picker and the resume id reach ClaudeAgentOptions on
    connect (connect-per-turn: no set_model needed), alongside the fixed option decisions."""
    session = _make_session(tmp_path)
    session.model_override = "claude-test-9"
    session.effort_override = "low"
    session.sdk_session_id = "resume-me"
    captured = []
    real_client = claude_agent_sdk.ClaudeSDKClient

    def spy(*, options, transport=None):
        captured.append(options)
        return real_client(options=options, transport=transport)

    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", spy)
    engine, _fakes = _engine([[assistant(text("ok")), result(session_id="resume-me")]])
    seen, emit = _collector()
    await engine.run_turn(session, "hi", emit=emit, request_approval=_approve)

    opts = captured[0]
    assert opts.model == "claude-test-9"
    assert opts.effort == "low"
    assert opts.resume == "resume-me"
    assert opts.max_turns == MAX_TURNS
    assert opts.tools == [] and opts.allowed_tools == []
    assert opts.setting_sources == [] and opts.permission_mode == "default"
    assert opts.include_partial_messages is True
    assert opts.cwd == session.ctx.workspace
    assert opts.env["ANTHROPIC_API_KEY"] == "" and opts.env["ANTHROPIC_AUTH_TOKEN"] == ""
    assert opts.env["MCP_TOOL_TIMEOUT"] == "86400000"
    assert set(opts.mcp_servers) == {"benchtools"}


async def test_restart_resumes_with_persisted_sdk_session_id(tmp_path, monkeypatch):
    """App "restart" mid-session (resume battery 3b): a fresh SessionManager over the same
    workspace rebuilds the session from state.json, and the next turn's connect carries
    ``resume=<the persisted sdk_session_id>`` — asserted on the ClaudeAgentOptions."""
    from app.agent.session import SessionManager

    settings = Settings(_env_file=None, repos_dir=tmp_path / "repos",
                        workspace_dir=tmp_path / "ws")
    policy = CommandPolicy.from_file(settings.command_policy_path)
    runner = CommandRunner(settings.repo_paths)
    session = SessionManager(settings, policy, runner).create()
    session.catalog_injected = True  # keep the wire minimal (no preamble)

    engine1, _f1 = _engine([[assistant(text("first")), result(session_id="sdk-abc")]])
    seen1, emit1 = _collector()
    await engine1.run_turn(session, "hi", emit=emit1, request_approval=_approve)
    assert session.sdk_session_id == "sdk-abc"  # minted + persisted by the end-of-turn persist

    reloaded = SessionManager(settings, policy, runner).get_or_load(session.id)  # the restart
    assert reloaded is not None and reloaded is not session
    assert reloaded.sdk_session_id == "sdk-abc" and reloaded.catalog_injected is True
    captured = []
    real_client = claude_agent_sdk.ClaudeSDKClient

    def spy(*, options, transport=None):
        captured.append(options)
        return real_client(options=options, transport=transport)

    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", spy)
    engine2, _f2 = _engine([[assistant(text("resumed")), result(session_id="sdk-abc")]])
    seen2, emit2 = _collector()
    await engine2.run_turn(reloaded, "and now?", emit=emit2, request_approval=_approve)

    assert captured[0].resume == "sdk-abc"
    assert not any(t == "error" for t, _ in seen2)
    assert [p["text"] for t, p in seen2 if t == "assistant_text"] == ["resumed"]


async def test_steer_requeued_to_legacy_list_when_turn_errors(tmp_path, monkeypatch):
    """Resume battery 3d: a steer still queued when the turn ends ABNORMALLY (an error
    ResultMessage — its drain point never came) returns to ``ctx.steer_messages``, so
    main.py's finally backstop applies the old semantics (follow-up turn on error)."""

    async def steer_probe(ctx):
        assert steer(session.id, "pivot to B") is True
        return {"ok": True}

    monkeypatch.setitem(
        REGISTRY, "steer_probe",
        ToolSpec("steer_probe", "queues a steer", _EmptyInput, steer_probe))
    session = _make_session(tmp_path)
    script = [[
        assistant(tool_use("tu_1", BT + "steer_probe", {})),
        result(subtype="error_during_execution", is_error=True),
    ]]
    engine, _fakes = _engine(script)
    seen, emit = _collector()

    await engine.run_turn(session, "do A", emit=emit, request_approval=_approve)

    assert any(t == "error" for t, _ in seen)
    assert session.ctx.steer_messages == ["pivot to B"]
    assert [t for t, _ in seen][-1] == "done"


async def test_watchdog_interrupts_dead_stream(tmp_path):
    """A stream that goes silent mid-turn with NO tool running is a wedged CLI: the watchdog
    interrupts it and surfaces a clean error instead of hanging the turn forever."""
    session = _make_session(tmp_path)
    # The "CLI" accepts the user message, then never sends anything — a dead stream.
    engine = SdkNativeEngine(transport_factory=lambda: FakeTransport([[]]),
                             stream_watchdog_s=0.05)
    seen, emit = _collector()

    await engine.run_turn(session, "hi", emit=emit, request_approval=_approve)

    err = next(p for t, p in seen if t == "error")
    assert "stalled" in err["message"] and "no tool running" in err["message"]
    assert [t for t, _ in seen][-1] == "done"


async def test_watchdog_spares_parked_approval_gate(tmp_path):
    """Silence while a tool is RUNNING is legitimate — an approval gate parked inside the
    handler (here far past the watchdog deadline) must never be counted as a wedged CLI."""
    import asyncio

    session = _make_session(tmp_path)

    async def slow_approve(kind, payload):
        await asyncio.sleep(0.3)  # park the gate well past the 0.05s watchdog
        return True

    script = [[
        assistant(tool_use("tu_1", BT + "run_shell", {"command": "rm -rf /tmp/scratch"})),
        assistant(text("cleaned")),
        result(),
    ]]
    engine = SdkNativeEngine(transport_factory=lambda: FakeTransport(script),
                             stream_watchdog_s=0.05)
    seen, emit = _collector()

    await engine.run_turn(session, "clean up", emit=emit, request_approval=slow_approve)

    assert not any(t == "error" for t, _ in seen)
    tr = next(p for t, p in seen if t == "tool_result")
    assert "rejected" not in tr["result"] and "error" not in tr["result"]
    assert [p["text"] for t, p in seen if t == "assistant_text"] == ["cleaned"]


async def test_simulate_mutating_command_is_a_noop(tmp_path):
    """SIMULATE end-to-end under the new engine: an approved mutating run_shell is ANNOUNCED
    (command event, simulated badge) but never executed — the same synthetic no-op result the
    old loop produced (the mechanism lives in shell.py, untouched)."""
    settings = Settings(_env_file=None, repos_dir=tmp_path / "repos",
                        workspace_dir=tmp_path / "ws", simulate=True)
    ctx = ToolContext(
        settings=settings,
        policy=CommandPolicy.from_file(settings.command_policy_path),
        runner=CommandRunner(settings.repo_paths),  # real runner: the SIMULATE no-op branch
        workspace=tmp_path / "ws",
    )
    session = Session(id="sim-t", ctx=ctx, catalog_injected=True)
    script = [[
        assistant(tool_use("tu_1", BT + "run_shell", {"command": "rm -rf /tmp/scratch"})),
        assistant(text("previewed")),
        result(),
    ]]
    engine, _fakes = _engine(script)
    seen, emit = _collector()
    await engine.run_turn(session, "clean up", emit=emit, request_approval=_approve)

    cmd = next(p for t, p in seen if t == "command")
    assert cmd["argv"] == ["bash", "-lc", "rm -rf /tmp/scratch"]
    assert cmd["mode"] == "mutating" and cmd["auto_run"] is False
    assert cmd["simulated"] is True and cmd["tool_call_id"] == "tu_1"
    tr = next(p for t, p in seen if t == "tool_result")
    assert tr["result"]["mode"] == "mutating" and tr["result"]["auto_run"] is False
    assert tr["result"]["exit_code"] == 0 and tr["result"]["timed_out"] is False
    assert "error" not in tr["result"] and "rejected" not in tr["result"]
