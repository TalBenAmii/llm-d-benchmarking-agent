"""The request-time mandate must map each operation to its OWN skill task, in order.

Strengthens the prompt-mandate guards: within the "GROUND EACH OPERATION IN ITS SKILL"
block, every operation keyword is paired with its grounding task, a compound request
grounds each op, and the kind/CPU-sim path REQUIRES the quickstart runbook. Hermetic.
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
    # Cover the full two-tier mandate AND the separate WVA bullet that follows it, up to the
    # next HARD_RULES bullet ("ALWAYS present the plan …").
    end = prompt.index("ALWAYS present the plan", start)
    return prompt[start:end]


@pytest.mark.parametrize("keyword,skill", MAPPING)
def test_mandate_maps_operation_to_its_skill(mandate_block, keyword, skill):
    """Each operation keyword precedes its own *_skill task in the mandate mapping."""
    assert keyword in mandate_block, f"{keyword} missing from mandate block"
    assert skill in mandate_block, f"{skill} missing from mandate block"
    assert mandate_block.index(keyword) < mandate_block.index(skill)


def test_mandate_grounds_each_op_of_a_compound_request(mandate_block):
    """A compound request grounds EACH operation in its own skill up front."""
    assert "a request spanning SEVERAL operations" in mandate_block
    assert "grounds EACH in ITS OWN *_skill UP FRONT" in mandate_block


def test_mandate_kind_path_requires_quickstart(mandate_block):
    """On the kind/CPU-sim path the quickstart runbook is REQUIRED grounding (not a skill substitute)."""
    assert "quickstart" in mandate_block
    assert 'fetch_key_docs(task="quickstart")' in mandate_block
    assert "it is REQUIRED before standup" in mandate_block
