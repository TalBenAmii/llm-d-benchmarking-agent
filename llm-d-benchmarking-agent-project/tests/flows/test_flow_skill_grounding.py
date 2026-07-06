"""Every operation flow in the golden corpus must ground in its llm-d skill FIRST.

Enforces commit 0769b5c's request-time mandate across the golden transcripts: a flow that
performs or plans an operation (propose_session_plan / execute_llmdbenchmark) must fetch a
*_skill (or read a skill doc) BEFORE that operation. Two flows are deliberately exempt and
documented. Hermetic, sibling-independent.
"""
from __future__ import annotations

import pytest

from tests.eval.simulate.test_skill_usage_live import SCENARIOS
from tests.flows.flows import ALL_FLOWS

_OPERATION_TOOLS = {"propose_session_plan", "execute_llmdbenchmark"}
_SKILL_DIRS = [s.read_prefix for s in SCENARIOS]

# Deliberately NOT grounded (documented): a refusal transcript that models a confused/over-eager
# model doing the wrong thing to be refused, and a deterministic (live_eval=False) catalog-denial
# policy test that already grounds in the on-disk catalog.
_EXEMPT = {"safety-refusal", "error-catalog-drift-denied"}


def _calls(flow):
    return [tc for t in flow.turns for tc in t.tool_calls]


def _is_skill_fetch(tc) -> bool:
    if tc.name == "fetch_key_docs":
        task = str(tc.input.get("task", ""))
        return task.endswith("_skill") or task == "quickstart"
    return tc.name == "read_repo_doc" and any(d in str(tc.input.get("path", "")) for d in _SKILL_DIRS)


def _first_op_index(calls):
    return next((i for i, tc in enumerate(calls) if tc.name in _OPERATION_TOOLS), None)


def _first_skill_index(calls):
    return next((i for i, tc in enumerate(calls) if _is_skill_fetch(tc)), None)


_OPERATION_FLOWS = [
    f for f in ALL_FLOWS
    if _first_op_index(_calls(f)) is not None and f.name not in _EXEMPT
]


@pytest.mark.parametrize("flow", _OPERATION_FLOWS, ids=lambda f: f.name)
def test_operation_flow_grounds_in_skill_before_operation(flow):
    """The golden transcript fetches a *_skill before its first plan/execute operation."""
    calls = _calls(flow)
    op_idx = _first_op_index(calls)
    skill_idx = _first_skill_index(calls)
    assert skill_idx is not None, f"{flow.name} performs an operation but never grounds in a skill"
    assert skill_idx < op_idx, f"{flow.name} grounds at index {skill_idx} but operates at {op_idx}"


def test_there_are_grounded_operation_flows():
    """Sanity: the corpus actually has operation flows to enforce against (not vacuous)."""
    assert len(_OPERATION_FLOWS) >= 10


def test_exempt_flows_still_exist():
    """Guard the exempt list against drift — each exempt name is a real flow."""
    names = {f.name for f in ALL_FLOWS}
    missing = _EXEMPT - names
    assert not missing, f"exempt flows no longer exist: {missing}"
