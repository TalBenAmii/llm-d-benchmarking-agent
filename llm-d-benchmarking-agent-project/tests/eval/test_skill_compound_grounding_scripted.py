"""Hermetic guard for the COMPOUND skill-grounding mandate: a request that spans
multiple operations must ground EACH operation in ITS OWN skill up front.

(prompt.py HARD_RULES: a compound request grounds EACH in ITS OWN skill UP FRONT —
fetch deploy_skill AND benchmark_skill.) Driven through the real engine with a
scripted transcript; reuses the live eval's per-scenario detection helpers.
"""
from __future__ import annotations

from tests.eval.simulate.test_skill_usage_live import SCENARIOS, _run_passes, _skill_index
from tests.flows.flows import Flow, _tc, _turn
from tests.flows.harness import run_flow

_BY_KEY = {s.key: s for s in SCENARIOS}
_DEPLOY = _BY_KEY["deploy_skill"]
_BENCH = _BY_KEY["benchmark_skill"]


def _plan_tc():
    """Schema-VALID plan args — the SDK's MCP layer rejects invalid input before dispatch
    (no tool_call event), and these tests score grounding ORDER, not arg validity."""
    return _tc("propose_session_plan", use_case_summary="scripted", spec="cicd/kind",
               namespace="llmd-quickstart", harness="inference-perf",
               workload="sanity_random.yaml", expected_steps=["standup"])


async def _tool_calls_for(turns, tmp_path):
    flow = Flow(
        name="skill-compound",
        title="compound skill grounding",
        description="deploy + benchmark in one request",
        mock_user_input="Deploy meta-llama/Llama-3.1-8B on kind and then benchmark it.",
        turns=turns,
    )
    run = await run_flow(flow, tmp_path=tmp_path, simulate=True)
    return run.tool_calls


async def test_both_skills_up_front_satisfies_each_operation(tmp_path):
    """Fetching deploy_skill AND benchmark_skill before any operation grounds both."""
    turns = [
        _turn(
            "Grounding in both operations' skills up front.",
            _tc("fetch_key_docs", task="deploy_skill"),
            _tc("fetch_key_docs", task="benchmark_skill"),
        ),
        _turn("Proposing the deploy.", _plan_tc()),
        _turn("Proposing the benchmark run.", _plan_tc()),
    ]
    calls = await _tool_calls_for(turns, tmp_path)
    assert _skill_index(calls, _DEPLOY) == 0
    assert _skill_index(calls, _BENCH) == 1
    assert _run_passes(calls, _DEPLOY)
    assert _run_passes(calls, _BENCH)


async def test_second_skill_after_first_op_fails_that_operation(tmp_path):
    """Grounding only the first op's skill, fetching the benchmark skill late, fails it."""
    turns = [
        _turn("Grounding in the deploy skill only.", _tc("fetch_key_docs", task="deploy_skill")),
        _turn("Proposing the deploy.", _plan_tc()),
        _turn("Late benchmark grounding.", _tc("fetch_key_docs", task="benchmark_skill")),
        _turn("Proposing the benchmark run.", _plan_tc()),
    ]
    calls = await _tool_calls_for(turns, tmp_path)
    assert _run_passes(calls, _DEPLOY)      # deploy skill was up front
    assert not _run_passes(calls, _BENCH)   # benchmark skill came after the first operation
