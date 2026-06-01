"""The SessionPlan — the structured, user-approved contract the agent proposes before
any deployment. It turns a fuzzy conversation into an inspectable object whose enum
fields are cross-checked against the live on-disk catalog (determinism gate b).
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.validation.analysis import SLOTargets

_RFC1123 = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")


class SessionPlan(BaseModel):
    use_case_summary: str = Field(..., description="Restated user intent, e.g. 'chat app, ~500 concurrent users'")
    goal_metrics: list[str] = Field(default_factory=list, description="e.g. ['ttft','throughput']")
    slo: SLOTargets | None = Field(
        default=None,
        description="Optional QoS targets captured during the interview (max TTFT/TPOT/ITL/"
                    "request-latency in ms, min throughput floor in tokens/s, success-rate "
                    "floor). Used later by analyze_results to filter results and estimate "
                    "goodput — the proposal's key differentiator. Omit if the user has none.",
    )
    spec: str = Field(..., description="A spec name from the live catalog, e.g. 'cicd/kind'")
    deploy_path: Literal["kind_sim", "guide", "gpu"] = "kind_sim"
    namespace: str = Field(..., description="RFC1123 label, e.g. 'llmd-quickstart'")
    harness: str = Field(..., description="A harness from the catalog, e.g. 'inference-perf'")
    workload: str = Field(..., description="A workload profile, e.g. 'sanity_random.yaml'")
    flags: dict[str, Any] = Field(default_factory=dict)
    expected_steps: list[str] = Field(default_factory=list)
    est_duration_hint: str | None = None
    reversible: bool = True
    notes: str | None = None


def validate_plan(plan: SessionPlan, catalog: dict[str, Any]) -> list[str]:
    """Cross-check the plan's enum fields against the live catalog. Returns a list of
    human-readable errors (empty == valid)."""
    errors: list[str] = []
    specs = catalog.get("specs", [])
    harnesses = catalog.get("harnesses", [])
    workloads = set(catalog.get("workloads", []))

    if specs and plan.spec not in specs:
        errors.append(f"spec {plan.spec!r} is not in the catalog (have e.g. {specs[:5]})")
    if harnesses and plan.harness not in harnesses:
        errors.append(f"harness {plan.harness!r} is not in the catalog ({harnesses})")
    if workloads:
        norm = {plan.workload, plan.workload.removesuffix(".yaml"), f"{plan.workload}.yaml"}
        if not (norm & workloads):
            errors.append(f"workload {plan.workload!r} is not in the catalog for any harness")
    if not _RFC1123.fullmatch(plan.namespace):
        errors.append(f"namespace {plan.namespace!r} is not a valid RFC1123 label")
    return errors
