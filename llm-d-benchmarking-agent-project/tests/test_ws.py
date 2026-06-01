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

    async def chat(self, *, system, messages, tools):
        turn = self._turns[self.i]
        self.i += 1
        return turn


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
