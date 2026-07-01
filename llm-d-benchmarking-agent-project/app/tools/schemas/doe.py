"""Pydantic input models for the DoE experiment generator."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


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
