"""Session token accounting: the engine accumulates each turn's usage into the persisted
session tally, and the totals survive persist()/load(). Hermetic (FakeTransport, no CLI)."""
from __future__ import annotations

import json

from app.agent import events
from app.agent.engine import SdkNativeEngine
from app.agent.session import Session, SessionManager
from app.config import Settings, get_settings
from app.security.policy import CommandPolicy
from app.security.runner import CommandRunner
from tests._helpers import COMMAND_POLICY_PATH, _capture_ctx
from tests._sdk_fake import FakeTransport, assistant, result, stream_event, text


def _usage_script(reply: str, usage: dict):
    """One text-only turn whose stream carries ``usage`` (message_start mirrors the input side,
    the ResultMessage is the authoritative session-total source)."""
    return [[
        stream_event({"type": "message_start", "message": {"usage": usage}}),
        stream_event({"type": "message_delta",
                      "usage": {"output_tokens": usage.get("output_tokens", 0)}}),
        assistant(text(reply)),
        result(usage=usage),
    ]]


async def test_engine_emits_usage_and_accumulates_session_totals(tmp_path):
    """Run two turns with known usage and assert the per-turn `usage` event math plus the
    cross-turn session accumulation (input/output/cache read+write, context_window)."""
    ctx, _runner = _capture_ctx(tmp_path)
    session = Session(id="tok", ctx=ctx, catalog_injected=True)
    captured: list[tuple[str, dict]] = []

    async def emit(t, p):
        captured.append((t, p))

    async def approve(kind, payload):
        return True

    u1 = {"input_tokens": 100, "output_tokens": 20,
          "cache_read_input_tokens": 900, "cache_creation_input_tokens": 10}
    engine1 = SdkNativeEngine(transport_factory=lambda: FakeTransport(_usage_script("hi", u1)))
    await engine1.run_turn(session, "hello", emit=emit, request_approval=approve)

    usage_events = [p for (t, p) in captured if t == events.USAGE]
    assert len(usage_events) == 1
    ue = usage_events[0]
    assert ue["turn"]["input"] == 100
    assert ue["turn"]["output"] == 20
    assert ue["turn"]["cache_read"] == 900
    assert ue["turn"]["cache_write"] == 10
    assert ue["turn"]["calls"] == 1
    # turn.total = total_input + output = (100+900+10) + 20
    assert ue["turn"]["total"] == 1030
    assert ue["session"]["total"] == 1030
    # context_window = total_input of THIS call (fresh+cache_read+cache_write), NOT the per-turn
    # sum and NOT including output. No model limit (model can change) — just the count.
    assert ue["context_window"]["tokens"] == 100 + 900 + 10
    assert "limit" not in ue["context_window"]
    assert session.last_context_tokens == 1010

    # Session cumulative fields after turn 1.
    assert session.total_input_tokens == 100
    assert session.total_output_tokens == 20
    assert session.total_cache_read_tokens == 900
    assert session.total_cache_write_tokens == 10
    assert session.session_total == 1030

    # Turn 2: another call, usage B — must ADD to the session tally.
    captured.clear()
    u2 = {"input_tokens": 50, "output_tokens": 5,
          "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 0}
    engine2 = SdkNativeEngine(transport_factory=lambda: FakeTransport(_usage_script("again", u2)))
    await engine2.run_turn(session, "again", emit=emit, request_approval=approve)

    ue2 = [p for (t, p) in captured if t == events.USAGE][-1]
    # turn totals reset per turn.
    assert ue2["turn"]["input"] == 50
    assert ue2["turn"]["calls"] == 1
    # context_window tracks the LATEST call only — it does NOT accumulate across turns.
    assert ue2["context_window"]["tokens"] == 50 + 1000 + 0
    assert session.last_context_tokens == 1050
    # session totals accumulated across both turns.
    assert session.total_input_tokens == 150
    assert session.total_output_tokens == 25
    assert session.total_cache_read_tokens == 1900
    assert session.total_cache_write_tokens == 10
    assert session.session_total == 2085
    assert ue2["session"]["total"] == 2085


async def test_session_token_totals_survive_persist_and_load(tmp_path):
    al = CommandPolicy.from_file(COMMAND_POLICY_PATH)
    runner = CommandRunner(get_settings().repo_paths)
    mgr2 = SessionManager(Settings(workspace_dir=tmp_path), al, runner)

    sess = mgr2.create()
    sess.messages.append({"role": "user", "content": "hi"})
    sess.total_input_tokens = 111
    sess.total_output_tokens = 22
    sess.total_cache_read_tokens = 3333
    sess.total_cache_write_tokens = 44
    sess.last_context_tokens = 5050
    sess.persist()

    # Drop from memory and reload from disk.
    mgr2._sessions.clear()
    loaded = mgr2.load(sess.id)
    assert loaded is not None
    assert loaded.total_input_tokens == 111
    assert loaded.total_output_tokens == 22
    assert loaded.total_cache_read_tokens == 3333
    assert loaded.total_cache_write_tokens == 44
    # The context-window meter is correct on reload before the next turn refreshes it.
    assert loaded.last_context_tokens == 5050
    assert loaded.session_total == 111 + 22 + 3333 + 44


def test_old_state_json_without_tokens_loads_as_zero(tmp_path):
    al = CommandPolicy.from_file(COMMAND_POLICY_PATH)
    runner = CommandRunner(get_settings().repo_paths)
    mgr = SessionManager(Settings(workspace_dir=tmp_path), al, runner)
    sess = mgr.create()
    sess.messages.append({"role": "user", "content": "hi"})
    sess.persist()
    # Simulate a pre-feature state.json by stripping the token fields.
    state = mgr._root / sess.id / "state.json"
    data = json.loads(state.read_text())
    for k in list(data):
        if k.startswith("total_") and k.endswith("_tokens"):
            del data[k]
    state.write_text(json.dumps(data))
    mgr._sessions.clear()
    loaded = mgr.load(sess.id)
    assert loaded is not None
    assert loaded.session_total == 0
