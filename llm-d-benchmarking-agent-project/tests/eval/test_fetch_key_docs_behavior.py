"""Behavioural guards for the skill-fetch tools (fetch_key_docs / read_repo_doc).

The agent grounds each operation in its llm-d-skill by CALLING these tools, so their
contract — task filtering, the full task catalogue, truncation, and read confinement
to the read-only repos — is what the skill-usage eval ultimately relies on. Hermetic;
the checks that read real skill bodies use the skills_ctx skip-guard.
"""
from __future__ import annotations

import pytest

from app.tools.access import knowledge_access
from app.tools.context import ToolError
from tests.eval._skills import SKILL_TASKS


def test_task_filter_returns_only_that_task(skills_ctx):
    """fetch_key_docs(task=X) returns docs for X only — never another task's docs."""
    for task in SKILL_TASKS:
        res = knowledge_access.fetch_key_docs(skills_ctx, task=task)
        tasks_seen = {d["task"] for d in res["docs"]}
        assert tasks_seen == {task}, f"{task} filter leaked other tasks: {tasks_seen}"


def test_no_filter_advertises_full_task_catalogue(skills_ctx):
    """Unfiltered fetch returns every doc and advertises the full task set."""
    res = knowledge_access.fetch_key_docs(skills_ctx)
    assert res["task"] is None
    assert res["docs"], "unfiltered fetch returned no docs"
    available = res["available_tasks"]
    assert set(SKILL_TASKS).issubset(available)
    assert len(available) == len(set(available)), "duplicate task names in available_tasks"


def test_available_tasks_independent_of_filter(tool_ctx):
    """available_tasks lists the full catalogue regardless of the task filter."""
    unknown = knowledge_access.fetch_key_docs(tool_ctx, task="not_a_real_task")
    nofilter = knowledge_access.fetch_key_docs(tool_ctx)
    assert unknown["docs"] == []
    assert unknown.get("found_count", -1) == 0
    assert sorted(unknown["available_tasks"]) == sorted(nofilter["available_tasks"])
    assert set(SKILL_TASKS).issubset(unknown["available_tasks"])


def test_repeat_fetch_still_carries_the_body(skills_ctx):
    """Every fetch returns the full body — a repeat within a session is NOT collapsed (the
    per-session doc dedup was removed at the SDK-native cutover; the CLI's own context
    management is the bound now)."""
    first = knowledge_access.fetch_key_docs(skills_ctx, task="benchmark_skill")
    fd = next(d for d in first["docs"] if d["path"].endswith("SKILL.md"))
    assert fd.get("content", "").strip(), "first fetch should carry the body"
    second = knowledge_access.fetch_key_docs(skills_ctx, task="benchmark_skill")
    sd = next(d for d in second["docs"] if d["path"].endswith("SKILL.md"))
    assert sd.get("content") == fd.get("content")


def test_max_bytes_each_truncates_content(skills_ctx):
    """max_bytes_each caps a fetched skill body and flags it truncated."""
    res = knowledge_access.fetch_key_docs(skills_ctx, task="benchmark_skill", max_bytes_each=100)
    d = res["docs"][0]
    assert d.get("truncated") is True
    assert 0 < len(d["content"]) <= 100


def test_read_repo_doc_rejects_paths_outside_readonly_repos(tool_ctx):
    """read_repo_doc confines reads to the read-only repos — an app path is refused."""
    with pytest.raises(ToolError):
        knowledge_access.read_repo_doc(tool_ctx, path="app/main.py")


def test_read_repo_doc_rejects_traversal_escape(tool_ctx):
    """A parent-traversal path cannot escape the read-only repos."""
    with pytest.raises(ToolError):
        knowledge_access.read_repo_doc(tool_ctx, path="llm-d-skills/../../etc/passwd")
