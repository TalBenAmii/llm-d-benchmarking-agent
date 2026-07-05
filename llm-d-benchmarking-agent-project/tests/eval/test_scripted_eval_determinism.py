"""The scripted eval harness must be deterministic — the golden-transcript gate and the
hermetic skill evals rely on run_flow replaying identically every time.

Runs the same scripted flow twice and asserts identical tool_calls + assistant_texts.
Sibling-independent (fetch_key_docs never errors).
"""
from __future__ import annotations

from tests.flows.flows import Flow, _tc, _turn
from tests.flows.harness import run_flow


def _flow():
    return Flow(
        name="determinism", title="t", description="d",
        mock_user_input="deploy on kind then benchmark it",
        turns=[
            _turn("Grounding in the deploy skill.", _tc("fetch_key_docs", task="deploy_skill")),
            _turn("Proposing the plan.", _tc("propose_session_plan")),
        ],
    )


async def test_scripted_run_is_deterministic(tmp_path):
    """Two runs of the same scripted flow produce identical tool_calls + assistant_texts."""
    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "b"
    b.mkdir()
    r1 = await run_flow(_flow(), tmp_path=a, simulate=True)
    r2 = await run_flow(_flow(), tmp_path=b, simulate=True)
    assert r1.tool_calls == r2.tool_calls
    assert r1.assistant_texts == r2.assistant_texts
    assert [c["name"] for c in r1.tool_calls] == ["fetch_key_docs", "propose_session_plan"]
