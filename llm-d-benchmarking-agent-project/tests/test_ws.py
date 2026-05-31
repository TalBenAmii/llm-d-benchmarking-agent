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
