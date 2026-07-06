"""Every llm-d skill operation has at least one golden-flow exemplar.

Completeness counterpart to test_no_orphan_operation (which guards the operation->skill map):
this asserts each *_skill in SKILL_TASKS is grounded by at least one flow in the golden corpus,
so a newly-added skill/operation can't ship without a golden transcript demonstrating it. Before
the compare + WVA flows were added, compare_skill and wva_skill had no exemplar and this would
have failed. Hermetic, sibling-independent.
"""
from __future__ import annotations

import pytest

from tests.eval._skills import SKILL_TASKS
from tests.flows.flows import ALL_FLOWS


def _grounded_skills(flow) -> set[str]:
    return {
        tc.input["task"]
        for t in flow.turns
        for tc in t.tool_calls
        if tc.name == "fetch_key_docs" and tc.input.get("task") in SKILL_TASKS
    }


_FLOWS_BY_SKILL = {
    skill: [f.name for f in ALL_FLOWS if skill in _grounded_skills(f)]
    for skill in SKILL_TASKS
}


@pytest.mark.parametrize("skill", sorted(SKILL_TASKS))
def test_skill_has_a_golden_flow(skill):
    """At least one golden flow grounds in this skill."""
    flows = _FLOWS_BY_SKILL[skill]
    assert flows, f"no golden flow grounds in {skill} — add an exemplar transcript"
