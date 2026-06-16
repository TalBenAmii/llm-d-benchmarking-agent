"""QA-fix infra tests: the agent loop's abandoned-turn cancellation guard.

Finding sim-1 00:40: after the WebSocket client disconnected, the loop kept calling tools
and ran to completion ~89s later, burning API tokens with no recipient. The loop now polls an
optional ``should_continue()`` predicate between steps and STOPS cleanly when it reports the
turn is abandoned — never mid-tool, and only between steps (so it also doubles as the
mid-workflow yield checkpoint of AGENT_FINDINGS 01:36).
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


class CountingProvider:
    """Records how many times the loop asked the model for another step."""

    def __init__(self, turns):
        self._turns = turns
        self.calls = 0

    async def chat(self, *, system, messages, tools, cache_key=None):
        turn = self._turns[self.calls]
        self.calls += 1
        return turn


def _session(tmp_path) -> Session:
    s = get_settings()
    al = Allowlist.from_file(PROJECT_ROOT / "security" / "allowlist.yaml")
    runner = CommandRunner(s.repo_paths)
    ctx = ToolContext(settings=s, allowlist=al, runner=runner, workspace=tmp_path / "ws")
    return Session(id="t", ctx=ctx)


async def test_abandoned_turn_stops_before_first_llm_call(tmp_path):
    """should_continue() == False on entry → the loop makes ZERO model calls and stops clean."""
    # A turn that, if it ran, would loop forever (every step calls a tool). The guard must
    # prevent it from ever calling the provider.
    provider = CountingProvider([
        AssistantTurn(text="working", tool_calls=[ToolCall("c1", "list_catalog", {"kinds": ["harnesses"]})]),
    ] * 5)

    events: list[tuple[str, dict]] = []

    async def emit(t, p):
        events.append((t, p))

    async def request_approval(kind, payload):
        raise AssertionError("no approval should be requested on an abandoned turn")

    session = _session(tmp_path)
    await AgentLoop(provider).run_turn(
        session, "hello", emit=emit, request_approval=request_approval,
        should_continue=lambda: False,
    )

    # The model was never asked for a step; the loop still emitted a terminal `done`.
    assert provider.calls == 0
    assert events[-1][0] == "done"
    # No tool ever ran.
    assert not [e for e in events if e[0] == "tool_call"]


async def test_abandoned_after_one_step_stops_before_next(tmp_path):
    """Disconnect partway through: allow the first step, then report abandoned → no 2nd step."""
    if not get_settings().bench_repo.is_dir():
        pytest.skip("repo not present")

    # Step 1 returns a tool call (loop runs the tool, feeds results, would do step 2).
    # Step 2, if reached, returns more work — but the guard flips to abandoned first.
    provider = CountingProvider([
        AssistantTurn(text="step1", tool_calls=[ToolCall("c1", "list_catalog", {"kinds": ["harnesses"]})]),
        AssistantTurn(text="step2 should never run", tool_calls=[ToolCall("c2", "list_catalog", {"kinds": ["specs"]})]),
    ])

    events: list[tuple[str, dict]] = []
    alive = {"v": True}

    async def emit(t, p):
        events.append((t, p))
        # Simulate the recipient dropping right after the first tool result lands.
        if t == "tool_result":
            alive["v"] = False

    async def request_approval(kind, payload):
        return True

    session = _session(tmp_path)
    await AgentLoop(provider).run_turn(
        session, "go", emit=emit, request_approval=request_approval,
        should_continue=lambda: alive["v"],
    )

    # Exactly one model step happened; the second was guarded off.
    assert provider.calls == 1
    # The first tool actually ran; the second tool call never fired.
    tool_calls = [p["name"] for (t, p) in events if t == "tool_call"]
    assert tool_calls == ["list_catalog"]
    assert events[-1][0] == "done"


async def test_default_no_guard_runs_to_completion(tmp_path):
    """Backward compatibility: with should_continue=None the loop behaves exactly as before."""
    provider = CountingProvider([
        AssistantTurn(text="all done, no tools", tool_calls=[]),
    ])
    events: list[tuple[str, dict]] = []

    async def emit(t, p):
        events.append((t, p))

    async def request_approval(kind, payload):
        return True

    session = _session(tmp_path)
    await AgentLoop(provider).run_turn(session, "hi", emit=emit, request_approval=request_approval)

    assert provider.calls == 1  # the single (toolless) step ran
    assert events[-1][0] == "done"
