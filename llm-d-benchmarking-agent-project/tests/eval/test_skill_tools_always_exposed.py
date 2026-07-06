"""The skill-fetch tools must always be reachable so the agent can ground any operation.

fetch_key_docs / read_repo_doc are in the STARTER_KIT (shown by default, no load_tools),
so the request-time skill mandate is always satisfiable in a single step. Instant/hermetic.
"""
from __future__ import annotations

import pytest

from app.tools.registry import STARTER_KIT


@pytest.mark.parametrize("tool", ["fetch_key_docs", "read_repo_doc", "propose_session_plan"])
def test_skill_grounding_tools_in_starter_kit(tool):
    """The tools the skill mandate relies on are exposed by default (never gated)."""
    assert tool in STARTER_KIT, f"{tool} must be exposed by default (not gated behind load_tools)"


def test_execute_llmdbenchmark_is_gated_not_starter():
    """Sanity anchor: the run tool IS gated — proves STARTER_KIT membership is meaningful."""
    assert "execute_llmdbenchmark" not in STARTER_KIT
