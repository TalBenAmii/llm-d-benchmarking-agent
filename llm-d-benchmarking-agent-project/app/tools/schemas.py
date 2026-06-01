"""Pydantic input models for every agent tool. These are the single source of truth
for each tool's argument schema: they validate the LLM's tool-call arguments
(determinism gate a) and are exported as JSON Schema in the provider tool definitions.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ProbeEnvironmentInput(BaseModel):
    checks: list[str] | Literal["all"] = Field(
        default="all",
        description="Which checks to run, or 'all'. Options: container_runtime, repos, "
                    "tools, venv, kind_clusters, kube_context, cluster_info, namespaces, stack",
    )
    namespace: str | None = Field(default=None, description="Namespace to check for an existing stack")


class ListCatalogInput(BaseModel):
    kinds: list[str] | None = Field(
        default=None,
        description="Subset to return: specs, harnesses, workloads, workloads_by_harness, scenarios. "
                    "Omit for everything.",
    )
    refresh: bool = Field(default=True, description="Re-scan the repo from disk")


class ReadRepoDocInput(BaseModel):
    path: str = Field(..., description="Repo-relative path, e.g. 'llm-d-benchmark/docs/quickstart.md'")
    max_bytes: int = Field(default=40_000, ge=1, le=200_000)


class FetchKeyDocsInput(BaseModel):
    task: str | None = Field(
        default=None,
        description="Filter to one task's docs (e.g. 'quickstart', 'optimized_baseline'). "
                    "Omit to fetch every pinned doc.",
    )
    max_bytes_each: int = Field(default=20_000, ge=1, le=80_000)


class RunCommandInput(BaseModel):
    argv: list[str] = Field(
        ...,
        description="The command as an argv list (NEVER a shell string), e.g. "
                    "['kind','create','cluster','--name','llmd-quickstart'] or "
                    "['install_prereqs.sh','--all']. Validated by the deny-by-default "
                    "allowlist; mutating commands require approval. Prefer a dedicated "
                    "tool when one exists.",
        min_length=1,
    )
    timeout: float | None = Field(default=None, description="Optional timeout in seconds")


class LocateReportInput(BaseModel):
    results_dir: str | None = Field(default=None, description="Explicit results directory, if known")
    session_id: str | None = None


class EnsureReposInput(BaseModel):
    repos: list[str] | None = Field(default=None, description="Subset of ['llm-d-benchmark','llm-d']; omit for both")
    ref: str | None = Field(default=None, description="Optional branch/tag")


class RunSetupInput(BaseModel):
    use_uv: bool = Field(default=True, description="Use uv to fetch Python 3.11 (recommended)")
    force: bool = Field(default=False, description="Re-run install.sh even if the venv exists")


class WriteConfigInput(BaseModel):
    artifact_type: Literal["workload", "run_config"]
    target_filename: str = Field(..., description="Bare *.yaml filename (no path separators)")
    content: dict[str, Any]


class ExecuteInput(BaseModel):
    subcommand: Literal["plan", "standup", "smoketest", "run", "teardown", "results", "experiment"]
    spec: str | None = Field(default=None, description="Spec name from the catalog, e.g. 'cicd/kind'")
    namespace: str | None = None
    harness: str | None = Field(default=None, description="run/experiment only")
    workload: str | None = Field(default=None, description="run/experiment only")
    flags: dict[str, Any] | None = Field(
        default=None,
        description="Optional: {skip_smoketest, dry_run, list_endpoints, methods, output, "
                    "endpoint_url}. For subcommand='experiment' (a DoE sweep over a treatments "
                    "file): {experiments (path to the experiment YAML), workspace (dir for "
                    "outputs), parallelism (int), overrides ('p=v,...'), stop_on_error, skip_teardown}.",
    )
    extra: list[str] | None = None


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


class ObserveRunMetricsInput(BaseModel):
    namespace: str = Field(..., description="Kubernetes namespace to read pod usage from "
                                            "(ignored for scope='nodes').")
    scope: Literal["pods", "nodes"] = Field(
        default="pods",
        description="'pods' = live CPU/memory of pods in the namespace; 'nodes' = node usage.",
    )
    run_id: str | None = Field(
        default=None,
        description="Narrow pod usage to ONE orchestrated run by its run-id label "
                    "(scope='pods' only). Omit to see all pods in the namespace.",
    )
    containers: bool = Field(
        default=False,
        description="Break pod usage down per-container (scope='pods' only).",
    )


class ResultHistoryInput(BaseModel):
    action: Literal["store", "list", "get", "trend", "delete"] = Field(
        ...,
        description="store = persist a validated Benchmark Report's summary for the long "
                    "term; list = show stored results (newest first); get = one record's full "
                    "summary; trend = the time-series of ONE metric across stored results; "
                    "delete = forget one record. All actions auto-run (none touches the "
                    "cluster or the repos).",
    )
    source: str | None = Field(
        default=None,
        description="action=store: a Benchmark Report file OR a run directory (its newest "
                    "report is used). The report is schema-validated before it is stored.",
    )
    label: str | None = Field(
        default=None,
        description="action=store: a short human label for this result, e.g. "
                    "'8B baseline, concurrency=16'.",
    )
    tags: list[str] | None = Field(
        default=None,
        description="action=store: free-form tags to group related results "
                    "(e.g. ['8B','baseline']); filter by one later with filter_tag.",
    )
    spec: str | None = Field(default=None, description="action=store: spec used (provenance).")
    harness: str | None = Field(default=None, description="action=store: harness used (provenance).")
    workload: str | None = Field(default=None, description="action=store: workload used (provenance).")
    namespace: str | None = Field(default=None, description="action=store: namespace used (provenance).")
    session_id: str | None = Field(default=None, description="action=store: originating chat id (provenance).")
    record_id: str | None = Field(
        default=None, description="action=get/delete: the stored record's id (from a prior list).",
    )
    metric: str | None = Field(
        default=None,
        description="action=trend: which metric to trend. One of ttft / tpot / itl / "
                    "request_latency / output_token_rate / total_token_rate / request_rate / "
                    "success_rate_pct.",
    )
    filter_tag: str | None = Field(
        default=None, description="action=list/trend: only include results carrying this tag.",
    )
    filter_model: str | None = Field(
        default=None, description="action=list/trend: only include results for this model name.",
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
