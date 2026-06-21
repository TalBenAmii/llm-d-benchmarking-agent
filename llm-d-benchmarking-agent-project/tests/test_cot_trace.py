"""Chain-of-thought debug trace + the Agent SDK reasoning options.

Hermetic — no CLI subprocess, no network, no API key. Covers:
  * the AGENT_SDK_THINKING / AGENT_SDK_EFFORT → ClaudeAgentOptions kwargs translation,
  * TurnTrace writing newline-delimited JSON to a session folder (and the no-op disabled form),
  * the agent loop persisting a turn's reasoning + decisions to <workspace>/cot_trace.jsonl.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.agent.loop import AgentLoop
from app.agent.session import Session
from app.config import get_settings
from app.llm.agent_sdk_provider import _effort_option, _thinking_options
from app.llm.provider import AssistantTurn, Usage
from app.observability.cot_trace import TRACE_FILENAME, TurnTrace
from app.security.allowlist import Allowlist
from app.security.runner import CommandRunner
from app.tools.context import ToolContext

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ---- option translation --------------------------------------------------------------------
def test_thinking_options_adaptive():
    assert _thinking_options("adaptive") == {"thinking": {"type": "adaptive"}}


def test_thinking_options_fixed_budget():
    assert _thinking_options("4096") == {"thinking": {"type": "enabled", "budget_tokens": 4096}}


def test_thinking_options_off_is_empty():
    # "off"/"disabled"/"0"/garbage all mean "don't set the option" → nothing to capture.
    for value in ("off", "disabled", "none", "0", "", "nonsense"):
        assert _thinking_options(value) == {}


def test_effort_option_passthrough_and_fallback():
    assert _effort_option("high") == {"effort": "high"}
    assert _effort_option("MAX") == {"effort": "max"}      # case-insensitive
    assert _effort_option("turbo") == {}                   # unknown → CLI default, never crash


# ---- TurnTrace -----------------------------------------------------------------------------
def test_disabled_trace_writes_nothing(tmp_path):
    trace = TurnTrace.disabled()
    assert trace.enabled is False
    trace.event("step", thinking="should not appear")
    assert not any(tmp_path.rglob(TRACE_FILENAME))


def test_trace_appends_jsonl(tmp_path):
    trace = TurnTrace.for_session(tmp_path)
    trace.event("turn_start", user_text="hi")
    trace.event("step", thinking="reasoning here", text="answer")

    lines = (tmp_path / TRACE_FILENAME).read_text().strip().splitlines()
    assert len(lines) == 2
    first, second = json.loads(lines[0]), json.loads(lines[1])
    assert first["kind"] == "turn_start" and first["user_text"] == "hi"
    assert second["kind"] == "step" and second["thinking"] == "reasoning here"
    assert "ts" in first  # every record is timestamped


def test_trace_bounds_a_nested_body(tmp_path):
    # The module's documented contract (_BODY_LIMIT) is that "a runaway turn can't grow the
    # trace without limit". A model can emit a large body nested INSIDE a tool_calls input dict
    # (e.g. write_config's `content`), not just as a flat string. That nested oversize body must
    # be bounded too — otherwise one step's record is unbounded (defeating the per-record cap).
    from app.observability.cot_trace import _BODY_LIMIT

    blob = "y" * (_BODY_LIMIT + 500_000)
    trace = TurnTrace.for_session(tmp_path)
    trace.event("step", tool_calls=[{"name": "write_config", "input": {"content": {"blob": blob}}}])

    line = (tmp_path / TRACE_FILENAME).read_text().strip()
    # The whole JSON record must stay within a small constant of the per-body limit, not balloon
    # to the full ~700KB of the un-clipped blob.
    assert len(line) < _BODY_LIMIT + 50_000
    # And it must remain valid JSON (a truncation marker, never malformed).
    json.loads(line)


# ---- loop integration ----------------------------------------------------------------------
def _session(tmp_path) -> Session:
    s = get_settings()
    al = Allowlist.from_file(PROJECT_ROOT / "security" / "allowlist.yaml")
    runner = CommandRunner(s.repo_paths)
    ctx = ToolContext(settings=s, allowlist=al, runner=runner, workspace=tmp_path / "ws")
    return Session(id="t", ctx=ctx)


class _FakeProvider:
    def __init__(self, turns):
        self._turns = turns
        self.i = 0

    async def chat(self, *, system, messages, tools, cache_key=None):
        turn = self._turns[self.i]
        self.i += 1
        return turn


async def test_loop_writes_thinking_to_session_trace(tmp_path):
    # A single text-only turn whose AssistantTurn carries chain-of-thought.
    turns = [AssistantTurn(text="All set.", tool_calls=[],
                           usage=Usage(input_tokens=10, output_tokens=3),
                           thinking="The user wants a tiny benchmark; no cluster work needed.")]

    async def emit(_t, _p):
        pass

    async def request_approval(_kind, _payload):
        return True

    session = _session(tmp_path)
    await AgentLoop(_FakeProvider(turns)).run_turn(
        session, "hello", emit=emit, request_approval=request_approval)

    trace_path = session.ctx.workspace / TRACE_FILENAME
    records = [json.loads(line) for line in trace_path.read_text().strip().splitlines()]
    kinds = [r["kind"] for r in records]
    assert kinds[0] == "turn_start" and kinds[-1] == "turn_end"
    step = next(r for r in records if r["kind"] == "step")
    assert step["thinking"] == "The user wants a tiny benchmark; no cluster work needed."
    assert step["text"] == "All set."
    assert step["usage"]["output"] == 3


async def test_loop_trace_omits_thinking_when_provider_gives_none(tmp_path):
    # Providers that don't surface reasoning (thinking=None) still get a trace; thinking is null.
    turns = [AssistantTurn(text="done", tool_calls=[])]

    async def emit(_t, _p):
        pass

    async def request_approval(_kind, _payload):
        return True

    session = _session(tmp_path)
    await AgentLoop(_FakeProvider(turns)).run_turn(
        session, "hi", emit=emit, request_approval=request_approval)

    records = [json.loads(line)
               for line in (session.ctx.workspace / TRACE_FILENAME).read_text().strip().splitlines()]
    step = next(r for r in records if r["kind"] == "step")
    assert step["thinking"] is None
