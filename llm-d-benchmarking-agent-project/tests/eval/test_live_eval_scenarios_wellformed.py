"""Meta-guards on the live skill-usage eval's own data (SCENARIOS + _OPERATION_TOOLS).

Keeps the live eval honest: unique scenario keys, well-formed *_skill dirs, and an
operation gate that keys on REAL registered tools. Hermetic; no repos, no LLM.
"""
from __future__ import annotations

import pytest

from app.tools.registry import REGISTRY
from tests.eval.simulate.test_skill_usage_live import _OPERATION_TOOLS, SCENARIOS


def test_scenarios_non_empty_and_keys_unique():
    """There is at least one scenario and no duplicate keys."""
    assert SCENARIOS, "no skill scenarios defined"
    keys = [s.key for s in SCENARIOS]
    assert len(keys) == len(set(keys)), f"duplicate scenario keys: {keys}"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.key for s in SCENARIOS])
def test_each_scenario_is_well_formed(scenario):
    """Each scenario names a grounding task, a real ask, and a read-path prefix under the repos."""
    assert scenario.key.endswith("_skill") or scenario.key == "quickstart", (
        f"scenario key not a *_skill task or quickstart: {scenario.key}")
    assert scenario.ask.strip() and len(scenario.ask) > 20, "scenario ask is trivial"
    if scenario.key.endswith("_skill"):
        assert scenario.read_prefix.startswith("llm-d-skills/skills/"), scenario.read_prefix
        assert scenario.read_prefix.endswith("/"), "a *_skill read_prefix should be a dir prefix"
    else:  # quickstart grounds in an llm-d-benchmark docs runbook, not a skills dir
        assert scenario.read_prefix.startswith("llm-d-benchmark/"), scenario.read_prefix


def test_operation_tools_are_real_registered_tools():
    """The eval's operation gate keys on tools that actually exist in the registry."""
    assert _OPERATION_TOOLS, "no operation tools defined"
    unknown = [t for t in _OPERATION_TOOLS if t not in REGISTRY]
    assert not unknown, f"_OPERATION_TOOLS references unregistered tools: {unknown}"
