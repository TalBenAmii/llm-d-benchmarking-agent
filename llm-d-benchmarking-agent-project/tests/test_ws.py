"""WebSocket integration test of the real FastAPI wiring (main.py): event streaming and
the approval round-trip, driven by a fake provider injected into app.state."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.llm.provider import AssistantTurn, ToolCall


class FakeProvider:
    def __init__(self, turns):
        self._turns = turns
        self.i = 0

    async def chat(self, *, system, messages, tools, cache_key=None):
        turn = self._turns[self.i]
        self.i += 1
        return turn


# Frames the background environment pre-probe (W2) can stream onto a brand-new connection while
# we're asserting a specific protocol response: its auto-run read-only probes emit `command`
# events (and, on a real cluster, the run-time poller could emit `resource_stats`/`output`).
# Tests that read one expected frame after `ready` skip past this benign background noise.
_BACKGROUND_TYPES = {"command", "resource_stats", "output"}


def _next_protocol(ws):
    """The next frame that is part of the protocol exchange, skipping background pre-probe noise."""
    for _ in range(50):
        ev = ws.receive_json()
        if ev["type"] not in _BACKGROUND_TYPES:
            return ev
    raise AssertionError("only background frames received")


@pytest.mark.skipif(not get_settings().bench_repo.is_dir(), reason="repo not present")
def test_ws_approval_roundtrip():
    from app.main import app

    turns = [
        AssistantTurn(text="Checking catalog.", tool_calls=[ToolCall("c1", "list_catalog", {"kinds": ["harnesses"]})]),
        AssistantTurn(text="Plan:", tool_calls=[ToolCall("c2", "propose_session_plan", {
            "use_case_summary": "tiny chat", "spec": "cicd/kind", "namespace": "llmd-quickstart",
            "harness": "inference-perf", "workload": "sanity_random.yaml", "expected_steps": ["standup"],
        })]),
        AssistantTurn(text="Standing up.", tool_calls=[ToolCall("c3", "execute_llmdbenchmark", {
            "subcommand": "standup", "spec": "cicd/kind", "namespace": "llmd-quickstart",
            "flags": {"skip_smoketest": True},
        })]),
        AssistantTurn(text="Rejected, understood.", tool_calls=[]),
    ]

    with TestClient(app) as client:
        # Inject the fake provider after startup.
        app.state.provider = FakeProvider(turns)

        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json()["type"] == "ready"
            ws.send_json({"type": "user_message", "text": "benchmark a tiny chat model"})

            seen: list[str] = []
            approvals_answered = 0
            for _ in range(80):
                ev = ws.receive_json()
                seen.append(ev["type"])
                if ev["type"] == "approval_request":
                    data = ev["data"]
                    # approve the plan, reject the mutating command
                    ws.send_json({
                        "type": "approval",
                        "request_id": data["request_id"],
                        "approved": data["kind"] == "session_plan",
                    })
                    approvals_answered += 1
                if ev["type"] == "done":
                    break

        assert "approval_request" in seen
        assert approvals_answered >= 2          # plan + command
        assert "tool_result" in seen
        assert seen[-1] == "done"


@pytest.mark.skipif(not get_settings().bench_repo.is_dir(), reason="repo not present")
def test_ws_approval_decisions_persist_and_replay():
    """A decided approval (Approve/Reject) is persisted and replayed as an `approval_decision`
    item — tied to its tool call — when the chat is reopened, so it doesn't vanish on a chat
    switch / reload."""
    from app.main import app

    turns = [
        AssistantTurn(text="Plan:", tool_calls=[ToolCall("c2", "propose_session_plan", {
            "use_case_summary": "tiny chat", "spec": "cicd/kind", "namespace": "llmd-quickstart",
            "harness": "inference-perf", "workload": "sanity_random.yaml", "expected_steps": ["standup"],
        })]),
        AssistantTurn(text="Standing up.", tool_calls=[ToolCall("c3", "execute_llmdbenchmark", {
            "subcommand": "standup", "spec": "cicd/kind", "namespace": "llmd-quickstart",
            "flags": {"skip_smoketest": True},
        })]),
        AssistantTurn(text="Understood.", tool_calls=[]),
    ]

    with TestClient(app) as client:
        app.state.provider = FakeProvider(turns)

        with client.websocket_connect("/ws") as ws:
            ready = ws.receive_json()
            assert ready["type"] == "ready"
            sid = ready["data"]["session_id"]
            ws.send_json({"type": "user_message", "text": "benchmark a tiny chat model"})
            for _ in range(80):
                ev = ws.receive_json()
                if ev["type"] == "approval_request":
                    d = ev["data"]
                    # approve the plan, reject the mutating command
                    ws.send_json({"type": "approval", "request_id": d["request_id"],
                                  "approved": d["kind"] == "session_plan"})
                if ev["type"] == "done":
                    break

        # Both decisions are persisted on the session, each tied to its tool call.
        s = app.state.sessions.get(sid)
        by_tc = {a["tool_call_id"]: a for a in s.approvals}
        assert by_tc.get("c2", {}).get("kind") == "session_plan" and by_tc["c2"]["approved"] is True
        assert by_tc.get("c3", {}).get("kind") == "command" and by_tc["c3"]["approved"] is False

        # Reopening the chat replays them as approval_decision items, each right after its tool_call.
        with client.websocket_connect(f"/ws?session={sid}") as ws2:
            items = None
            for _ in range(20):
                ev = ws2.receive_json()
                if ev["type"] == "history":
                    items = ev["data"]["items"]
                    break
        assert items is not None, "no history replayed on reconnect"
        decisions = [it for it in items if it["role"] == "approval_decision"]
        assert any(it["kind"] == "session_plan" and it["approved"] is True for it in decisions)
        assert any(it["kind"] == "command" and it["approved"] is False for it in decisions)
        for i, it in enumerate(items):
            if it["role"] == "approval_decision":
                assert i > 0 and items[i - 1]["role"] == "tool_call", \
                    "decision must replay directly after its tool_call"


# --- Phase 15: WS protocol hardening + live event buffer -------------------------------


@pytest.mark.skipif(not get_settings().bench_repo.is_dir(), reason="repo not present")
def test_ws_malformed_frame_rejected_socket_survives():
    """A malformed inbound frame is rejected with a structured `error` (kind=protocol_error)
    and the connection stays alive: a well-formed `ping` immediately after still gets a
    `pong`. The handler must never crash on a bad/hostile frame."""
    from app.main import app

    # Bind a real (fake) provider BEFORE connecting: the /ws handler snapshots app.state.provider
    # into its AgentLoop at handshake, so step (5)'s user_message drives a real turn.
    with TestClient(app) as client:
        app.state.provider = FakeProvider([AssistantTurn(text="hi there", tool_calls=[])])
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json()["type"] == "ready"
            # A brand-new connection now emits a DETERMINISTIC `welcome` card (B2) then the
            # start-of-chat suggestion chips (W1) right after `ready`, and may stream background
            # pre-probe `command` events (W2). `_next_protocol` skips that benign noise so we
            # assert the real protocol responses below.
            assert _next_protocol(ws)["type"] == "welcome"
            assert _next_protocol(ws)["type"] == "suggestions"

            # (1) unknown message type -> structured protocol error, socket alive.
            ws.send_json({"type": "totally_bogus", "text": "x"})
            err = _next_protocol(ws)
            assert err["type"] == "error"
            assert err["data"].get("kind") == "protocol_error"
            assert "malformed message" in err["data"]["message"]

            # (1b) a raw NON-JSON text frame (the most basic malformed frame) -> structured
            # protocol error, socket KEPT ALIVE. This guards the JSON-decode layer itself:
            # `receive_json()` would raise json.JSONDecodeError straight out of the handler and
            # tear the socket down, violating the spec's "do NOT crash the handler" requirement.
            ws.send_text("this is not json {{{")
            err_nonjson = _next_protocol(ws)
            assert err_nonjson["type"] == "error"
            assert err_nonjson["data"].get("kind") == "protocol_error"
            assert "malformed message" in err_nonjson["data"]["message"]

            # (1c) an empty text frame is likewise non-JSON -> structured error, still alive.
            ws.send_text("")
            err_empty = _next_protocol(ws)
            assert err_empty["type"] == "error"
            assert err_empty["data"].get("kind") == "protocol_error"

            # (1d) a binary frame carries no JSON text payload -> structured error, still alive.
            ws.send_bytes(b"\x00\x01\x02 not a frame")
            err_bin = _next_protocol(ws)
            assert err_bin["type"] == "error"
            assert err_bin["data"].get("kind") == "protocol_error"

            # (2) a non-dict JSON frame (a bare list) -> structured error, still alive.
            ws.send_json(["not", "an", "object"])
            err2 = _next_protocol(ws)
            assert err2["type"] == "error" and err2["data"].get("kind") == "protocol_error"

            # (3) right type but a malformed body (missing required `request_id`) -> error.
            ws.send_json({"type": "approval", "approved": True})
            err3 = _next_protocol(ws)
            assert err3["type"] == "error" and err3["data"].get("kind") == "protocol_error"
            assert "request_id" in err3["data"]["message"]

            # (4) the socket is STILL usable: a valid ping is answered with a pong.
            ws.send_json({"type": "ping"})
            assert _next_protocol(ws)["type"] == "pong"

            # (5) and a valid user_message still drives a real turn afterwards.
            ws.send_json({"type": "user_message", "text": "hello"})
            seen = []
            for _ in range(20):
                ev = ws.receive_json()
                seen.append(ev["type"])
                if ev["type"] == "done":
                    break
            assert "assistant_text" in seen and seen[-1] == "done"


@pytest.mark.skipif(not get_settings().bench_repo.is_dir(), reason="repo not present")
def test_ws_reconnect_midturn_replays_live_events():
    """A client that disconnects mid-turn (while the turn is parked at the plan-approval gate)
    and reconnects with `?session=<id>` receives the LIVE events it missed — the assistant
    text + the tool_call that streamed before it dropped — replayed from the per-turn buffer,
    plus the still-pending approval card re-surfaced. It can then answer the approval and the
    SAME turn continues to `done` (not a replayed end-state)."""
    from app.main import app

    turns = [
        AssistantTurn(text="Here is the plan.", tool_calls=[ToolCall("c1", "propose_session_plan", {
            "use_case_summary": "tiny chat", "spec": "cicd/kind", "namespace": "llmd-quickstart",
            "harness": "inference-perf", "workload": "sanity_random.yaml", "expected_steps": ["standup"],
        })]),
        AssistantTurn(text="Plan approved — all set.", tool_calls=[]),
    ]

    with TestClient(app) as client:
        app.state.provider = FakeProvider(turns)

        # Connection #1: start the turn, drive it until it parks at the approval gate, then
        # DISCONNECT without answering (leave the context manager) — the turn stays parked.
        with client.websocket_connect("/ws") as ws1:
            ready = ws1.receive_json()
            assert ready["type"] == "ready"
            sid = ready["data"]["session_id"]
            ws1.send_json({"type": "user_message", "text": "benchmark a tiny chat model"})
            saw_text = saw_tool = False
            for _ in range(40):
                ev = ws1.receive_json()
                if ev["type"] == "assistant_text" and ev["data"]["text"] == "Here is the plan.":
                    saw_text = True
                if ev["type"] == "tool_call" and ev["data"]["name"] == "propose_session_plan":
                    saw_tool = True
                if ev["type"] == "approval_request":
                    # Parked at the gate. Drop the connection WITHOUT answering.
                    break
            assert saw_text and saw_tool, "did not stream the live events before disconnect"

        # The turn is still running (parked at the approval), so it's in app.state.running.
        running = app.state.running.get(sid)
        assert running is not None and not running.done(), "turn should still be parked, not finished"

        # Connection #2: reconnect to the same session mid-turn. We must REPLAY the live events
        # the dropped client missed (the assistant text + the tool_call), then re-surface the
        # pending approval card — NOT just wait for the final result.
        with client.websocket_connect(f"/ws?session={sid}") as ws2:
            replayed_text = replayed_tool = re_approval = False
            approval_rid = None
            for _ in range(40):
                ev = ws2.receive_json()
                if ev["type"] == "assistant_text" and ev["data"]["text"] == "Here is the plan.":
                    replayed_text = True
                if ev["type"] == "tool_call" and ev["data"]["name"] == "propose_session_plan":
                    replayed_tool = True
                if ev["type"] == "approval_request":
                    re_approval = True
                    approval_rid = ev["data"]["request_id"]
                    break
            assert replayed_text, "missed live assistant_text was not replayed on reconnect"
            assert replayed_tool, "missed live tool_call was not replayed on reconnect"
            assert re_approval, "pending approval was not re-surfaced on reconnect"

            # Answer the re-surfaced approval; the SAME parked turn continues live to done.
            ws2.send_json({"type": "approval", "request_id": approval_rid, "approved": True})
            seen_after = []
            for _ in range(40):
                ev = ws2.receive_json()
                seen_after.append(ev["type"])
                if ev["type"] == "done":
                    break
            assert "tool_result" in seen_after, "turn did not continue live after the approval"
            assert seen_after[-1] == "done", "the same turn must run to completion live"


@pytest.mark.skipif(not get_settings().bench_repo.is_dir(), reason="repo not present")
def test_ws_reconnect_does_not_double_send_approval():
    """Regression for the buffer/pending interplay: on reconnect mid-turn the live replay must
    skip buffered approval_request frames (they're owned by reemit_pending), so the client sees
    exactly ONE approval card for the single pending gate, not a duplicate."""
    from app.main import app

    turns = [
        AssistantTurn(text="Plan.", tool_calls=[ToolCall("c1", "propose_session_plan", {
            "use_case_summary": "tiny chat", "spec": "cicd/kind", "namespace": "llmd-quickstart",
            "harness": "inference-perf", "workload": "sanity_random.yaml", "expected_steps": ["standup"],
        })]),
        AssistantTurn(text="done.", tool_calls=[]),
    ]
    with TestClient(app) as client:
        app.state.provider = FakeProvider(turns)
        with client.websocket_connect("/ws") as ws1:
            sid = ws1.receive_json()["data"]["session_id"]
            ws1.send_json({"type": "user_message", "text": "go"})
            for _ in range(40):
                if ws1.receive_json()["type"] == "approval_request":
                    break
        with client.websocket_connect(f"/ws?session={sid}") as ws2:
            approvals = 0
            rid = None
            # Drain everything the server pushes on reconnect (history + live replay + pending).
            for _ in range(40):
                ev = ws2.receive_json()
                if ev["type"] == "approval_request":
                    approvals += 1
                    rid = ev["data"]["request_id"]
                    break
            assert approvals == 1, f"exactly one approval card expected on reconnect, got {approvals}"
            # Finish cleanly so no background task is left parked.
            ws2.send_json({"type": "approval", "request_id": rid, "approved": True})
            for _ in range(40):
                if ws2.receive_json()["type"] == "done":
                    break


def test_channel_live_buffer_is_bounded():
    """The per-turn live buffer is a BOUNDED ring: appending more events than its cap keeps
    only the most recent `buffer_max`, so a long, chatty turn can't grow memory without limit.
    Driven directly against the Channel (no cluster, no TestClient)."""
    import asyncio

    from app.agent.channel import Channel

    class _Sess:
        # Minimal stand-in: emit() only touches record_command for `command` events, which we
        # don't emit here, so a bare object with an id is enough for the buffer mechanism.
        id = "buftest"

    async def _drive() -> Channel:
        ch = Channel(_Sess(), buffer_max=10)
        ch.begin_turn()
        # Emit far more than the cap; ch.ws is None so nothing is sent — pure buffering.
        for i in range(100):
            await ch.emit("output", {"line": f"line-{i}"})
        return ch

    ch = asyncio.run(_drive())
    buffered = ch.buffered_events
    assert len(buffered) == 10, "buffer must be capped at buffer_max, not grow unbounded"
    # It keeps the MOST RECENT events (oldest fall off the ring).
    lines = [f["data"]["line"] for f in buffered]
    assert lines == [f"line-{i}" for i in range(90, 100)]
    # Every buffered frame is the canonical outbound envelope plus a resume `seq` cursor.
    assert all(set(f) == {"type", "data", "seq"} and f["type"] == "output" for f in buffered)


def test_channel_begin_turn_resets_buffer():
    """begin_turn() clears the buffer so a reconnecting client replays only the CURRENT turn,
    never a stale prior turn's tail; end_turn() flips the live flag off."""
    import asyncio

    from app.agent.channel import Channel

    class _Sess:
        id = "resettest"

    async def _drive():
        ch = Channel(_Sess(), buffer_max=50)
        ch.begin_turn()
        await ch.emit("assistant_text", {"text": "turn-1"})
        assert ch.turn_active is True
        ch.end_turn()
        assert ch.turn_active is False
        # A new turn must start from an empty buffer.
        ch.begin_turn()
        assert ch.buffered_events == []
        await ch.emit("assistant_text", {"text": "turn-2"})
        return ch

    ch = asyncio.run(_drive())
    texts = [f["data"]["text"] for f in ch.buffered_events]
    assert texts == ["turn-2"], "old turn's events must not linger after begin_turn()"


def test_channel_buffer_excludes_lifecycle_frames():
    """Connection-lifecycle frames (ready/history/pong) the handler emits on every (re)connect
    are NOT buffered into the per-turn live ring — only true turn events are. Otherwise a SECOND
    mid-turn reconnect would replay a stale ready/history/pong interleaved before the real missed
    turn events. The buffer holds 'only the in-flight turn's events', as its docstring promises."""
    import asyncio

    from app.agent import events
    from app.agent.channel import Channel

    class _Sess:
        id = "lifecycletest"

    async def _drive():
        ch = Channel(_Sess(), buffer_max=50)
        ch.begin_turn()
        # A real turn event, then the lifecycle frames the /ws handler emits on a mid-turn
        # reconnect (ready + history), and a pong for a keep-alive ping, then another turn event.
        await ch.emit(events.ASSISTANT_TEXT, {"text": "missed-1"})
        await ch.emit(events.READY, {"session_id": "lifecycletest"})
        await ch.emit(events.HISTORY, {"items": [], "commands": []})
        await ch.emit(events.PONG, {})
        await ch.emit(events.TOOL_CALL, {"id": "t1", "name": "x", "input": {}})
        return ch

    ch = asyncio.run(_drive())
    types = [f["type"] for f in ch.buffered_events]
    # Only the turn events are buffered; lifecycle frames are excluded.
    assert types == [events.ASSISTANT_TEXT, events.TOOL_CALL]
    assert events.READY not in types
    assert events.HISTORY not in types
    assert events.PONG not in types


# --- W1: start-of-chat suggestion chips ------------------------------------


@pytest.mark.skipif(not get_settings().bench_repo.is_dir(), reason="repo not present")
def test_ws_new_connection_emits_suggestions_after_ready():
    """A brand-new /ws connection emits a `suggestions` event (non-empty {label,prompt} chips)
    right after `ready`. A resumed connection (?session=<existing>) emits NO suggestions."""
    from app.main import app

    with TestClient(app) as client:
        app.state.provider = FakeProvider([AssistantTurn(text="hi", tool_calls=[])])

        # Brand-new chat: ready, then the deterministic welcome card, then suggestions.
        with client.websocket_connect("/ws") as ws:
            ready = ws.receive_json()
            assert ready["type"] == "ready" and ready["data"]["resumed"] is False
            sid = ready["data"]["session_id"]
            welcome = ws.receive_json()
            assert welcome["type"] == "welcome"
            wd = welcome["data"]
            assert isinstance(wd.get("bullets"), list) and wd["bullets"]
            assert wd.get("heading")
            sugg = ws.receive_json()
            assert sugg["type"] == "suggestions"
            chips = sugg["data"]["chips"]
            assert chips and all(c.get("label") and c.get("prompt") for c in chips)
            # Drive one turn so the session is persisted on disk and can be resumed below.
            ws.send_json({"type": "user_message", "text": "hello"})
            for _ in range(20):
                if ws.receive_json()["type"] == "done":
                    break

        # Resumed chat: ready (resumed True) is followed by history — never suggestions. The
        # handler emits exactly ready+history with no turn running, so the frame right after
        # ready must be `history` (the suggestions branch is gated on `not resumed`). A `ping`
        # round-trip then confirms no stray frame (e.g. a suggestions) is queued ahead of `pong`.
        with client.websocket_connect(f"/ws?session={sid}") as ws2:
            ready2 = ws2.receive_json()
            assert ready2["type"] == "ready" and ready2["data"]["resumed"] is True
            assert ws2.receive_json()["type"] == "history"
            ws2.send_json({"type": "ping"})
            assert ws2.receive_json()["type"] == "pong"


# --- chat-switch state: server-authoritative elapsed + resume cursor -------


def test_channel_elapsed_ms_tracks_turn():
    """`elapsed_ms` is None outside a turn, a small non-negative int that grows during one, and
    None again after end_turn() — the server-authoritative clock the client seeds its timer from."""
    import asyncio
    import time

    from app.agent.channel import Channel

    class _Sess:
        id = "elapsed"

    async def _drive():
        ch = Channel(_Sess())
        assert ch.elapsed_ms is None, "no turn yet -> None"
        ch.begin_turn()
        e1 = ch.elapsed_ms
        assert isinstance(e1, int) and e1 >= 0
        time.sleep(0.02)
        e2 = ch.elapsed_ms
        assert e2 >= e1, "elapsed must not go backwards"
        ch.end_turn()
        assert ch.elapsed_ms is None, "after a turn ends -> None"

    asyncio.run(_drive())


def test_channel_seq_monotonic_and_window():
    """Turn events are stamped with a channel-lifetime, strictly increasing `seq` (the resume
    cursor); lifecycle frames carry none; begin_turn() clears the buffer but does NOT reset seq,
    so a cursor from a prior turn reliably predates the new turn's min_buffered_seq."""
    import asyncio

    from app.agent import events
    from app.agent.channel import Channel

    class _Sess:
        id = "seq"

    async def _drive() -> Channel:
        ch = Channel(_Sess(), buffer_max=50)
        ch.begin_turn()
        await ch.emit("assistant_text", {"text": "a"})   # seq 1
        await ch.emit("tool_call", {"id": "t", "name": "x", "input": {}})  # seq 2
        await ch.emit(events.PONG, {})                     # lifecycle: no seq, no advance
        await ch.emit("assistant_text", {"text": "b"})    # seq 3
        return ch

    ch = asyncio.run(_drive())
    seqs = [f["seq"] for f in ch.buffered_events]
    assert seqs == [1, 2, 3], "buffered turn frames carry a strictly increasing seq"
    assert ch.cur_seq == 3 and ch.min_buffered_seq == 1

    # A second turn clears the buffer but the cursor keeps climbing (it is NOT reset in
    # begin_turn). At a clean boundary a prior cursor sits exactly at min_buffered_seq-1, so it
    # remains resumable — the next turn's events simply append to the cached view.
    async def _next() -> Channel:
        ch.begin_turn()
        assert ch.buffered_events == []
        await ch.emit("assistant_text", {"text": "c"})    # seq 4
        return ch

    asyncio.run(_next())
    assert ch.cur_seq == 4 and ch.min_buffered_seq == 4

    # Overflow is what truly pushes a cursor out of the resumable window: a small ring drops the
    # oldest events, advancing min_buffered_seq, so a stale cursor below it must full-rebuild.
    async def _overflow() -> Channel:
        ch2 = Channel(_Sess(), buffer_max=3)
        ch2.begin_turn()
        for i in range(6):
            await ch2.emit("output", {"line": str(i)})    # seq 1..6, ring keeps the last 3
        return ch2

    ch2 = asyncio.run(_overflow())
    assert ch2.cur_seq == 6 and ch2.min_buffered_seq == 4, "oldest events fell off the ring"
    assert not (ch2.min_buffered_seq - 1 <= 1 <= ch2.cur_seq), "stale cursor 1 is out of window"
    assert ch2.min_buffered_seq - 1 <= 5 <= ch2.cur_seq, "a fresh cursor stays resumable"


def test_channel_replay_after_seq_sends_only_tail():
    """replay_live(after_seq) patches a cached view with ONLY the frames past the cursor, and
    still skips approval_request frames (owned by reemit_pending). after_seq=None replays all."""
    import asyncio

    from app.agent.channel import Channel

    class _Sess:
        id = "replay"

    class _FakeWS:
        def __init__(self):
            self.sent = []

        async def send_json(self, frame):
            self.sent.append(frame)

    async def _drive():
        ch = Channel(_Sess(), buffer_max=50)
        ch.begin_turn()
        await ch.emit("assistant_text", {"text": "a"})    # seq 1
        await ch.emit("approval_request", {"request_id": "r", "kind": "k", "payload": {}})  # seq 2
        await ch.emit("output", {"line": "o"})            # seq 3
        ws = _FakeWS()
        ch.ws = ws
        await ch.replay_live(after_seq=1)                  # only seq>1, minus approvals
        return ws

    ws = asyncio.run(_drive())
    types = [f["type"] for f in ws.sent]
    assert types == ["output"], "only seq>after_seq non-approval frames replay"
    assert all(f["seq"] > 1 for f in ws.sent)


@pytest.mark.skipif(not get_settings().bench_repo.is_dir(), reason="repo not present")
def test_ws_ready_reports_running_elapsed():
    """`ready` carries `running_elapsed_ms`: None for an idle/brand-new chat, and a non-negative
    int when reconnecting to a chat whose turn is still in flight (parked at an approval gate)."""
    from app.main import app

    turns = [
        AssistantTurn(text="Plan.", tool_calls=[ToolCall("c1", "propose_session_plan", {
            "use_case_summary": "tiny chat", "spec": "cicd/kind", "namespace": "llmd-quickstart",
            "harness": "inference-perf", "workload": "sanity_random.yaml", "expected_steps": ["standup"],
        })]),
        AssistantTurn(text="done.", tool_calls=[]),
    ]
    with TestClient(app) as client:
        app.state.provider = FakeProvider(turns)

        with client.websocket_connect("/ws") as ws1:
            ready1 = ws1.receive_json()
            assert ready1["type"] == "ready"
            assert ready1["data"]["running"] is False
            assert ready1["data"]["running_elapsed_ms"] is None, "idle chat reports no elapsed"
            sid = ready1["data"]["session_id"]
            ws1.send_json({"type": "user_message", "text": "go"})
            for _ in range(40):
                if ws1.receive_json()["type"] == "approval_request":
                    break  # parked at the gate; drop without answering

        # Reconnect mid-turn: ready must report the turn as running with real elapsed time.
        with client.websocket_connect(f"/ws?session={sid}") as ws2:
            ready2 = ws2.receive_json()
            assert ready2["type"] == "ready" and ready2["data"]["running"] is True
            elapsed = ready2["data"]["running_elapsed_ms"]
            assert isinstance(elapsed, int) and elapsed >= 0, "running chat reports elapsed ms"
            # Finish cleanly so no background task is left parked.
            rid = None
            for _ in range(40):
                ev = ws2.receive_json()
                if ev["type"] == "approval_request":
                    rid = ev["data"]["request_id"]
                    break
            ws2.send_json({"type": "approval", "request_id": rid, "approved": True})
            for _ in range(40):
                if ws2.receive_json()["type"] == "done":
                    break


@pytest.mark.skipif(not get_settings().bench_repo.is_dir(), reason="repo not present")
def test_ws_incremental_resume_skips_history_no_duplicates():
    """Reconnecting with ?after_seq=<cursor> while the turn is still in flight PATCHES the cached
    view: the server sends NO history and does NOT re-replay events at/under the cursor (no
    duplicate assistant_text/tool_call). resume.incremental is True. The pending approval still
    re-surfaces (it's cursor-independent)."""
    from app.main import app

    turns = [
        AssistantTurn(text="Here is the plan.", tool_calls=[ToolCall("c1", "propose_session_plan", {
            "use_case_summary": "tiny chat", "spec": "cicd/kind", "namespace": "llmd-quickstart",
            "harness": "inference-perf", "workload": "sanity_random.yaml", "expected_steps": ["standup"],
        })]),
        AssistantTurn(text="Plan approved.", tool_calls=[]),
    ]
    with TestClient(app) as client:
        app.state.provider = FakeProvider(turns)

        last_seq = 0
        with client.websocket_connect("/ws") as ws1:
            ready = ws1.receive_json()
            sid = ready["data"]["session_id"]
            ws1.send_json({"type": "user_message", "text": "benchmark a tiny chat model"})
            for _ in range(40):
                ev = ws1.receive_json()
                if "seq" in ev:
                    last_seq = max(last_seq, ev["seq"])
                if ev["type"] == "approval_request":
                    break  # parked at the gate; we've seen everything up to last_seq
        assert last_seq > 0, "live turn events should have carried a seq cursor"

        with client.websocket_connect(f"/ws?session={sid}&after_seq={last_seq}") as ws2:
            ready2 = ws2.receive_json()
            assert ready2["type"] == "ready"
            assert ready2["data"]["resume"]["incremental"] is True
            saw_history = saw_dupe = saw_reapproval = False
            for _ in range(20):
                ev = ws2.receive_json()
                if ev["type"] == "history":
                    saw_history = True
                if ev["type"] in ("assistant_text", "tool_call") and ev.get("seq", 0) <= last_seq:
                    saw_dupe = True
                if ev["type"] == "approval_request":
                    saw_reapproval = True
                    rid = ev["data"]["request_id"]
                    break
            assert not saw_history, "incremental resume must NOT resend history"
            assert not saw_dupe, "events at/under the cursor must not be replayed again"
            assert saw_reapproval, "the still-pending approval should re-surface"
            ws2.send_json({"type": "approval", "request_id": rid, "approved": True})
            for _ in range(40):
                if ws2.receive_json()["type"] == "done":
                    break


@pytest.mark.skipif(not get_settings().bench_repo.is_dir(), reason="repo not present")
def test_ws_stale_cursor_falls_back_to_full_history():
    """A resume cursor outside the retained buffer window (here: beyond the head — what a client
    with a stale-high cursor would send) is NOT incremental: the server falls back to a full
    history rebuild so the client never silently misses events."""
    from app.main import app

    turns = [AssistantTurn(text="hi", tool_calls=[])]
    with TestClient(app) as client:
        app.state.provider = FakeProvider(turns)
        with client.websocket_connect("/ws") as ws1:
            sid = ws1.receive_json()["data"]["session_id"]
            ws1.send_json({"type": "user_message", "text": "hello"})
            for _ in range(20):
                if ws1.receive_json()["type"] == "done":
                    break

        # Reconnect with a bogus, too-high cursor → out of window → full rebuild (history present).
        with client.websocket_connect(f"/ws?session={sid}&after_seq=999999") as ws2:
            ready2 = ws2.receive_json()
            assert ready2["type"] == "ready" and ready2["data"]["resume"]["incremental"] is False
            assert _next_protocol(ws2)["type"] == "history", "stale cursor must fall back to history"
