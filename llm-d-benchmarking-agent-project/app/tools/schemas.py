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
    subcommand: Literal["plan", "standup", "smoketest", "run", "teardown", "results"]
    spec: str | None = Field(default=None, description="Spec name from the catalog, e.g. 'cicd/kind'")
    namespace: str | None = None
    harness: str | None = Field(default=None, description="run only")
    workload: str | None = Field(default=None, description="run only")
    flags: dict[str, Any] | None = Field(
        default=None,
        description="Optional: {skip_smoketest, dry_run, list_endpoints, methods, output, endpoint_url}",
    )
    extra: list[str] | None = None
