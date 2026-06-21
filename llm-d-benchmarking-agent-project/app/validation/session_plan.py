"""The SessionPlan — the structured, user-approved contract the agent proposes before
any deployment. It turns a fuzzy conversation into an inspectable object whose enum
fields are cross-checked against the live on-disk catalog (determinism gate b).
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.validation.analysis import SLOTargets

# Reuse the DoE dotted-key vocabulary VERBATIM so an autotune knob's `key` follows the same
# rules the DoE generator / scenario authoring validate (e.g. ``decode.parallelism.tensor``).
from app.validation.doe import _KEY_RE

_RFC1123 = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")

# Upper bound on the bounded autotune budget that ONE plan-approval authorizes. A search is
# inherently iterative; this caps how many trials a single approval can spend so the user is
# never surprised by an unbounded loop. NOT a convergence decision — just the budget ceiling.
_MAX_AUTOTUNE_BUDGET = 50


class AutotuneKnob(BaseModel):
    """One tunable knob in a goal-seeking search: a human ``name``, the dotted config ``key``
    its value overrides (reusing the DoE key vocabulary), and the declared ``[min, max]``
    bounds (plus an optional ``resolution`` — the smallest step worth distinguishing).

    Mechanism only: this declares the BOX the agent's candidate must fall inside. WHICH knob
    to tune and WHAT bounds to set is the agent's judgment — see
    read_knowledge('autotune_strategy') and read_knowledge('sweep_playbook')."""

    name: str = Field(
        ...,
        description="Short token naming this knob, e.g. 'concurrency' or 'tp'. Used in trial "
                    "labels.",
    )
    key: str = Field(
        ...,
        description="The DOTTED override key this knob sets each trial, e.g. 'max-concurrency' "
                    "or 'decode.parallelism.tensor'. Same vocabulary as a DoE factor key.",
    )
    min: float = Field(..., description="Lower bound of the search range for this knob (inclusive).")
    max: float = Field(..., description="Upper bound of the search range for this knob (inclusive).")
    resolution: float | None = Field(
        default=None, gt=0,
        description="Optional smallest meaningful step for this knob (e.g. 1 for an integer "
                    "concurrency). Used by the agent's convergence rubric (knowledge), not by "
                    "Python — the tool never computes a step.",
    )

    @model_validator(mode="after")
    def _check(self) -> AutotuneKnob:
        if not _KEY_RE.fullmatch(self.key):
            raise ValueError(
                f"knob key {self.key!r} must be a dotted override key like "
                "'decode.parallelism.tensor'"
            )
        if self.max <= self.min:
            raise ValueError(f"knob {self.name!r}: max ({self.max}) must be greater than min ({self.min})")
        return self


class AutotunePlan(BaseModel):
    """The bounded goal-seeking (autotuner) block of a SessionPlan. It rides the EXISTING
    plan-approval path, so ONE upfront approval authorizes "up to ``budget`` trials within
    these knob bounds" — the per-trial runs still go through the normal approval gate.

    ``strategy`` is a NAME only (e.g. 'coordinate-descent', 'bisection') — the strategy is
    DESCRIBED as a procedure in knowledge/autotune_strategy.md and EXECUTED by the agent, not
    enumerated as logic here. ``objective`` + ``direction`` name what to optimize subject to
    the plan's ``slo`` constraint. This model carries NO search/convergence logic; it is a
    declaration the agent gets approved once."""

    strategy: str = Field(
        ...,
        description="The NAME of the search strategy the agent will execute (described in "
                    "knowledge/autotune_strategy.md), e.g. 'coordinate-descent', 'bisection', "
                    "'hill-climb', 'bayesian-lite'. A label only — the strategy is the agent's.",
    )
    objective: str = Field(
        ...,
        description="The analyzer objective to optimize, e.g. 'output_token_rate', 'ttft', "
                    "'tpot', 'request_latency'. Optimized SUBJECT TO the plan's slo constraint.",
    )
    direction: Literal["max", "min"] = Field(
        ...,
        description="'max' (e.g. throughput) or 'min' (e.g. latency) for the objective.",
    )
    knobs: list[AutotuneKnob] = Field(
        ...,
        min_length=1,
        description="The knob(s) to search over, each with its dotted key and [min,max] bounds. "
                    "Usually one knob in v1 (e.g. concurrency); the schema allows several for "
                    "coordinate-descent.",
    )
    budget: int = Field(
        ...,
        ge=1,
        le=_MAX_AUTOTUNE_BUDGET,
        description="Max number of trials this ONE approval authorizes (the bounded search "
                    "budget). The agent stops at or before this — running out of budget is one "
                    "stop condition in its convergence rubric (knowledge/autotune_strategy.md).",
    )


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
    autotune: AutotunePlan | None = Field(
        default=None,
        description="Optional CLOSED-LOOP GOAL-SEEKING block. Set this when the user states a "
                    "GOAL ('hit X at the best Y you can') rather than 'compare these N configs'. "
                    "It names the search strategy, the objective to maximize/minimize subject to "
                    "`slo`, the knob(s) + bounds to search, and the trial budget — so ONE approval "
                    "authorizes the whole bounded search. The agent then drives the autotune_search "
                    "tool per trial. Omit for a normal one-shot/sweep run. See "
                    "read_knowledge('autotune_strategy').",
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
    by_harness = catalog.get("workloads_by_harness") or {}

    if specs and plan.spec not in specs:
        errors.append(f"spec {plan.spec!r} is not in the catalog (have e.g. {specs[:5]})")
    if harnesses and plan.harness not in harnesses:
        errors.append(f"harness {plan.harness!r} is not in the catalog ({harnesses})")
    # The run uses the (harness, workload) PAIR — a profile valid for harness B is not a valid
    # `-w` for harness A. When the per-harness map is available AND lists the chosen harness,
    # scope the workload check to THAT harness so a cross-harness mismatch (which the flat
    # union would wrongly accept) is rejected. Fall back to the union otherwise (partial/
    # absent catalog), preserving the original behavior.
    norm = {plan.workload, plan.workload.removesuffix(".yaml"), f"{plan.workload}.yaml"}
    scoped = by_harness.get(plan.harness) if isinstance(by_harness, dict) else None
    if scoped is not None:
        if not (norm & set(scoped)):
            errors.append(
                f"workload {plan.workload!r} is not a profile for harness {plan.harness!r} "
                f"(have {sorted(scoped)})"
            )
    elif workloads and not (norm & workloads):
        errors.append(f"workload {plan.workload!r} is not in the catalog for any harness")
    if not _RFC1123.fullmatch(plan.namespace):
        errors.append(f"namespace {plan.namespace!r} is not a valid RFC1123 label")
    return errors
