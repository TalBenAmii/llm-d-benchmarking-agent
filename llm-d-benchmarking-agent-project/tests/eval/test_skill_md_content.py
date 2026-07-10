"""Each operation's SKILL.md is a real llm-d skill, not a placeholder.

Guards that the read-only llm-d-skills the agent grounds in are the expected upstream
skills: YAML frontmatter, a substantial body, and an llm-d reference. Sibling-dependent.
"""
from __future__ import annotations

import pytest

from app.tools.access import knowledge_access
from tests.eval._skills import SKILL_TASKS


@pytest.mark.parametrize("task", sorted(SKILL_TASKS))
def test_skill_md_is_a_real_llm_d_skill(skills_ctx, task):
    """Each skill's SKILL.md opens with frontmatter, is substantial, and names llm-d."""
    res = knowledge_access.read_repo_doc(skills_ctx, path=SKILL_TASKS[task] + "SKILL.md")
    content = res["content"]
    assert content.startswith("---"), f"{task} SKILL.md should open with YAML frontmatter"
    assert len(content) > 1000, f"{task} SKILL.md body is too short to be a real skill"
    assert "llm-d" in content.lower(), f"{task} SKILL.md should reference llm-d"
