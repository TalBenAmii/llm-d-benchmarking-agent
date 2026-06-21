"""Pydantic input models for the K8s-native orchestrator tools (run / sweep / drill /
cancel / manage)."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class OrchestrateBenchmarkInput(BaseModel):
    namespace: str = Field(..., description="Kubernetes namespace to run the benchmark Job in")
    spec: str | None = Field(default=None, description="llm-d spec from the catalog, e.g. 'cicd/kind'")
    harness: str | None = Field(default=None, description="Harness name from the catalog")
    workload: str | None = Field(default=None, description="Workload profile from the catalog")
    image: str | None = Field(
        default=None,
        description="Container image for the Job; defaults to the configured orchestrator image. "
                    "Required (here or in config) — an orchestrated run is a real K8s Job.",
    )
    service_account: str | None = Field(
        default=None,
        description="ServiceAccount the Job pod runs under; defaults to the configured "
                    "orchestrator service account (the least-privilege SA the deploy creates). "
                    "Leave unset to use the namespace default SA.",
    )
    command: list[str] | None = Field(
        default=None,
        description="Override the in-Job argv; default runs 'llmdbenchmark run' with the given "
                    "spec/harness/workload/namespace.",
    )
    cpu: str = Field(default="1", description="CPU request/limit for the Job pod")
    memory: str = Field(default="1Gi", description="Memory request/limit for the Job pod")
    scheduling: dict[str, Any] | None = Field(
        default=None,
        description="Optional hardware/placement intent for the Job pod (see "
                    "knowledge/resource_management.md for HOW to choose). Keys (all optional): "
                    "gpu_count (int >=1, request N GPUs), gpu_resource (extended-resource name, "
                    "default 'nvidia.com/gpu'), gpu_type_label ([label_key, value] to pin the GPU "
                    "TYPE, e.g. ['nvidia.com/gpu.product','NVIDIA-A100-SXM4-80GB']), node_selector "
                    "(dict of exact node-label matches), tolerations (list of K8s toleration dicts "
                    "for tainted GPU pools), affinity (a raw K8s affinity block, merged verbatim), "
                    "avoid_labels (dict — schedules the benchmark pod AWAY from nodes already "
                    "running pods with these labels, e.g. the measured llm-d stack {'llm-d.ai/role':"
                    "'decode'}, so the load generator never starves the system under test), "
                    "avoid_topology_key (topology domain for avoid_labels, default "
                    "'kubernetes.io/hostname'). Omit entirely for the generic cpu/memory baseline.",
    )
    active_deadline_seconds: int | None = Field(
        default=None, description="Job timeout; exceeding it is classified as a timeout failure",
    )
    max_attempts: int = Field(
        default=1, ge=1, le=5,
        description="Retry budget for TRANSIENT faults (eviction). Each attempt is a fresh, "
                    "distinct Job. Deterministic faults (OOM/unschedulable/image) never retry.",
    )
    watch: bool = Field(default=True, description="Watch the Job to completion + diagnose failures (vs submit-and-return)")
    poll_interval: float = Field(default=3.0, ge=0, description="Seconds between status polls while watching")
    max_wait: float = Field(default=3600.0, ge=0, description="Max seconds to watch before giving up")
    require_ready_endpoint: bool = Field(
        default=True,
        description="Gate submission on a real inference-endpoint READINESS check (a Service "
                    "with a ready backing endpoint — beyond mere pod presence). When the stack "
                    "isn't ready the run is NOT submitted (nothing mutates) and a structured "
                    "not-ready result with a standup_suggestion is returned. Set False ONLY if "
                    "you know the endpoint is reachable another way (e.g. an external -U URL).",
    )


class SweepTreatment(BaseModel):
    """One treatment in a parallel DoE sweep: a benchmark run with its own workload/run
    parameters, executed as its own Kubernetes Job against the SHARED stood-up stack. WHICH
    treatments to run is your judgment — usually the run treatments emitted by
    generate_doe_experiment (read_knowledge('sweep_playbook')); this only declares one run."""

    name: str = Field(
        ...,
        description="Unique, short, human-readable label for this treatment (e.g. 'rate-10', "
                    "'tp2-rate20'). It identifies the treatment in the result roll-up AND, "
                    "slugified, forms the Job's stable run-id — so the SAME name in a later "
                    "resume call maps to the SAME treatment (already-completed ones are skipped). "
                    "Keep names stable across resumes; letters/digits/_/-/. only.",
    )
    spec: str | None = Field(default=None, description="Per-treatment llm-d spec override; falls back to the top-level `spec`.")
    harness: str | None = Field(default=None, description="Per-treatment harness override; falls back to the top-level `harness`.")
    workload: str | None = Field(
        default=None,
        description="Per-treatment workload profile (the usual swept axis in a run-parameter "
                    "sweep); falls back to the top-level `workload`.",
    )
    command: list[str] | None = Field(
        default=None,
        description="Override the in-Job argv for this treatment; default runs 'llmdbenchmark "
                    "run' with this treatment's effective spec/harness/workload/namespace. Use "
                    "this to encode run-parameter variations the workload name can't express.",
    )
    cpu: str | None = Field(default=None, description="Per-treatment CPU request/limit; falls back to the top-level `cpu`.")
    memory: str | None = Field(default=None, description="Per-treatment memory request/limit; falls back to the top-level `memory`.")


class OrchestrateSweepInput(BaseModel):
    namespace: str = Field(..., description="Kubernetes namespace the sweep's Jobs run in (all treatments share this stood-up stack)")
    treatments: list[SweepTreatment] = Field(
        ...,
        description="The treatments to run in parallel (each its own Job + retry/dead-letter "
                    "budget). Typically the run treatments from generate_doe_experiment. "
                    "Treatment names must be unique. A single treatment is allowed but the value "
                    "of this tool is N>1.",
        min_length=1,
    )
    spec: str | None = Field(default=None, description="Default llm-d spec for treatments that don't override it, e.g. 'cicd/kind'.")
    harness: str | None = Field(default=None, description="Default harness for treatments that don't override it.")
    workload: str | None = Field(default=None, description="Default workload profile for treatments that don't override it.")
    image: str | None = Field(
        default=None,
        description="Container image for the Jobs; defaults to the configured orchestrator "
                    "image. Required (here or in config) — an orchestrated sweep is real K8s Jobs.",
    )
    service_account: str | None = Field(
        default=None,
        description="ServiceAccount the Job pods run under; defaults to the configured "
                    "orchestrator service account. Leave unset to use the namespace default SA.",
    )
    cpu: str = Field(default="1", description="Default CPU request/limit per treatment Job pod.")
    memory: str = Field(default="1Gi", description="Default memory request/limit per treatment Job pod.")
    scheduling: dict[str, Any] | None = Field(
        default=None,
        description="Optional hardware/placement intent applied to EVERY treatment Job (same "
                    "keys as orchestrate_benchmark_run's `scheduling`: gpu_count, gpu_resource, "
                    "gpu_type_label, node_selector, tolerations, affinity, avoid_labels, "
                    "avoid_topology_key — see knowledge/resource_management.md). Omit for the "
                    "generic cpu/memory baseline.",
    )
    active_deadline_seconds: int | None = Field(
        default=None, description="Per-treatment Job timeout; exceeding it is classified as a timeout failure.",
    )
    max_parallel: int = Field(
        default=2, ge=1, le=16,
        description="Concurrency cap — at most this many treatment Jobs run at once (the "
                    "proposal's configurable parallel-scheduling knob). The rest queue. Size it "
                    "to the cluster's spare capacity so parallel runs don't contend.",
    )
    max_attempts: int = Field(
        default=2, ge=1, le=5,
        description="Per-treatment retry budget for TRANSIENT faults (eviction). Each attempt is "
                    "a fresh, distinct Job. A persistently-failing treatment dead-letters without "
                    "sinking the rest of the sweep.",
    )
    poll_interval: float = Field(default=3.0, ge=0, description="Seconds between status polls while watching each treatment.")
    max_wait: float = Field(default=3600.0, ge=0, description="Max seconds to watch a single treatment before giving up on it.")
    sweep_id: str | None = Field(
        default=None,
        description="Resume key. Omit on a FRESH sweep — one is generated and RETURNED. To "
                    "RESUME an interrupted sweep, pass back the returned sweep_id WITH the same "
                    "treatments: completed treatments are skipped (read from the cluster "
                    "checkpoint) and only the remainder runs. DNS-label-safe (lowercase "
                    "alphanumeric/'-', short).",
    )
    checkpoint: bool = Field(
        default=True,
        description="Persist sweep progress to a cluster ConfigMap so the sweep is "
                    "restart-resumable (the source of truth for what's done). Set False for a "
                    "stateless one-shot sweep with no checkpoint writes (then resume is not "
                    "possible).",
    )
    require_ready_endpoint: bool = Field(
        default=True,
        description="Gate the sweep on a real inference-endpoint READINESS check before "
                    "submitting ANY treatment (all treatments share one stack). When not ready, "
                    "NOTHING is submitted and a structured not-ready result with a "
                    "standup_suggestion is returned. Set False only if the endpoint is reachable "
                    "another way.",
    )


class RunResilienceDrillInput(BaseModel):
    namespace: str = Field(..., description="Kubernetes namespace label for the drill (the drill "
                                            "runs against an in-process/fake cluster — nothing is "
                                            "mutated on a real cluster).")
    spec: str | None = Field(default=None, description="llm-d spec from the catalog (annotation only), e.g. 'cicd/kind'")
    harness: str | None = Field(default=None, description="Harness name from the catalog (annotation only)")
    workload: str | None = Field(default=None, description="Workload profile from the catalog (annotation only)")
    image: str | None = Field(default=None, description="Container image annotation for the drill's synthetic Job spec")
    chaos_plan: dict[str, Any] | None = Field(
        default=None,
        description="The fault-injection plan (the agent's judgment — see "
                    "read_knowledge('resilience') for WHICH faults to inject). Shape: "
                    "{seed: int, injections: [ {kind, at_attempt, point, probability, "
                    "exit_code?, message?}, ... ]}. `kind` is one of evicted/oom/unschedulable/"
                    "image_error/run_error/timeout/unknown (evicted+unknown are TRANSIENT → the "
                    "orchestrator retries them; the rest dead-letter). `at_attempt` (>=1) targets "
                    "that attempt's Job — inject `evicted` at attempt 1 to prove a retry succeeds. "
                    "`point` is 'before-watch' (default) or 'mid-watch'. `probability` in [0,1] "
                    "(default 1.0). A bad shape returns an error you can self-correct.",
    )
    max_attempts: int = Field(
        default=3, ge=1, le=5,
        description="Retry budget for the drilled run. Each attempt is a fresh, distinct Job — "
                    "so a transient fault at attempt 1 can be retried and succeed at attempt 2.",
    )
    prove_restart: bool = Field(
        default=True,
        description="Also prove orchestrator-restart durability: a FRESH orchestrator (no local "
                    "state) reconstructs the run / resumes a sweep from the cluster checkpoint with "
                    "0 duplicate Jobs.",
    )
    slo_budget_s: float = Field(
        default=600.0, ge=0,
        description="Wall-clock SLO budget (seconds) for the drill; the report states whether the "
                    "drill completed within it.",
    )


class CancelRunInput(BaseModel):
    session_id: str = Field(
        ...,
        description="The chat/session id whose in-flight run to cancel (as shown in "
                    "/api/sessions or the `ready` event's session_id). Cancelling frees the "
                    "run's concurrency slot and cleans up its subprocess. You cannot cancel the "
                    "run you are calling from — cancel a DIFFERENT session's run.",
        min_length=1,
    )


class ManageOrchestratedRunsInput(BaseModel):
    namespace: str = Field(
        ...,
        description="Kubernetes namespace whose orchestrated benchmark Jobs to list/stop/reap.",
    )
    action: Literal["list", "stop", "cleanup"] = Field(
        default="list",
        description="list = show the agent-managed Jobs and their phase, classified fresh from "
                    "the cluster (read-only, auto-runs). stop = DELETE the still-running Jobs in "
                    "scope (approval-gated) to ACTUALLY stop cluster work — cancel_run only stops "
                    "the in-process watch, so a submitted Job keeps running after it. cleanup = "
                    "reap only TERMINAL Jobs to tidy the namespace (approval-gated; in-flight runs "
                    "are never touched). Deleting a Job preserves results on the PVC.",
    )
    session_id: str | None = Field(
        default=None,
        description="Scope to ONE session's orchestrated Jobs (the chat/session id stamped on the "
                    "Job labels at submit time). Omit to span every agent-managed Job in the "
                    "namespace. Pair with action=stop after cancel_run to halt that session's "
                    "still-running cluster Jobs.",
    )
    sweep_id: str | None = Field(
        default=None,
        description="Scope to ONE DoE sweep's treatment Jobs (the sweep_id orchestrate_sweep "
                    "returned). Pair with action=stop to halt a whole running sweep at once.",
    )
