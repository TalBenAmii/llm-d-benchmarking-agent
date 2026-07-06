"""Every grounded operation flow fetches the RIGHT grounding doc for its operations.

Two-tier (mirrors the merged skill-gate policy): an operation on the kind/CPU-sim path
(spec `cicd/kind`) grounds in the `quickstart` runbook; an operation on a guide/GPU spec
grounds in its own `*_skill`. Asserts the set of grounding fetches EXACTLY matches what the
flow's operation steps require — catching mis-grounding and under/over-grounding. Hermetic.
"""
from __future__ import annotations

import pytest

from tests.flows.flows import ALL_FLOWS

# Per-operation-step skill for the GUIDE/GPU path. The kind/CPU-sim path (spec cicd/kind)
# overrides every one of these to the `quickstart` runbook (see _required_grounding).
_SKILL_FOR_STEP = {
    "standup": "deploy_skill",
    "run": "benchmark_skill",
    "teardown": "teardown_skill",
    "compare": "compare_skill",
    "sweep": "compare_skill",
    "autoscale": "wva_skill",
}
_OPERATION_TOOLS = {"propose_session_plan", "execute_llmdbenchmark"}
_KIND_TASK = "quickstart"
_GROUNDING_TASKS = set(_SKILL_FOR_STEP.values()) | {_KIND_TASK}

# Documented non-grounded transcripts (rationale in test_flow_skill_grounding).
_EXEMPT = {"safety-refusal", "error-catalog-drift-denied"}


def _calls(flow):
    return [tc for t in flow.turns for tc in t.tool_calls]


def _is_kind_spec(spec) -> bool:
    return bool(spec) and str(spec).startswith("cicd/")


def _required_grounding(flow) -> set[str]:
    """Grounding tasks the flow's operations require: quickstart on the kind path, else each
    step's own *_skill."""
    req: set[str] = set()
    for tc in _calls(flow):
        if tc.name == "execute_llmdbenchmark":
            sub = tc.input.get("subcommand")
            if sub in _SKILL_FOR_STEP:
                req.add(_KIND_TASK if _is_kind_spec(tc.input.get("spec")) else _SKILL_FOR_STEP[sub])
        elif tc.name == "propose_session_plan":
            kind = _is_kind_spec(tc.input.get("spec"))
            for step in tc.input.get("expected_steps", []):
                if step in _SKILL_FOR_STEP:
                    req.add(_KIND_TASK if kind else _SKILL_FOR_STEP[step])
    return req


def _fetched_grounding(flow) -> set[str]:
    return {
        tc.input["task"]
        for tc in _calls(flow)
        if tc.name == "fetch_key_docs" and tc.input.get("task") in _GROUNDING_TASKS
    }


def _has_operation(flow) -> bool:
    return any(tc.name in _OPERATION_TOOLS for tc in _calls(flow))


_GROUNDED_OP_FLOWS = [f for f in ALL_FLOWS if _has_operation(f) and f.name not in _EXEMPT]


@pytest.mark.parametrize("flow", _GROUNDED_OP_FLOWS, ids=lambda f: f.name)
def test_grounded_skills_exactly_match_operations(flow):
    """The grounding fetches equal what the flow's operations require (kind→quickstart, guide→*_skill)."""
    required = _required_grounding(flow)
    fetched = _fetched_grounding(flow)
    assert required, f"{flow.name} has no recognized operation steps to ground"
    missing = required - fetched
    spurious = fetched - required
    assert not missing, f"{flow.name} requires {sorted(required)} but never grounds in {sorted(missing)}"
    assert not spurious, f"{flow.name} grounds in {sorted(spurious)} with no matching operation"


def test_skill_for_step_covers_only_real_skills():
    """Every *_skill named by the step map is a real skill task (guards typos/drift)."""
    from tests.eval._skills import SKILL_TASKS

    unknown = set(_SKILL_FOR_STEP.values()) - set(SKILL_TASKS)
    assert not unknown, f"step map references unknown skills: {sorted(unknown)}"
