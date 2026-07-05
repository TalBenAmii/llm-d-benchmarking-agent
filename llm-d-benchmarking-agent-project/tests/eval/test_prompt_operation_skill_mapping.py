"""The request-time mandate must map each operation to its OWN skill task, in order.

Strengthens the prompt-mandate guards: within the "GROUND EACH OPERATION IN ITS SKILL"
block, every operation keyword is paired with its *_skill task, a compound request
grounds each op, and task="quickstart" is not a skill substitute. Hermetic.
"""
from __future__ import annotations

import pytest

from app.agent.prompt import build_system_prompt

MAPPING = [
    ("deploy", "deploy_skill"),
    ("teardown", "teardown_skill"),
    ("benchmark", "benchmark_skill"),
    ("compare", "compare_skill"),
    ("autoscal", "wva_skill"),
]


@pytest.fixture()
def mandate_block(tool_ctx) -> str:
    prompt = build_system_prompt(tool_ctx)
    start = prompt.index("GROUND EACH OPERATION IN ITS SKILL")
    return prompt[start:start + 1800]


@pytest.mark.parametrize("keyword,skill", MAPPING)
def test_mandate_maps_operation_to_its_skill(mandate_block, keyword, skill):
    """Each operation keyword precedes its own *_skill task in the mandate mapping."""
    assert keyword in mandate_block, f"{keyword} missing from mandate block"
    assert skill in mandate_block, f"{skill} missing from mandate block"
    assert mandate_block.index(keyword) < mandate_block.index(skill)


def test_mandate_grounds_each_op_of_a_compound_request(mandate_block):
    """A compound request grounds EACH operation in its own skill up front."""
    assert "deploy_skill AND benchmark_skill" in mandate_block


def test_mandate_quickstart_is_not_a_skill_substitute(mandate_block):
    """task=quickstart is orientation only — never a substitute for the skill."""
    assert "quickstart" in mandate_block
    assert "substitute" in mandate_block
