"""Pydantic input models for the results-analysis / capacity / aggregation / comparison tools."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AnalyzeResultsInput(BaseModel):
    slo: dict[str, Any] | None = Field(
        default=None,
        description="QoS targets to filter results and estimate goodput. Keys (all optional, "
                    "at least one required): ttft_ms / tpot_ms / itl_ms / request_latency_ms "
                    "(max latency in ms), throughput_floor_tok_s (min output tokens/s), "
                    "min_success_rate_pct, percentile (which statistic latency SLOs are judged "
                    "at: mean/p50/p90/p95/p99/p99p9, default p99). Pass the SLOTargets captured "
                    "in the approved SessionPlan. Omit for a frontier-only sweep analysis.",
    )
    sources: list[str] | None = Field(
        default=None,
        description="1+ report files OR run directories to analyze (each dir uses its newest "
                    "Benchmark Report). For a single run, pass one; for an A/B, pass two.",
    )
    experiment_dir: str | None = Field(
        default=None,
        description="A DoE/sweep output dir to scan for ALL Benchmark Reports under it; use "
                    "instead of `sources` for a sweep (enables Pareto-frontier analysis).",
    )
    labels: list[str] | None = Field(
        default=None,
        description="Optional human labels parallel to `sources` (e.g. ['concurrency=1','concurrency=16']).",
    )


class CheckCapacityInput(BaseModel):
    spec: str = Field(
        ...,
        description="A spec name from the live catalog, e.g. 'cicd/kind' or 'examples/gpu'. "
                    "The pre-flight reads its scenario (model/accelerator/parallelism) from the repo.",
    )
    overrides: dict[str, Any] | None = Field(
        default=None,
        description="Conversation-derived deviations from the spec's defaults. Allowed keys: "
                    "model, huggingface_id, max_model_len, gpu_memory_utilization, gpu_memory_gb, "
                    "accelerator_count, tensor_parallelism, data_parallelism, decode_replicas, "
                    "prefill_replicas. E.g. {'model':'meta-llama/Llama-3.1-8B','max_model_len':8192,"
                    "'gpu_memory_gb':80}. Use these to reflect what the user actually asked for.",
    )
    enforce: bool = Field(
        default=False,
        description="When True, shortfalls are tagged ERROR (deployment-halting) rather than "
                    "advisory WARNING — the strict read a real standup uses when "
                    "ignoreFailedValidation is off.",
    )


class AggregateRunsInput(BaseModel):
    results_prefix: str = Field(
        ...,
        description="An EXISTING results dir holding the per-run result directories from "
                    "completed runs (the upstream naming convention is "
                    "'{results_prefix}/{harness}_{run_id}_{stack}'). This tool READS the "
                    "Benchmark Report v0.2 files there; it does NOT run a benchmark.",
    )
    harness: str = Field(
        ...,
        description="The harness whose repeated runs to aggregate (e.g. 'inference-perf') — "
                    "part of the per-run directory name.",
    )
    stack: str = Field(
        ...,
        description="The stack name the runs targeted (e.g. 'llm-d-7b-base') — part of the "
                    "per-run directory name.",
    )
    run_ids: list[str] = Field(
        ...,
        description="The run IDs to combine (>=2 — aggregation needs repeated runs of the SAME "
                    "benchmark to report run-to-run mean/std/min/max).",
    )
    output_name: str | None = Field(
        default=None,
        description="Optional subdir name (under the session workspace) to write the "
                    "aggregated_summary.{txt,json} into. Defaults to 'aggregated'. Must stay "
                    "within the workspace (no '..').",
    )


class CompareReportsInput(BaseModel):
    sources: list[str] | None = Field(
        default=None,
        description="2+ report files OR run directories to compare; each directory uses its "
                    "newest Benchmark Report. Use for an A/B of separate runs.",
    )
    experiment_dir: str | None = Field(
        default=None,
        description="A directory to scan for ALL Benchmark Reports under it (e.g. a DoE "
                    "experiment output/workspace dir). Use instead of `sources` for a sweep.",
    )
    labels: list[str] | None = Field(
        default=None,
        description="Optional human labels parallel to `sources` (e.g. ['concurrency=1','concurrency=16']).",
    )
    baseline_index: int = Field(
        default=0, ge=0,
        description="Index of the baseline run (deltas are computed relative to it).",
    )


class CompareHarnessRunsInput(BaseModel):
    sources: list[str] = Field(
        ...,
        description="2+ report files OR run directories produced by DIFFERENT harnesses in "
                    "this session (e.g. an inference-perf SLO-validation run dir and a "
                    "guidellm throughput-sweep dir). Each directory uses its newest Benchmark "
                    "Report; the harness that produced each is read from the report itself "
                    "(scenario.load.standardized.tool) — do not guess it.",
        min_length=2,
    )
    labels: list[str] | None = Field(
        default=None,
        description="Optional human labels parallel to `sources` (e.g. "
                    "['inference-perf SLO','guidellm sweep']). If omitted, the harness "
                    "name + run dir is used.",
    )
