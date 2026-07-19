"""Canonical skill-grounded golden flows — one per operation — that DEMONSTRATE the
request-time mandate and pass BOTH scoring layers (score_flow + the skill detection
helpers). Additive reference examples of correct behavior; they do NOT modify the
shared golden corpus (tests/flows/flows.py).
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
from tests.flows.harness import run_flow, score_flow

_BY_KEY = {s.key: s for s in SCENARIOS}
# operation tool the golden flow uses to carry out each skill's operation
_OP_TOOL = {
    "deploy_skill": "propose_session_plan",
    "teardown_skill": "propose_session_plan",
    "benchmark_skill": "execute_llmdbenchmark",
    "compare_skill": "propose_session_plan",
    "wva_skill": "propose_session_plan",
}


def _op_call(op):
    # Schema-VALID args — the SDK's MCP layer rejects invalid input before dispatch (no
    # tool_call event), and these flows score grounding order + tool choice, not arg validity.
    if op == "execute_llmdbenchmark":
        return _tc(op, subcommand="run")
    return _tc(op, use_case_summary="scripted", spec="cicd/kind", namespace="llmd-quickstart",
               harness="inference-perf", workload="sanity_random.yaml", expected_steps=["standup"])


def _grounded_flow(key, op):
    return Flow(
        name=f"grounded-{key}", title="t", description="d",
        mock_user_input=_BY_KEY[key].ask,
        turns=[
            _turn("Grounding in the operation's skill first.", _tc("fetch_key_docs", task=key)),
            _turn("Carrying out the operation.", _op_call(op)),
        ],
        required_tools=["fetch_key_docs", op],
    )


@pytest.mark.parametrize("key", sorted(_OP_TOOL), ids=sorted(_OP_TOOL))
async def test_skill_grounded_flow_passes_both_scorers(key, tmp_path):
    """A skill-first flow passes score_flow AND the skill-usage detection contract."""
    op = _OP_TOOL[key]
    flow = _grounded_flow(key, op)
    run = await run_flow(flow, tmp_path=tmp_path, simulate=True)
    assert run.errors == [], run.errors
    passed, notes = score_flow(run, flow)
    assert passed, notes
    scenario = _BY_KEY[key]
    assert _skill_index(run.tool_calls, scenario) == 0
    assert _operation_index(run.tool_calls) == 1
    assert _run_passes(run.tool_calls, scenario)
