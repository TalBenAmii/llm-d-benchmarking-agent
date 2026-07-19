"""The skill-fetch tools must always be reachable so the agent can ground any operation.

fetch_key_docs / read_repo_doc (and the plan tool the mandate gates) are exposed to the model —
every registered tool always is — so the request-time skill mandate is always satisfiable in a
single step. Instant/hermetic.
"""
from __future__ import annotations

import pytest

from app.tools.registry import tool_definitions


@pytest.mark.parametrize("tool", ["fetch_key_docs", "read_repo_doc", "propose_session_plan"])
def test_skill_grounding_tools_are_exposed(tool):
    """The tools the skill mandate relies on are in the exposed tool surface."""
    assert tool in {d["name"] for d in tool_definitions()}, f"{tool} must be exposed to the model"
