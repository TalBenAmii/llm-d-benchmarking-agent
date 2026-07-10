"""read_knowledge — the tool that loads an operation's skill-adapted playbook — must
return real content that grounds in the skill, and must refuse unsafe names.

Hermetic (reads only local knowledge/); read_knowledge auto-runs and is read-only.
"""
from __future__ import annotations

import pytest

from app.tools.access import knowledge_access

# operation playbook topic -> the skill task its body must reference
PLAYBOOK_TOPIC_SKILL = {
    "deploy_path_playbook": "deploy_skill",
    "teardown": "teardown_skill",
    "author_spec_workload": "benchmark_skill",
    "sweep_playbook": "compare_skill",
    "autoscaling": "wva_skill",
}


@pytest.mark.parametrize("topic,skill", sorted(PLAYBOOK_TOPIC_SKILL.items()))
def test_read_knowledge_returns_playbook_grounded_in_skill(tool_ctx, topic, skill):
    """read_knowledge loads each operation playbook and its body grounds in the skill."""
    res = knowledge_access.read_knowledge(tool_ctx, name=topic)
    assert "error" not in res, res
    assert res.get("content", "").strip(), f"{topic} returned no content"
    assert skill in res["content"], f"{topic} body does not reference {skill}"


def test_read_knowledge_stem_and_basename_both_resolve(tool_ctx):
    """A topic resolves by bare stem and by explicit .md basename to the same content."""
    by_stem = knowledge_access.read_knowledge(tool_ctx, name="deploy_path_playbook")
    tool_ctx.fetched_docs.clear()  # read_knowledge dedups per context; reset between reads
    by_name = knowledge_access.read_knowledge(tool_ctx, name="deploy_path_playbook.md")
    assert "error" not in by_stem and "error" not in by_name
    assert by_stem["content"] == by_name["content"]


def test_read_knowledge_rejects_path_traversal(tool_ctx):
    """A traversal / path-bearing name is refused (no repo escape)."""
    res = knowledge_access.read_knowledge(tool_ctx, name="../key_docs")
    assert "error" in res
    assert "valid_topics" in res


def test_read_knowledge_unknown_topic_lists_valid(tool_ctx):
    """An unknown topic returns an error plus the valid topic list."""
    res = knowledge_access.read_knowledge(tool_ctx, name="does_not_exist_topic")
    assert "error" in res
    assert res.get("valid_topics"), "should list valid topics"
