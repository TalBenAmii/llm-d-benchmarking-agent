"""Hermetic mirror of the live skill-usage eval (tests/eval/simulate/test_skill_usage_live.py).

The live eval drives a real LLM to check the agent pulls each operation's llm-d-skill
into context before the operation — that spends Max-plan quota, so it's gated behind
LLM_EVAL_LIVE=1. THIS file exercises the SAME detection contract
(_skill_index / _operation_index / _run_passes) against deterministic scripted
transcripts driven through the real AgentLoop + real fetch_key_docs / read_repo_doc
tools — free, always-run. It proves the skill-grounding signal is observable in the
tool-call stream and that the detection helpers reward the right orderings and reject
the wrong ones (missing skill, wrong skill, skill fetched after the operation).
"""
from __future__ import annotations

import pytest

from tests.eval.simulate.test_skill_usage_live import (
    SCENARIOS,
    _operation_index,
    _run_passes,
    _skill_index,
)
from tests.flows.flows import Flow, _tc, _turn
from tests.flows.harness import run_flow


async def _tool_calls_for(turns, ask, tmp_path):
    """Run a scripted golden transcript through the real AgentLoop, return run.tool_calls."""
    flow = Flow(
        name="skill-scripted",
        title="scripted skill grounding",
        description="hermetic scripted skill-usage transcript",
        mock_user_input=ask,
        turns=turns,
    )
    run = await run_flow(flow, tmp_path=tmp_path, simulate=True)
    return run.tool_calls


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.key for s in SCENARIOS])
async def test_scripted_fetch_key_docs_before_op_passes(scenario, tmp_path):
    """fetch_key_docs(task=key) before the operation satisfies the skill-grounding contract."""
    turns = [
        _turn("Grounding in the operation's skill first.", _tc("fetch_key_docs", task=scenario.key)),
        _turn("Now proposing the plan.", _tc("propose_session_plan")),
    ]
    calls = await _tool_calls_for(turns, scenario.ask, tmp_path)
    assert _skill_index(calls, scenario) == 0
    assert _operation_index(calls) == 1
    assert _run_passes(calls, scenario)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.key for s in SCENARIOS])
async def test_scripted_read_repo_doc_alt_path_passes(scenario, tmp_path):
    """read_repo_doc on the skill's SKILL.md is the accepted alternative grounding path."""
    turns = [
        _turn("Reading the skill doc directly.",
              _tc("read_repo_doc", path=scenario.skill_dir + "SKILL.md")),
        _turn("Now proposing the plan.", _tc("propose_session_plan")),
    ]
    calls = await _tool_calls_for(turns, scenario.ask, tmp_path)
    assert _skill_index(calls, scenario) == 0
    assert _run_passes(calls, scenario)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.key for s in SCENARIOS])
async def test_scripted_op_without_skill_fails(scenario, tmp_path):
    """Jumping to the operation without grounding in the skill must NOT pass."""
    turns = [_turn("Proposing directly, skipping the skill.", _tc("propose_session_plan"))]
    calls = await _tool_calls_for(turns, scenario.ask, tmp_path)
    assert _skill_index(calls, scenario) is None
    assert _operation_index(calls) == 0
    assert not _run_passes(calls, scenario)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.key for s in SCENARIOS])
async def test_scripted_wrong_skill_does_not_satisfy(scenario, tmp_path):
    """Fetching a DIFFERENT operation's skill does not count for this scenario."""
    other = next(s for s in SCENARIOS if s.key != scenario.key)
    turns = [
        _turn("Grabbing the wrong skill.", _tc("fetch_key_docs", task=other.key)),
        _turn("Proposing the plan.", _tc("propose_session_plan")),
    ]
    calls = await _tool_calls_for(turns, scenario.ask, tmp_path)
    assert _skill_index(calls, scenario) is None
    assert not _run_passes(calls, scenario)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.key for s in SCENARIOS])
async def test_scripted_skill_after_op_fails_ordering(scenario, tmp_path):
    """A skill fetched AFTER the operation is too late — ordering must fail."""
    turns = [
        _turn("Proposing first.", _tc("propose_session_plan")),
        _turn("Fetching the skill late.", _tc("fetch_key_docs", task=scenario.key)),
    ]
    calls = await _tool_calls_for(turns, scenario.ask, tmp_path)
    assert _operation_index(calls) == 0
    assert _skill_index(calls, scenario) == 1
    assert not _run_passes(calls, scenario)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.key for s in SCENARIOS])
async def test_scripted_skill_without_op_passes(scenario, tmp_path):
    """Grounding in the skill with no operation reached still passes (op_idx is None)."""
    turns = [_turn("Just grounding in the skill.", _tc("fetch_key_docs", task=scenario.key))]
    calls = await _tool_calls_for(turns, scenario.ask, tmp_path)
    assert _operation_index(calls) is None
    assert _skill_index(calls, scenario) == 0
    assert _run_passes(calls, scenario)
