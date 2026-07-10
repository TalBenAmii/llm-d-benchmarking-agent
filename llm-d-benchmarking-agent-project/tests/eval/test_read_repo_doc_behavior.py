"""Behavioural guards for read_repo_doc — the alt path the skill-usage eval accepts.

The agent can ground in a skill by reading its SKILL.md directly; read_repo_doc must
return real bodies, dedup exact repeats, and confine reads to real files under the
read-only repos. Sibling-dependent reads use the skills_ctx skip-guard.
"""
from __future__ import annotations

import pytest

from app.tools.access import knowledge_access
from app.tools.context import ToolError
from tests.eval._skills import SKILL_TASKS


@pytest.mark.parametrize("task", sorted(SKILL_TASKS))
def test_reads_each_skill_md_body(skills_ctx, task):
    """read_repo_doc returns a real, non-empty SKILL.md body for each skill."""
    res = knowledge_access.read_repo_doc(skills_ctx, path=SKILL_TASKS[task] + "SKILL.md")
    assert res["content"].strip()
    assert res["path"].endswith("SKILL.md")


def test_exact_repeat_is_deduped(skills_ctx):
    """Reading the same skill doc twice on one context dedups the body the second time."""
    path = "llm-d-skills/skills/deploy-llm-d/SKILL.md"
    first = knowledge_access.read_repo_doc(skills_ctx, path=path)
    assert first["content"].strip()
    second = knowledge_access.read_repo_doc(skills_ctx, path=path)
    assert second.get("already_provided") is True
    assert "content" not in second


def test_directory_path_is_rejected(tool_ctx):
    """A directory (not a file) is refused."""
    with pytest.raises(ToolError):
        knowledge_access.read_repo_doc(tool_ctx, path="llm-d-skills/skills/deploy-llm-d/")


def test_unknown_repo_prefix_is_rejected(tool_ctx):
    """A path under an unknown repo cannot be resolved."""
    with pytest.raises(ToolError):
        knowledge_access.read_repo_doc(tool_ctx, path="nonexistent-repo/x.md")
