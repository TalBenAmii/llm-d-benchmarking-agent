"""Shared fixtures for the hermetic llm-d-skills eval tests."""
from __future__ import annotations

import pytest

from tests.eval._skills import skills_populated


@pytest.fixture()
def skills_ctx(tool_ctx):
    """A ToolContext requiring the read-only skills repo; skipped when it's absent.

    The populated-check probes fetch_key_docs and resets ctx.fetched_docs inside
    skills_populated(), so tests start with a clean dedup set.
    """
    if not skills_populated(tool_ctx):
        pytest.skip("llm-d-skills sibling repo not materialized (set REPOS_DIR to a populated checkout)")
    return tool_ctx
