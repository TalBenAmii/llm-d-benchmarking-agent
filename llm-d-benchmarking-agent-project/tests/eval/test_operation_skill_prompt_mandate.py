"""The request-time skill-grounding mandate must live in the assembled system prompt.

Guards the prompt wiring behind the live skill-usage eval (commit 0769b5c): the agent
is told to fetch each operation's *_skill via fetch_key_docs FIRST, at request time.
Hermetic — builds the system prompt in-process; no siblings needed.
"""
from __future__ import annotations

import pytest

from app.agent.prompt import build_system_prompt
from tests.eval._skills import SKILL_TASKS


@pytest.fixture()
def system_prompt(tool_ctx) -> str:
    return build_system_prompt(tool_ctx)


def test_prompt_states_ground_each_operation_in_its_skill(system_prompt):
    """The HARD_RULES mandate to ground each operation in its skill is present."""
    assert "GROUND EACH OPERATION IN ITS SKILL" in system_prompt
    assert "FETCH IT FIRST" in system_prompt
    assert "AT REQUEST TIME" in system_prompt


def test_prompt_names_the_fetch_tool(system_prompt):
    """The mandate names fetch_key_docs as the grounding mechanism."""
    assert "fetch_key_docs" in system_prompt


def test_prompt_role_step_grounds_operations_first(system_prompt):
    """The ROLE section tells the agent to ground each operation in its *_skill first."""
    assert "*_skill FIRST" in system_prompt


@pytest.mark.parametrize("task", sorted(SKILL_TASKS))
def test_prompt_maps_each_operation_to_its_skill_task(system_prompt, task):
    """Every operation's *_skill task name appears in the prompt's operation->skill map."""
    assert task in system_prompt, f"{task} mapping missing from system prompt mandate"
