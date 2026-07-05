"""No mutating llmdbenchmark operation is missing a skill grounding.

Derives the set of MUTATING subcommands from the security allowlist (the source of truth that
gates the runner), then asserts every one is classified — either a grounded operation (mapped
to a real *_skill in SKILL_TASKS) or an explicitly-exempt sub-step (a mutating verification
step that only ever runs inside an already-grounded operation and has no dedicated skill of its
own). If a new mutating subcommand is ever added to the allowlist, the partition assertion fails
until it is classified — guarding against an un-grounded operation slipping in unnoticed.

Hermetic: reads the checked-in allowlist + the skill task map; no cluster, no LLM, no siblings.
"""
from __future__ import annotations

import yaml

from app.config import PROJECT_ROOT
from tests.eval._skills import SKILL_TASKS

# The one benchmark skill grounds both a single `run` and a multi-config `experiment` — the
# llm-d-skills library has exactly five skills, with no dedicated experiment/smoketest skill.
_OPERATION_SUBCOMMANDS = {
    "standup": "deploy_skill",
    "run": "benchmark_skill",
    "experiment": "benchmark_skill",
    "teardown": "teardown_skill",
}
# Mutating but NOT a standalone grounded operation: a readiness/verification probe that only
# runs inside a deploy/benchmark flow (already grounded) and has no skill of its own.
_EXEMPT_SUBSTEPS = {"smoketest"}


def _mutating_subcommands() -> set[str]:
    node = yaml.safe_load((PROJECT_ROOT / "security" / "allowlist.yaml").read_text())
    subs = node["executables"]["llmdbenchmark"]["subcommands"]
    return {name for name, sub in subs.items() if sub.get("mode") == "mutating"}


def test_every_mutating_subcommand_is_classified():
    """Each mutating llmdbenchmark subcommand is either a grounded operation or an exempt sub-step."""
    mutating = _mutating_subcommands()
    classified = set(_OPERATION_SUBCOMMANDS) | _EXEMPT_SUBSTEPS
    orphan = mutating - classified  # a mutating op with neither a skill nor an exemption
    stale = classified - mutating  # a classification for a subcommand that no longer mutates/exists
    assert not orphan, f"mutating subcommands lack a skill mapping: {sorted(orphan)}"
    assert not stale, f"classified subcommands are not mutating in the allowlist: {sorted(stale)}"


def test_operation_subcommands_map_to_real_skills():
    """Every grounded operation maps to a real *_skill task."""
    unknown = set(_OPERATION_SUBCOMMANDS.values()) - set(SKILL_TASKS)
    assert not unknown, f"operation map references unknown skills: {sorted(unknown)}"


def test_operations_and_substeps_are_disjoint():
    """A subcommand is either a grounded operation or an exempt sub-step, never both."""
    assert not (set(_OPERATION_SUBCOMMANDS) & _EXEMPT_SUBSTEPS)


def test_golden_step_map_agrees_on_shared_subcommands():
    """The golden-flow correctness step-map and this operation map agree where they overlap."""
    from tests.flows.test_flow_skill_correctness import _SKILL_FOR_STEP

    for sub, skill in _OPERATION_SUBCOMMANDS.items():
        if sub in _SKILL_FOR_STEP:
            assert _SKILL_FOR_STEP[sub] == skill, f"{sub}: {skill} vs {_SKILL_FOR_STEP[sub]}"
