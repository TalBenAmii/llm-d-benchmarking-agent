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
