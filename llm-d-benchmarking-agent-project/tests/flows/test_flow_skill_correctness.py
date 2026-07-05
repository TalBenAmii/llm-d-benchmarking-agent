"""Every grounded operation flow fetches the RIGHT skill for the operations it runs.

Stronger than test_flow_skill_grounding (which only checks that *some* skill is fetched
BEFORE the first operation): this asserts the *set* of *_skill fetches EXACTLY matches the
operations the flow performs or plans — a deploy+benchmark flow grounds in deploy_skill AND
benchmark_skill; a teardown grounds in teardown_skill and nothing spurious. Catches both
mis-grounding (wrong skill for the op) and under/over-grounding. Hermetic, sibling-independent.
"""
from __future__ import annotations

import pytest

from tests.flows.flows import ALL_FLOWS

# Each operation step (an execute_llmdbenchmark subcommand or a propose_session_plan
# expected_steps entry) maps to the llm-d skill the mandate requires grounding it in.
_SKILL_FOR_STEP = {
    "standup": "deploy_skill",
    "run": "benchmark_skill",
    "teardown": "teardown_skill",
    "compare": "compare_skill",
    "sweep": "compare_skill",
    "autoscale": "wva_skill",
}
_OPERATION_TOOLS = {"propose_session_plan", "execute_llmdbenchmark"}

# Documented non-grounded transcripts (rationale in test_flow_skill_grounding).
_EXEMPT = {"safety-refusal", "error-catalog-drift-denied"}


def _calls(flow):
    return [tc for t in flow.turns for tc in t.tool_calls]


def _operation_steps(flow) -> set[str]:
    steps: set[str] = set()
    for tc in _calls(flow):
        if tc.name == "execute_llmdbenchmark":
            sub = tc.input.get("subcommand")
            if sub:
                steps.add(sub)
        elif tc.name == "propose_session_plan":
            steps.update(tc.input.get("expected_steps", []))
    return steps


def _fetched_skills(flow) -> set[str]:
    return {
        tc.input["task"]
        for tc in _calls(flow)
        if tc.name == "fetch_key_docs" and str(tc.input.get("task", "")).endswith("_skill")
    }


def _required_skills(flow) -> set[str]:
    return {_SKILL_FOR_STEP[s] for s in _operation_steps(flow) if s in _SKILL_FOR_STEP}


def _has_operation(flow) -> bool:
    return any(tc.name in _OPERATION_TOOLS for tc in _calls(flow))


_GROUNDED_OP_FLOWS = [f for f in ALL_FLOWS if _has_operation(f) and f.name not in _EXEMPT]


@pytest.mark.parametrize("flow", _GROUNDED_OP_FLOWS, ids=lambda f: f.name)
def test_grounded_skills_exactly_match_operations(flow):
    """The *_skill fetches equal the skills required by the flow's operation steps."""
    required = _required_skills(flow)
    fetched = _fetched_skills(flow)
    assert required, f"{flow.name} has no recognized operation steps to ground"
    missing = required - fetched
    spurious = fetched - required
    assert not missing, f"{flow.name} runs {sorted(required)} ops but never grounds in {sorted(missing)}"
    assert not spurious, f"{flow.name} grounds in {sorted(spurious)} with no matching operation"


def test_skill_for_step_covers_only_real_skills():
    """Every *_skill named by the step map is a real skill task (guards typos/drift)."""
    from tests.eval._skills import SKILL_TASKS

    unknown = set(_SKILL_FOR_STEP.values()) - set(SKILL_TASKS)
    assert not unknown, f"step map references unknown skills: {unknown}"
