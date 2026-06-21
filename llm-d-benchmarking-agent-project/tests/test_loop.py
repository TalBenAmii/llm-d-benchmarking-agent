"""End-to-end agent loop test with a scripted fake provider.

Proves the loop wiring without an API key or running heavy commands:
- read-only tools auto-run,
- a SessionPlan is proposed and approved through the approval channel,
- a mutating command is GATED — when approval is declined it is rejected and never runs.
"""
from __future__ import annotations

import json
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


async def test_mid_thinking_steer_extends_the_same_turn(tmp_path):
    """Claude-Code steering at the loop level: a message queued onto ctx.steer_messages WHILE the
    agent is mid-turn (here: the instant it emits what would be its final, tool-less reply) is
    drained at the next step boundary and keeps the SAME turn alive, so the model answers the steer
    instead of the turn ending. This is the core behavior the WS handler relies on when it queues a
    message typed while the agent is 'thinking' (no approval gate open)."""
    if not get_settings().bench_repo.is_dir():
        pytest.skip("repo not present")

    turns = [
        # Step 1: a final, tool-less reply. WITHOUT a steer the turn ends right here.
        AssistantTurn(text="All set — anything else?", tool_calls=[]),
        # Step 2: only reached because the steer kept the turn alive; the model answers it.
        AssistantTurn(text="Sure — bumping to 1000 users.", tool_calls=[]),
    ]

    session = _session(tmp_path)
    events: list[tuple[str, dict]] = []
    injected = {"done": False}

    async def emit(t, p):
        events.append((t, p))
        # Stand in for the WS handler queueing a steer the moment the agent produces its
        # (would-be final) reply — i.e. the user typed while it was thinking. One-shot.
        if t == "assistant_text" and not injected["done"]:
            session.ctx.steer_messages.append("actually make it 1000 concurrent users")
            injected["done"] = True

    async def request_approval(kind, payload):
        raise AssertionError("no approval gate is expected in this turn")

    loop = AgentLoop(FakeProvider(turns))
    await loop.run_turn(session, "benchmark a tiny chat model", emit=emit, request_approval=request_approval)

    # The turn did NOT stop at step 1: the queued steer drove a second LLM call.
    assert loop._provider.i == 2, "the steer must have extended the turn to a second LLM call"
    # It was threaded into the transcript as a real user turn (so the model saw it).
    assert any(m.get("role") == "user" and m.get("content") == "actually make it 1000 concurrent users"
               for m in session.messages)
    # The queue was drained, not left dangling for a future turn.
    assert session.ctx.steer_messages == []
    assert events[-1][0] == "done"


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


async def test_schema_validation_error_does_not_break_the_loop(tmp_path):
    """A Pydantic *schema* validation failure that runs through a custom validator (one that
    ``raise``s ``ValueError``) must come back as a CLEAN, JSON-serializable tool error the model
    can self-correct from — not crash the loop.

    Trigger: the model calls ``propose_session_plan`` with an ``autotune`` knob whose ``max <=
    min``. ``AutotuneKnob``'s ``model_validator`` raises ``ValueError`` during
    ``SessionPlan.model_validate`` (so the handler never runs); ``dispatch`` turns the
    ``ValidationError`` into ``{"error": "invalid arguments", "details": ...}``. Pydantic's
    ``errors()`` embeds the raised ``ValueError`` OBJECT in each entry's ``ctx`` — a NON-JSON-
    serializable value. The loop then ``clamp_tool_result_content``-s every tool result with
    ``json.dumps``; an unsanitized ``details`` makes that raise ``TypeError`` OUTSIDE the
    per-tool ``_invoke`` guard, escaping ``run_turn`` after the assistant message (with its
    ``tool_calls``) was appended but BEFORE the matching ``tool_results`` block — an orphaned
    tool_call that poisons the next turn. The fix sanitizes ``details`` so the result serializes.
    """
    turns = [
        AssistantTurn(text="Plan with a search.", tool_calls=[ToolCall("c1", "propose_session_plan", {
            "use_case_summary": "x", "spec": "cicd/kind", "namespace": "ns",
            "harness": "inference-perf", "workload": "sanity_random.yaml",
            "autotune": {
                "strategy": "bisection", "objective": "ttft", "direction": "min", "budget": 5,
                # max <= min trips AutotuneKnob._check -> raise ValueError -> non-serializable ctx
                "knobs": [{"name": "c", "key": "max-concurrency", "min": 10.0, "max": 5.0}],
            },
        })]),
        AssistantTurn(text="Let me fix the bounds.", tool_calls=[]),
    ]
    events: list[tuple[str, dict]] = []

    async def emit(t, p):
        events.append((t, p))

    async def request_approval(kind, payload):
        raise AssertionError("approval must not be requested for a schema-invalid plan")

    session = _session(tmp_path)
    # Must NOT raise (the bug raised TypeError out of run_turn here).
    await AgentLoop(FakeProvider(turns)).run_turn(
        session, "tune it", emit=emit, request_approval=request_approval)

    # The turn reached a clean end (did not die mid-loop).
    assert events[-1][0] == "done"
    assert not any(t == "error" for (t, _) in events), "the loop must not surface an agent error"

    # The validation error came back as a clean, JSON-serializable tool result.
    plan_results = [p for (t, p) in events if t == "tool_result" and p["name"] == "propose_session_plan"]
    assert plan_results and plan_results[0]["result"].get("error") == "invalid arguments"
    json.dumps(plan_results[0]["result"])  # the whole result must serialize (incl. details)

    # The model got a SECOND step to self-correct (the loop continued past the bad call).
    assert AgentLoop  # keep import used
    assert any(m.get("role") == "assistant" and m.get("content") == "Let me fix the bounds."
               for m in session.messages)

    # Tool-call/result pairing intact: the assistant message that issued c1 is followed by a
    # tool_results block carrying c1 — no orphaned tool_call that would poison the next turn.
    asst = next((m for m in session.messages if m.get("role") == "assistant"
                 and any(tc.get("id") == "c1" for tc in m.get("tool_calls", []))), None)
    assert asst is not None
    results_blocks = [m for m in session.messages if m.get("role") == "tool_results"]
    assert any(r.get("tool_call_id") == "c1"
               for block in results_blocks for r in block.get("results", []))
    # Every persisted message round-trips through JSON (the transcript is replay-safe).
    json.dumps(session.messages)
