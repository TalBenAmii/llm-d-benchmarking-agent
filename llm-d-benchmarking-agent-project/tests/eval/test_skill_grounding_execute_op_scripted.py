"""Scripted skill-grounding when the operation is execute_llmdbenchmark (not just
propose_session_plan) — covers the second member of the eval's operation gate.

Driven through the real AgentLoop; reuses the live eval's detection helpers.
"""
from __future__ import annotations

from tests.eval.simulate.test_skill_usage_live import (
    SCENARIOS,
    _operation_index,
    _run_passes,
    _skill_index,
)
from tests.flows.flows import Flow, _tc, _turn
from tests.flows.harness import run_flow

_BENCH = next(s for s in SCENARIOS if s.key == "benchmark_skill")


async def _tool_calls_for(turns, tmp_path):
    flow = Flow(
        name="skill-exec", title="t", description="d",
        mock_user_input="run a benchmark against the running stack",
        turns=turns,
    )
    run = await run_flow(flow, tmp_path=tmp_path, simulate=True)
    return run.tool_calls


async def test_skill_before_execute_op_passes(tmp_path):
    """Grounding in benchmark_skill before execute_llmdbenchmark satisfies the contract."""
    turns = [
        _turn("Grounding in the benchmark skill.", _tc("fetch_key_docs", task="benchmark_skill")),
        _turn("Running the benchmark.", _tc("execute_llmdbenchmark", subcommand="run")),
    ]
    calls = await _tool_calls_for(turns, tmp_path)
    assert [c["name"] for c in calls] == ["fetch_key_docs", "execute_llmdbenchmark"]
    assert _skill_index(calls, _BENCH) == 0
    assert _operation_index(calls) == 1
    assert _run_passes(calls, _BENCH)


async def test_execute_op_without_skill_fails(tmp_path):
    """Running the benchmark without grounding in its skill must NOT pass."""
    turns = [_turn("Running directly.", _tc("execute_llmdbenchmark", subcommand="run"))]
    calls = await _tool_calls_for(turns, tmp_path)
    assert _operation_index(calls) == 0
    assert _skill_index(calls, _BENCH) is None
    assert not _run_passes(calls, _BENCH)
