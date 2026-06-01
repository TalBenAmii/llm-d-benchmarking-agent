"""Tool registry + dispatch.

Maps each tool name to its input model and handler. Dispatch validates the LLM's
arguments against the Pydantic model (gate a) before calling the handler. Tool
definitions (name/description/JSON-Schema) are exported for the LLM providers.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from app.tools import (
    analyze,
    capacity,
    command,
    compare,
    config_artifact,
    execute,
    orchestrate,
    plan,
    probe,
    repos,
)
from app.tools.context import ToolContext
from app.tools.schemas import (
    AnalyzeResultsInput,
    CheckCapacityInput,
    CompareReportsInput,
    EnsureReposInput,
    ExecuteInput,
    FetchKeyDocsInput,
    ListCatalogInput,
    LocateReportInput,
    OrchestrateBenchmarkInput,
    ProbeEnvironmentInput,
    ReadRepoDocInput,
    RunCommandInput,
    RunSetupInput,
    WriteConfigInput,
)
from app.validation.session_plan import SessionPlan


@dataclass
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: Callable[..., Any]


_DESCRIPTIONS = {
    "probe_environment": (
        "Sense the local environment in one structured snapshot: container runtime, "
        "repos present, toolchain, venv, kind clusters, kube context/cluster reachability, "
        "namespaces, and whether a stack is already running in a namespace. Read-only; "
        "ALWAYS run this first before proposing or doing anything."
    ),
    "list_catalog": (
        "Enumerate the valid specs, harnesses, workload profiles, and scenarios that "
        "actually exist in the llm-d-benchmark repo on disk. Use this to ground every "
        "choice — never invent a spec/harness/workload name."
    ),
    "read_repo_doc": (
        "Read a documentation or spec file from inside the (read-only) repos, e.g. the "
        "quickstart guide. Use to confirm the authoritative flow/flags before acting."
    ),
    "fetch_key_docs": (
        "Fetch the LIVE content of the authoritative docs pinned in knowledge/key_docs.yaml "
        "(filter by task, e.g. 'quickstart'). The list of docs is fixed; content is read "
        "fresh from the clone. Read-only. Call this to ground yourself in the real procedure "
        "BEFORE proposing a deployment SessionPlan."
    ),
    "run_command": (
        "Run an allowlisted CLI command given as an argv list (e.g. ['kind','create',"
        "'cluster','--name','llmd-quickstart'] or ['install_prereqs.sh','--all']). The "
        "deny-by-default allowlist validates it; read-only commands auto-run, mutating ones "
        "need approval. Use for allowlisted commands without a dedicated tool — notably "
        "creating/deleting the kind cluster and installing the prerequisites (Docker + kind) "
        "via install_prereqs.sh. Prefer the dedicated tools (execute_llmdbenchmark, "
        "ensure_repos, run_setup) when one fits."
    ),
    "propose_session_plan": (
        "Propose a structured SessionPlan (use case, spec, namespace, harness, workload, "
        "flags, steps) for the user to APPROVE before any deployment. Enum fields are "
        "checked against the live catalog. You MUST get a plan approved before any "
        "mutating step (ensure_repos/run_setup/standup/run/teardown)."
    ),
    "check_capacity": (
        "Capacity PRE-FLIGHT: will this deployment fit? Runs the benchmark repo's OWN "
        "capacity planner over the spec's rendered config (model weights + activation + KV "
        "cache vs GPU memory, valid tensor-parallelism, max-context limits) and returns a "
        "feasible/infeasible verdict with the planner's diagnostics. Read-only; auto-runs. "
        "Pass `overrides` to reflect what the user actually asked for (a bigger model, "
        "longer context, a real GPU). Call this right after propose_session_plan and BEFORE "
        "standing anything up — it catches OOM / won't-load / can't-serve cases before a "
        "long standup fails opaquely. Interpret the verdict with knowledge/capacity.md. "
        "(Needs the benchmark venv: run_setup installs it.)"
    ),
    "ensure_repos": (
        "Clone the llm-d-benchmark and/or llm-d repos if missing (mutating; needs approval). "
        "Idempotent; never overwrites an existing directory."
    ),
    "run_setup": (
        "Run install.sh in the benchmark repo to build its Python venv and verify tools "
        "(mutating; needs approval). Required before any llmdbenchmark command."
    ),
    "write_and_validate_config": (
        "Write a generated workload/run config into the session workspace and validate it. "
        "MVP uses stock profiles, so you rarely need this."
    ),
    "execute_llmdbenchmark": (
        "Run the llmdbenchmark CLI: subcommand is one of plan/standup/smoketest/run/"
        "teardown/results/experiment. plan and --dry-run/--list-endpoints are read-only and "
        "auto-run; standup/run/teardown/experiment are mutating and require approval. "
        "'experiment' runs a full DoE sweep (standup+run+teardown per treatment) over an "
        "experiment YAML you pass via flags.experiments; its per-treatment reports land in "
        "the session workspace. Results from a 'run' are written into the session workspace too."
    ),
    "locate_and_parse_report": (
        "Find the newest Benchmark Report from a completed run, validate it against the "
        "repo schema, and return a plain-language metric summary. Read-only. Use after a run."
    ),
    "compare_reports": (
        "Compare 2+ Benchmark Reports side by side (an A/B of separate runs, or every "
        "report from a DoE 'experiment' sweep) and return per-metric deltas vs a baseline "
        "plus the winning run for each metric (latency: lower is better; throughput: higher). "
        "Read-only. Pass `sources` (run dirs/files, with optional `labels`) OR `experiment_dir` "
        "(scans for all reports under it). Use after a sweep or to compare two configurations."
    ),
    "analyze_results": (
        "Results Analyzer: SLO-aware filtering, goodput estimation, and Pareto/DoE analysis "
        "over one or more validated Benchmark Reports. Read-only. Pass the SLO targets from the "
        "approved SessionPlan (`slo`) plus either `sources` (1+ run dirs/files) or "
        "`experiment_dir` (a whole sweep). Returns, per run, whether it MEETS the SLOs and an "
        "honest goodput ESTIMATE (fraction of requests meeting the SLOs — the proposal's key "
        "differentiator; estimated from aggregate percentiles, flagged as such); for a sweep it "
        "also returns the Pareto-optimal configs and the SLO-feasible frontier (best trade-off "
        "subject to the constraints). Use after a run or sweep when the user has QoS targets or "
        "wants the best config. compare_reports gives raw deltas; this adds SLO/goodput/Pareto."
    ),
    "orchestrate_benchmark_run": (
        "Run a benchmark as a Kubernetes Job the orchestrator manages end-to-end: submit "
        "(approval-gated `kubectl apply`), watch the Job to completion, stream logs, and on "
        "failure classify the cause (OOM / timeout / eviction / unschedulable / image / run "
        "error). With max_attempts>1, a TRANSIENT fault (eviction) retries as a fresh, distinct "
        "Job; deterministic faults never retry. Distinct from execute_llmdbenchmark (which runs "
        "the CLI locally as a blocking subprocess): use this for K8s-native, restart-resilient, "
        "individually-retryable runs. Needs the orchestrator container image (config "
        "ORCHESTRATOR_IMAGE or `image`)."
    ),
}


def build_registry() -> dict[str, ToolSpec]:
    specs = [
        ToolSpec("probe_environment", _DESCRIPTIONS["probe_environment"], ProbeEnvironmentInput, probe.probe_environment),
        ToolSpec("list_catalog", _DESCRIPTIONS["list_catalog"], ListCatalogInput, probe.list_catalog),
        ToolSpec("read_repo_doc", _DESCRIPTIONS["read_repo_doc"], ReadRepoDocInput, probe.read_repo_doc),
        ToolSpec("fetch_key_docs", _DESCRIPTIONS["fetch_key_docs"], FetchKeyDocsInput, probe.fetch_key_docs),
        ToolSpec("propose_session_plan", _DESCRIPTIONS["propose_session_plan"], SessionPlan, plan.propose_session_plan),
        ToolSpec("check_capacity", _DESCRIPTIONS["check_capacity"], CheckCapacityInput, capacity.check_capacity),
        ToolSpec("ensure_repos", _DESCRIPTIONS["ensure_repos"], EnsureReposInput, repos.ensure_repos),
        ToolSpec("run_setup", _DESCRIPTIONS["run_setup"], RunSetupInput, repos.run_setup),
        ToolSpec("write_and_validate_config", _DESCRIPTIONS["write_and_validate_config"], WriteConfigInput, config_artifact.write_and_validate_config),
        ToolSpec("execute_llmdbenchmark", _DESCRIPTIONS["execute_llmdbenchmark"], ExecuteInput, execute.execute_llmdbenchmark),
        ToolSpec("run_command", _DESCRIPTIONS["run_command"], RunCommandInput, command.run_command),
        ToolSpec("locate_and_parse_report", _DESCRIPTIONS["locate_and_parse_report"], LocateReportInput, probe.locate_and_parse_report),
        ToolSpec("compare_reports", _DESCRIPTIONS["compare_reports"], CompareReportsInput, compare.compare_reports),
        ToolSpec("analyze_results", _DESCRIPTIONS["analyze_results"], AnalyzeResultsInput, analyze.analyze_results),
        ToolSpec("orchestrate_benchmark_run", _DESCRIPTIONS["orchestrate_benchmark_run"], OrchestrateBenchmarkInput, orchestrate.orchestrate_benchmark_run),
    ]
    return {s.name: s for s in specs}


REGISTRY = build_registry()


def tool_definitions() -> list[dict[str, Any]]:
    """Export {name, description, input_schema} for the LLM providers."""
    out = []
    for spec in REGISTRY.values():
        schema = spec.input_model.model_json_schema()
        out.append({"name": spec.name, "description": spec.description, "input_schema": schema})
    return out


async def dispatch(ctx: ToolContext, name: str, raw_input: dict[str, Any]) -> dict[str, Any]:
    """Validate args against the tool's schema, then run the handler. Validation errors
    are returned (not raised) so the agent can self-correct and retry."""
    spec = REGISTRY.get(name)
    if spec is None:
        return {"error": f"unknown tool {name!r}", "valid_tools": sorted(REGISTRY)}

    try:
        model = spec.input_model.model_validate(raw_input or {})
    except ValidationError as exc:
        return {"error": "invalid arguments", "details": exc.errors(include_url=False)}

    kwargs = model.model_dump()
    result = spec.handler(ctx, **kwargs)
    if inspect.isawaitable(result):
        result = await result
    return result
