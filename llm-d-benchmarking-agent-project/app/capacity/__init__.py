"""Capacity pre-flight (Phase 6) — a feasibility check the agent runs at the plan gate.

Mechanism only. It resolves a spec into the same rendered plan_config the benchmark
repo's standup would build, lets the agent apply conversation-derived overrides, then
defers to the REPO's OWN capacity planner (via the vetted ``scripts/bridges/capacity_check.py``
bridge) for the verdict. The judgment of how to read that verdict — when a plan is
infeasible and what to change — lives in ``knowledge/capacity.md``, not here.
"""
from app.capacity.planner import (
    CapacityVerdict,
    classify_diagnostics,
    merge_gated_access,
    plan_config_for_spec,
    resolve_scenario_file,
)

__all__ = [
    "CapacityVerdict",
    "classify_diagnostics",
    "merge_gated_access",
    "plan_config_for_spec",
    "resolve_scenario_file",
]
