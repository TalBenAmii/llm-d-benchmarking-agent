"""Auto-approve toggle: the per-session "skip the Approve card for commands" switch.

Covers the Channel gate (kind=="command" is auto-approved; kind=="session_plan" is NEVER), and
the WS wiring (set_auto_approve flips + persists the session flag; the `ready` frame seeds it;
a malformed frame is rejected without killing the socket).
"""
from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient

from app.agent.channel import Channel
from app.agent.events import APPROVAL_REQUEST
from tests._helpers import _session


class _EmptyProvider:
    async def chat(self, *, system, messages, tools, cache_key=None):  # pragma: no cover - unused
        raise AssertionError("no turn is run in these tests")


# ---- the Channel gate ----------------------------------------------------

async def test_channel_auto_approves_commands_only_not_plan(tmp_path):
    session = _session(tmp_path)
    session.auto_approve = True
    session.ctx.current_tool_call_id = "tc-cmd"
    ch = Channel(session)

    # A COMMAND gate is auto-approved: returns True immediately, nothing parks, and the decision
    # is recorded (auto=True) so the transcript can replay an auto-approved card on resume.
    ok = await ch.request_approval("command", {"command": "kubectl apply -f x.yaml", "mode": "mutating"})
    assert ok is True
    assert ch.pending == {}                          # no Future minted, nothing to reconcile
    assert not any(f.get("type") == APPROVAL_REQUEST for f in ch._buffer)  # never prompted
    rec = session.approvals[-1]
    assert rec["tool_call_id"] == "tc-cmd" and rec["approved"] is True and rec.get("auto") is True

    # The SessionPlan gate STILL prompts even with auto_approve on (the one deliberate "are you
    # sure" stays). It parks on a Future + registers in pending; resolve it to clean up.
    session.ctx.current_tool_call_id = "tc-plan"
    task = asyncio.create_task(ch.request_approval("session_plan", {"spec": "cicd/kind"}))
    for _ in range(10):
        if ch.pending:
            break
        await asyncio.sleep(0)
    assert len(ch.pending) == 1                       # the plan gate DID park
    assert any(f.get("type") == APPROVAL_REQUEST for f in ch._buffer)
    rid = next(iter(ch.pending))
    ch.resolve(rid, True)
    assert await task is True


async def test_channel_command_gate_parks_when_auto_approve_off(tmp_path):
    # Control: with the toggle OFF a command gate parks for the user like always.
    session = _session(tmp_path)
    session.auto_approve = False
    session.ctx.current_tool_call_id = "tc"
    ch = Channel(session)

    task = asyncio.create_task(ch.request_approval("command", {"command": "rm -rf x", "mode": "mutating"}))
    for _ in range(10):
        if ch.pending:
            break
        await asyncio.sleep(0)
    assert len(ch.pending) == 1                       # not auto-approved → parked
    rid = next(iter(ch.pending))
    ch.resolve(rid, False)
    assert await task is False


# ---- the WS wiring -------------------------------------------------------

def _drain_for(ws, want_type, *, limit=40):
    """Return the first frame of ``want_type``, skipping background pre-probe noise."""
    for _ in range(limit):
        ev = ws.receive_json()
        if ev["type"] == want_type:
            return ev
    raise AssertionError(f"never saw a {want_type!r} frame")


def test_ws_set_auto_approve_roundtrip_and_persists():
    from app.main import app

    with TestClient(app) as client:
        app.state.provider = _EmptyProvider()
        with client.websocket_connect("/ws") as ws:
            ready = ws.receive_json()
            assert ready["type"] == "ready"
            sid = ready["data"]["session_id"]
            # The ready frame seeds the toggle; a fresh chat defaults OFF.
            assert ready["data"]["auto_approve"] is False

            # Turn it ON, then ping to force the receive loop to have processed it in order.
            ws.send_json({"type": "set_auto_approve", "enabled": True})
            ws.send_json({"type": "ping"})
            assert _drain_for(ws, "pong")

            sess = app.state.sessions.get(sid)
            assert sess.auto_approve is True
            # Persisted to disk (survives reconnect / re-seeds the button via `ready`).
            state = json.loads((sess.ctx.workspace / "state.json").read_text())
            assert state["auto_approve"] is True

            # Toggling back OFF flips + persists it the other way.
            ws.send_json({"type": "set_auto_approve", "enabled": False})
            ws.send_json({"type": "ping"})
            assert _drain_for(ws, "pong")
            assert app.state.sessions.get(sid).auto_approve is False


def test_ws_set_auto_approve_missing_enabled_is_rejected_socket_survives():
    from app.main import app

    with TestClient(app) as client:
        app.state.provider = _EmptyProvider()
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json()["type"] == "ready"
            # Missing the required `enabled` field → structured error, socket stays alive.
            ws.send_json({"type": "set_auto_approve"})
            assert _drain_for(ws, "error")
            # The connection is still usable afterwards.
            ws.send_json({"type": "ping"})
            assert _drain_for(ws, "pong")
