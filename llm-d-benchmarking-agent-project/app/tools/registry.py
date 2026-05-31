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

from app.tools import config_artifact, execute, plan, probe, repos
from app.tools.context import ToolContext
from app.tools.schemas import (
    EnsureReposInput,
    ExecuteInput,
    ListCatalogInput,
    LocateReportInput,
    ProbeEnvironmentInput,
    ReadRepoDocInput,
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
    "propose_session_plan": (
        "Propose a structured SessionPlan (use case, spec, namespace, harness, workload, "
        "flags, steps) for the user to APPROVE before any deployment. Enum fields are "
        "checked against the live catalog. You MUST get a plan approved before any "
        "mutating step (ensure_repos/run_setup/standup/run/teardown)."
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
        "teardown/results. plan and --dry-run/--list-endpoints are read-only and auto-run; "
        "standup/run/teardown are mutating and require approval. Results from a 'run' are "
        "written into the session workspace."
    ),
    "locate_and_parse_report": (
        "Find the newest Benchmark Report from a completed run, validate it against the "
        "repo schema, and return a plain-language metric summary. Read-only. Use after a run."
    ),
}


def build_registry() -> dict[str, ToolSpec]:
    specs = [
        ToolSpec("probe_environment", _DESCRIPTIONS["probe_environment"], ProbeEnvironmentInput, probe.probe_environment),
        ToolSpec("list_catalog", _DESCRIPTIONS["list_catalog"], ListCatalogInput, probe.list_catalog),
        ToolSpec("read_repo_doc", _DESCRIPTIONS["read_repo_doc"], ReadRepoDocInput, probe.read_repo_doc),
        ToolSpec("propose_session_plan", _DESCRIPTIONS["propose_session_plan"], SessionPlan, plan.propose_session_plan),
        ToolSpec("ensure_repos", _DESCRIPTIONS["ensure_repos"], EnsureReposInput, repos.ensure_repos),
        ToolSpec("run_setup", _DESCRIPTIONS["run_setup"], RunSetupInput, repos.run_setup),
        ToolSpec("write_and_validate_config", _DESCRIPTIONS["write_and_validate_config"], WriteConfigInput, config_artifact.write_and_validate_config),
        ToolSpec("execute_llmdbenchmark", _DESCRIPTIONS["execute_llmdbenchmark"], ExecuteInput, execute.execute_llmdbenchmark),
        ToolSpec("locate_and_parse_report", _DESCRIPTIONS["locate_and_parse_report"], LocateReportInput, probe.locate_and_parse_report),
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
