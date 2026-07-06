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
from app.tools.skill_gate import _TASK_BY_SUBCOMMAND
from tests.eval._skills import SKILL_TASKS

# Derived from the canonical skill-gate map so this test can't drift from the gate the runner
# actually enforces: every mutating llmdbenchmark subcommand the gate grounds is a grounded
# operation here, mapped to the same *_skill (run→benchmark_skill, experiment→compare_skill,
# smoketest→benchmark_skill, …).
_OPERATION_SUBCOMMANDS = dict(_TASK_BY_SUBCOMMAND)
# Mutating but NOT a standalone grounded operation: a verification sub-step that only runs inside
# an already-grounded flow and has no skill of its own. Currently none — smoketest is a grounded
# operation in the canonical gate — but kept as an (empty) set for the partition assertions below.
_EXEMPT_SUBSTEPS: set[str] = set()
# Mutating result-store plumbing (git-like add/rm/push/pull to a results store) — not a
# skill-grounded llm-d lifecycle operation, so no skill applies.
_EXEMPT_STORE = {
    "results.add", "results.rm", "results.push", "results.pull",
    "results.remote.add", "results.remote.rm",
}


def _mutating_subcommands() -> set[str]:
    """Every mutating llmdbenchmark subcommand, top-level AND nested, as dotted paths."""
    node = yaml.safe_load((PROJECT_ROOT / "security" / "allowlist.yaml").read_text())
    root = node["executables"]["llmdbenchmark"]
    out: set[str] = set()

    def _walk(subs: dict, prefix: str) -> None:
        for name, sub in subs.items():
            path = f"{prefix}{name}"
            if sub.get("mode") == "mutating":
                out.add(path)
            nested = sub.get("subcommands")
            if nested:
                _walk(nested, f"{path}.")

    _walk(root.get("subcommands", {}), "")
    return out


def test_every_mutating_subcommand_is_classified():
    """Each mutating llmdbenchmark subcommand is either a grounded operation or an exempt sub-step."""
    mutating = _mutating_subcommands()
    classified = set(_OPERATION_SUBCOMMANDS) | _EXEMPT_SUBSTEPS | _EXEMPT_STORE
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
