"""Pydantic input models for the autotuner search-state tracker and the DoE experiment
generator."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.validation.session_plan import AutotuneKnob


class AutotuneSearchInput(BaseModel):
    """Search-state tracker for the closed-loop autotuner. MECHANISM ONLY — it tracks the
    trial log, VALIDATES the candidate YOU computed, and exposes FACTS. It NEVER computes the
    next config and NEVER returns a converge/stop verdict. The search STRATEGY and the STOP
    decision are YOURS, grounded in read_knowledge('autotune_strategy'). All actions auto-run
    (read/write only the session workspace; nothing touches the cluster or the repos)."""

    action: Literal["record_trial", "propose_next_config", "status"] = Field(
        ...,
        description="record_trial = log the result of a trial you just ran (validates the "
                    "Benchmark Report, evaluates it against the plan's SLO, appends it). "
                    "propose_next_config = ask the tool to VALIDATE the candidate config YOU "
                    "computed (in-bounds? duplicate? budget left?) BEFORE you run it — it does "
                    "NOT compute the value for you. status = read the convergence FACTS "
                    "(incumbent, feasible frontier, budget left, recent improvement, whether the "
                    "SLO boundary is bracketed) so YOU can decide converge/continue per "
                    "read_knowledge('autotune_strategy'). The tool returns NO converge/stop verdict.",
    )
    search_id: str = Field(
        ...,
        min_length=1,
        description="A stable id you pick for THIS goal-seeking session (e.g. "
                    "'chat-ttft-concurrency'). Keys the trial log; reuse it across every action "
                    "of the same search.",
    )
    slo: dict[str, Any] | None = Field(
        default=None,
        description="The SLO constraint from the approved SessionPlan's `slo` block (same keys "
                    "as analyze_results.slo: ttft_ms/tpot_ms/itl_ms/request_latency_ms/"
                    "throughput_floor_tok_s/min_success_rate_pct/percentile). Used to evaluate "
                    "each trial's feasibility and the SLO-feasible frontier (REUSES the analyzer). "
                    "Pass it on record_trial and status; the trial's feasibility comes from it.",
    )
    objective: str | None = Field(
        default=None,
        description="The objective metric to optimize, from the autotune plan (e.g. "
                    "'output_token_rate', 'ttft', 'tpot', 'request_latency'). Used to compute "
                    "each trial's objective_value and to pick the incumbent. Required for "
                    "record_trial and status to be meaningful.",
    )
    direction: Literal["max", "min"] | None = Field(
        default=None,
        description="'max' or 'min' for the objective (from the autotune plan). Used to choose "
                    "the incumbent and to sign the recent-improvement fact. Pure facts — never a "
                    "stop decision.",
    )
    config: dict[str, Any] | None = Field(
        default=None,
        description="action=record_trial: the knob value(s) used THIS trial, e.g. "
                    "{'max-concurrency': 16}. Keyed by each knob's dotted `key`.",
    )
    report_source: str | None = Field(
        default=None,
        description="action=record_trial: the run dir or Benchmark Report file this trial "
                    "produced. The report is schema-validated before it is recorded — an "
                    "unvalidated report is REFUSED, never logged (determinism gate d).",
    )
    candidate: dict[str, Any] | None = Field(
        default=None,
        description="action=propose_next_config: the next config YOU computed (per your "
                    "strategy), e.g. {'max-concurrency': 24}. The tool only VALIDATES it "
                    "(bounds/duplicate/budget); it does not produce or alter the value.",
    )
    knobs: list[AutotuneKnob] | None = Field(
        default=None,
        description="The knob bounds to validate a candidate against (the autotune plan's "
                    "knobs). Required for propose_next_config so out-of-bounds candidates are "
                    "rejected. Mirror the approved plan's bounds.",
    )
    budget: int | None = Field(
        default=None,
        ge=1,
        description="The trial budget from the approved autotune plan, so the tool can report "
                    "budget_remaining and reject a candidate once it's exhausted. Pass it on "
                    "propose_next_config and status.",
    )


class DoEFactor(BaseModel):
    """One swept parameter in a DoE matrix: a human `name`, the dotted config `key` the
    level overrides, and the list of `levels` to sweep. The cross-product of all factors'
    levels becomes the treatments. WHICH factor/levels to pick is your judgment (see
    knowledge/sweep_playbook.md) — this only declares one axis of the grid."""

    name: str = Field(
        ...,
        description="Short token naming this factor, used to build treatment names "
                    "(e.g. 'tp', 'rep', 'numCpuBlocks'). Letters/digits/_/-/. only.",
    )
    key: str = Field(
        ...,
        description="The DOTTED override key this factor sets in each treatment, e.g. "
                    "'decode.parallelism.tensor' or 'data.shared_prefix.num_groups' (setup "
                    "factors override the scenario config; run factors override the workload "
                    "profile). Read the repo's experiment examples to pick real keys.",
    )
    levels: list[Any] = Field(
        ...,
        description="The scalar values to sweep for this factor, e.g. [2, 4, 8]. The "
                    "cross-product of every factor's levels yields the treatments. Non-empty.",
        min_length=1,
    )


class GenerateDoeInput(BaseModel):
    name: str = Field(
        ...,
        description="Experiment name (a token: letters/digits/_/-/. only). Also the default "
                    "output filename (<name>.yaml).",
    )
    run_factors: list[DoEFactor] = Field(
        ...,
        description="REQUIRED. The workload/run factors to sweep against a single stood-up "
                    "stack (each: name + dotted key + levels). The cross-product of these is "
                    "the run treatments. Prefer a run-parameter sweep on kind/CPU-sim.",
        min_length=1,
    )
    setup_factors: list[DoEFactor] | None = Field(
        default=None,
        description="Optional infrastructure factors that change the DEPLOYMENT (replicas, "
                    "tensor parallelism, prefill/decode split, model). Each setup treatment "
                    "triggers its own standup/teardown — a full DoE. Omit for a run-only sweep "
                    "(one standup, N runs). The full matrix is setup × run treatments.",
    )
    run_constants: dict[str, Any] | None = Field(
        default=None,
        description="Optional dotted-key → value pairs held FIXED across every run treatment "
                    "(e.g. {'data.shared_prefix.output_len': 256}). Keep everything not being "
                    "swept fixed so deltas are attributable.",
    )
    setup_constants: dict[str, Any] | None = Field(
        default=None,
        description="Optional dotted-key → value pairs merged into every setup treatment "
                    "(e.g. {'model.maxModelLen': 16000}).",
    )
    harness: str | None = Field(
        default=None,
        description="Optional harness override recorded in the experiment metadata (e.g. "
                    "'inference-perf', 'vllm-benchmark'). Match the swept keys to the harness/"
                    "workload. Usually set on the scenario instead; omit if so.",
    )
    profile: str | None = Field(
        default=None, description="Optional workload-profile override recorded in the metadata.",
    )
    description: str | None = Field(
        default=None, description="Optional human description recorded in the experiment metadata.",
    )
    target_filename: str | None = Field(
        default=None,
        description="Optional bare *.yaml filename to write into the session workspace "
                    "(no path separators). Defaults to '<name>.yaml'.",
    )
