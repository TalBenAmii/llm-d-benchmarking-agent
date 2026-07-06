"""score_flow (the eval scoring harness) must reward grounding via fetch_key_docs.

Ties skill grounding into the general required/forbidden-tool scoring the live flow
eval uses — not just the skill-specific detection helpers. Scripted through the real
AgentLoop; group_scoring=False (scripted replay ignores the exposed tool set).
Sibling-independent — fetch_key_docs never errors.
"""
from __future__ import annotations

from tests.flows.flows import Flow, _tc, _turn
from tests.flows.harness import run_flow, score_flow


async def _score(flow, tmp_path):
    run = await run_flow(flow, tmp_path=tmp_path, simulate=True)
    return score_flow(run, flow, group_scoring=False)


async def test_score_passes_when_required_skill_tool_called(tmp_path):
    """A run that calls the required fetch_key_docs passes the tool score."""
    flow = Flow(
        name="score-skill-ok", title="t", description="d",
        mock_user_input="deploy on kind",
        turns=[_turn("Grounding in the deploy skill.", _tc("fetch_key_docs", task="deploy_skill"))],
        required_tools=["fetch_key_docs"],
    )
    passed, notes = await _score(flow, tmp_path)
    assert passed, notes


async def test_score_fails_when_required_skill_tool_missing(tmp_path):
    """A run that never grounds (no fetch_key_docs) fails the required-tool score."""
    flow = Flow(
        name="score-skill-miss", title="t", description="d",
        mock_user_input="deploy on kind",
        turns=[_turn("Answering directly without grounding.")],
        required_tools=["fetch_key_docs"],
    )
    passed, notes = await _score(flow, tmp_path)
    assert not passed
    assert any("missing required tool" in n for n in notes), notes


async def test_score_fails_when_skill_tool_forbidden(tmp_path):
    """A run that calls a forbidden tool fails even though it grounded."""
    flow = Flow(
        name="score-skill-forb", title="t", description="d",
        mock_user_input="deploy on kind",
        turns=[_turn("Grounding when it was forbidden.", _tc("fetch_key_docs", task="deploy_skill"))],
        forbidden_tools=["fetch_key_docs"],
    )
    passed, notes = await _score(flow, tmp_path)
    assert not passed
    assert any("FORBIDDEN" in n for n in notes), notes
