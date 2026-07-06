"""Hermetic guards for the llm-d-skills wiring the agent grounds each operation in.

These assert the *data + resolution* layer the live skill-usage eval
(tests/eval/simulate/test_skill_usage_live.py) depends on: every operation's
`*_skill` task in knowledge/key_docs.yaml must resolve to a real, non-empty
SKILL.md under the read-only llm-d-skills repo, and fetch_key_docs / read_repo_doc
must actually return that skill's body. Free + always-run; skipped only when the
read-only sibling repos aren't materialized (e.g. a bare git worktree — set
REPOS_DIR to a populated checkout).
"""
from __future__ import annotations

import pytest

from app.tools import knowledge_access
from tests.eval._skills import SKILL_TASKS


@pytest.mark.parametrize("task", sorted(SKILL_TASKS))
def test_skill_task_resolves_to_real_skill_md(skills_ctx, task):
    """Each *_skill task fetches a real, non-empty SKILL.md under its skill dir."""
    res = knowledge_access.fetch_key_docs(skills_ctx, task=task)
    assert res["task"] == task
    docs = res["docs"]
    assert docs, f"{task} resolved to zero docs"
    skill_docs = [d for d in docs if d["path"].endswith("SKILL.md")]
    assert skill_docs, f"{task} has no SKILL.md doc"
    for d in skill_docs:
        assert d["path"].startswith(SKILL_TASKS[task]), (
            f"{task} SKILL.md path {d['path']} not under {SKILL_TASKS[task]}"
        )
        assert d.get("found") is True, f"{task} SKILL.md not found: {d.get('reason')}"
        assert d.get("content", "").strip(), f"{task} SKILL.md content empty"
        assert len(d["content"]) > 200, f"{task} SKILL.md suspiciously short"


def test_all_skill_tasks_listed_in_available_tasks(skills_ctx):
    """fetch_key_docs advertises every *_skill task in available_tasks."""
    res = knowledge_access.fetch_key_docs(skills_ctx)  # no task -> all
    available = set(res.get("available_tasks", []))
    missing = set(SKILL_TASKS) - available
    assert not missing, f"skill tasks missing from key_docs.yaml available_tasks: {missing}"


def test_skill_tasks_map_to_distinct_dirs():
    """No two operations point at the same skill directory."""
    dirs = list(SKILL_TASKS.values())
    assert len(dirs) == len(set(dirs)), "duplicate skill dirs across operations"


def test_read_repo_doc_returns_each_skill_body(skills_ctx):
    """read_repo_doc (the alt path the eval accepts) reads each skill's SKILL.md."""
    for skill_dir in SKILL_TASKS.values():
        path = skill_dir + "SKILL.md"
        res = knowledge_access.read_repo_doc(skills_ctx, path=path)
        assert res.get("content", "").strip(), f"empty body for {path}"
