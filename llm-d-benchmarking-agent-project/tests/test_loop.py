"""End-to-end agent loop test with a scripted fake provider.

Proves the loop wiring without an API key or running heavy commands:
- read-only tools auto-run,
- a SessionPlan is proposed and approved through the approval channel,
- a mutating command is GATED — when approval is declined it is rejected and never runs.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.agent.loop import AgentLoop
from app.agent.session import Session
from app.config import get_settings
from app.llm.provider import AssistantTurn, ToolCall
from app.security.allowlist import Allowlist
from app.security.runner import CommandRunner
from app.tools.context import ToolContext

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeProvider:
    def __init__(self, turns):
        self._turns = turns
        self.i = 0

    async def chat(self, *, system, messages, tools, cache_key=None):
        turn = self._turns[self.i]
        self.i += 1
        return turn


def _session(tmp_path) -> Session:
    s = get_settings()
    al = Allowlist.from_file(PROJECT_ROOT / "security" / "allowlist.yaml")
    runner = CommandRunner(s.repo_paths)
    ctx = ToolContext(settings=s, allowlist=al, runner=runner, workspace=tmp_path / "ws")
    return Session(id="t", ctx=ctx)


async def test_full_loop_with_gating(tmp_path):
    if not get_settings().bench_repo.is_dir():
        pytest.skip("repo not present")

    turns = [
        AssistantTurn(text="Checking the catalog.", tool_calls=[ToolCall("c1", "list_catalog", {"kinds": ["harnesses"]})]),
        AssistantTurn(text="Here's my plan.", tool_calls=[ToolCall("c2", "propose_session_plan", {
            "use_case_summary": "tiny chat benchmark on laptop",
            "spec": "cicd/kind", "namespace": "llmd-quickstart",
            "harness": "inference-perf", "workload": "sanity_random.yaml",
            "expected_steps": ["standup", "run"],
        })]),
        AssistantTurn(text="Standing up the stack.", tool_calls=[ToolCall("c3", "execute_llmdbenchmark", {
            "subcommand": "standup", "spec": "cicd/kind", "namespace": "llmd-quickstart",
            "flags": {"skip_smoketest": True},
        })]),
        AssistantTurn(text="Understood — it was rejected.", tool_calls=[]),
    ]

    events: list[tuple[str, dict]] = []
    approval_calls: list[str] = []

    async def emit(t, p):
        events.append((t, p))

    async def request_approval(kind, payload):
        # Approve the plan; reject the mutating command (so nothing actually runs).
        approval_calls.append(kind)
        return kind == "session_plan"

    session = _session(tmp_path)
    loop = AgentLoop(FakeProvider(turns))
    await loop.run_turn(session, "benchmark a tiny chat model", emit=emit, request_approval=request_approval)

    kinds = [e[0] for e in events]
    assert kinds[-1] == "done"

    # list_catalog ran and returned harnesses
    cat_results = [p for (t, p) in events if t == "tool_result" and p["name"] == "list_catalog"]
    assert cat_results and "inference-perf" in cat_results[0]["result"]["harnesses"]

    # the plan was approved and captured on the session
    assert session.approved_plan is not None
    assert session.approved_plan["spec"] == "cicd/kind"
    # approving the plan adopts its namespace as the chat's sidebar folder (was unset → filled)
    assert session.namespace == "llmd-quickstart"

    # the standup command was GATED and rejected — never executed
    standup_results = [p for (t, p) in events if t == "tool_result" and p["name"] == "execute_llmdbenchmark"]
    assert standup_results and standup_results[0]["result"].get("rejected") is True

    # an approval was requested for both the plan and the command (in that order)
    assert "session_plan" in approval_calls and "command" in approval_calls


async def test_invalid_plan_is_reported_not_executed(tmp_path):
    if not get_settings().bench_repo.is_dir():
        pytest.skip("repo not present")

    turns = [
        AssistantTurn(text="Plan.", tool_calls=[ToolCall("c1", "propose_session_plan", {
            "use_case_summary": "x", "spec": "guides/does-not-exist", "namespace": "ns",
            "harness": "nope", "workload": "sanity_random.yaml",
        })]),
        AssistantTurn(text="Let me fix that.", tool_calls=[]),
    ]
    events = []

    async def emit(t, p):
        events.append((t, p))

    async def request_approval(kind, payload):
        raise AssertionError("approval should not be requested for an invalid plan")

    session = _session(tmp_path)
    await AgentLoop(FakeProvider(turns)).run_turn(session, "go", emit=emit, request_approval=request_approval)

    plan_results = [p for (t, p) in events if t == "tool_result" and p["name"] == "propose_session_plan"]
    assert plan_results and plan_results[0]["result"]["valid"] is False
    assert session.approved_plan is None
