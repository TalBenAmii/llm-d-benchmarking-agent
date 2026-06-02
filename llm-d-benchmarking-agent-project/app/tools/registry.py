"""Tool registry + dispatch.

Maps each tool name to its input model and handler. Dispatch validates the LLM's
arguments against the Pydantic model (gate a) before calling the handler. Tool
definitions (name/description/JSON-Schema) are exported for the LLM providers.
"""
from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from app.tools import (
    analyze,
    cancel,
    capacity,
    command,
    compare,
    config_artifact,
    doe,
    execute,
    history,
    multiharness,
    observe,
    orchestrate,
    plan,
    probe,
    repos,
)
from app.tools.context import ToolContext
from app.tools.schemas import (
    AnalyzeResultsInput,
    CancelRunInput,
    CheckCapacityInput,
    CompareHarnessRunsInput,
    CompareReportsInput,
    EnsureReposInput,
    ExecuteInput,
    FetchKeyDocsInput,
    GenerateDoeInput,
    ListCatalogInput,
    LocateReportInput,
    ObserveRunMetricsInput,
    OrchestrateBenchmarkInput,
    ProbeEnvironmentInput,
    ReadKnowledgeInput,
    ReadRepoDocInput,
    ResultHistoryInput,
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
    "read_knowledge": (
        "Load the FULL text of ONE of the agent's on-demand knowledge guides by topic name "
        "(e.g. read_knowledge('capacity')). The system prompt inlines the core guides and "
        "lists the rest in a knowledge index with their topics; call this to pull in the "
        "relevant guide BEFORE interpreting that kind of result or making that decision. "
        "Read-only; auto-runs. On an unknown name it returns the valid topics."
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
        "flags, steps) for the user to APPROVE. Enum fields are checked against the live "
        "catalog. Required and approved before any mutating step "
        "(ensure_repos/run_setup/standup/run/teardown)."
    ),
    "check_capacity": (
        "Capacity PRE-FLIGHT: will this deployment fit? Runs the benchmark repo's OWN "
        "capacity planner over the spec's rendered config (model weights + activation + KV "
        "cache vs GPU memory, valid tensor-parallelism, max-context limits) and returns a "
        "feasible/infeasible verdict with the planner's diagnostics. Read-only; auto-runs. "
        "Pass `overrides` to reflect what the user actually asked for (a bigger model, "
        "longer context, a real GPU). Call this right after propose_session_plan and BEFORE "
        "standing anything up — it catches OOM / won't-load / can't-serve cases before a "
        "long standup fails opaquely. Call read_knowledge('capacity') to interpret the "
        "verdict. (Needs the benchmark venv: run_setup installs it.)"
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
    "generate_doe_experiment": (
        "AUTHOR a Design-of-Experiments (DoE) experiment YAML: you supply the FACTORS to "
        "sweep — `run_factors` (workload knobs swept against one stack; REQUIRED) and "
        "optional `setup_factors` (infrastructure knobs that change the deployment, each its "
        "own standup/teardown) — where each factor is {name, dotted `key`, list of `levels`}. "
        "The tool CROSS-PRODUCTS the levels into the full, deduped, named treatments matrix "
        "(setup × run), writes a valid experiment YAML into the session workspace, and "
        "validates it structurally against the repo's own experiment examples. Read-only "
        "(only writes the workspace), auto-runs. Then pass the returned `path` as "
        "flags.experiments to execute_llmdbenchmark (subcommand='experiment' for a full DoE, "
        "or 'run' for a run-parameter sweep) — preview with flags.dry_run first. WHICH "
        "factors/levels to sweep is YOUR judgment: call read_knowledge('sweep_playbook') to "
        "pick them (e.g. the optimal prefill/decode ratio) and to elicit token characteristics "
        "(input/output length distributions, system-prompt reuse → prefix sharing). Read the "
        "repo's experiment examples (read_repo_doc) to pick real override keys."
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
    "compare_harness_runs": (
        "Cross-harness comparison for a MULTI-HARNESS session: contrast Benchmark Reports "
        "produced by DIFFERENT harnesses (e.g. an inference-perf SLO/latency-validation run "
        "and a guidellm throughput-sweep run) against the SAME stack. Read-only. Pass "
        "`sources` (2+ run dirs/files, one or more per harness); the harness that produced "
        "each is read from the report itself (never guessed). Returns each harness's runs + "
        "the metric families it measured, which metrics ≥2 harnesses both measured (so you "
        "can cross-validate) vs only one did, and the per-harness values side by side WITHOUT "
        "a winner (different load generators aren't directly comparable). Use this AFTER "
        "running both harnesses in one session. compare_reports contrasts configs of the "
        "SAME harness; this contrasts the harnesses. Call read_knowledge('multi_harness') "
        "to interpret it."
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
        "wants the best config. compare_reports gives raw deltas; this adds SLO/goodput/Pareto. "
        "Call read_knowledge('analysis') to interpret it (and read_knowledge('sweep_playbook') "
        "when designing or reading a sweep/A-B)."
    ),
    "observe_run_metrics": (
        "Read LIVE cluster resource usage (CPU/memory) during a run via `kubectl top`. "
        "scope='pods' shows pod usage in a namespace (optionally narrowed to one orchestrated "
        "run by run_id, or per-container); scope='nodes' shows node usage. Read-only "
        "(auto-runs). Use it WHILE a benchmark is running to see if the model server / harness "
        "is near its CPU or memory limit (a leading indicator of an OOM/throttle). Requires the "
        "in-cluster metrics-server (present in the cicd/kind spec); if it is missing the tool "
        "reports that and changes nothing. Distinct from /metrics, which exposes the agent's "
        "OWN Prometheus counters. Call read_knowledge('observability') to interpret the numbers."
    ),
    "result_history": (
        "Persist VALIDATED Benchmark Report summaries across sessions and read trends over "
        "time. All actions auto-run (nothing here touches the cluster or the repos). "
        "action='store' saves a report (pass `source` = a report file or run dir, plus "
        "optional `label`/`tags`/`spec`/`harness`/`workload`); it validates the report first "
        "and is idempotent (storing the same report twice keeps one record). 'list' shows "
        "stored results newest first (filter by `filter_tag`/`filter_model`); 'get' returns "
        "one record's full summary by `record_id`; 'trend' returns the time-series of ONE "
        "`metric` (ttft/tpot/itl/request_latency/output_token_rate/total_token_rate/"
        "request_rate/success_rate_pct) across stored runs; 'delete' forgets a record. Store "
        "a result the user wants to keep AFTER you've parsed/analyzed it; use 'trend' to "
        "answer 'has performance regressed over time?'. The tool returns facts (values + "
        "direction); call read_knowledge('history') to interpret trends and give the verdict."
    ),
    "cancel_run": (
        "Cancel a still-running background run/turn in ANOTHER chat by its session id (from "
        "/api/sessions or a `ready` event). Use this to free a concurrency slot held by an "
        "abandoned or clearly-stuck run so a new run can start, or when the user changed their "
        "mind about a run they navigated away from. Cancelling frees the run's concurrency-cap "
        "slot AND cleans up its subprocess (no orphaned process / leaked Job). Auto-runs (it "
        "STOPS work; it starts no mutation). Idempotent — a session with no live run reports "
        "cancelled=false. You cannot cancel the run you are calling from. Judgment on WHEN to "
        "cancel is in knowledge/run_lifecycle.md."
    ),
    "orchestrate_benchmark_run": (
        "Run a benchmark as a Kubernetes Job the orchestrator manages end-to-end: submit "
        "(approval-gated `kubectl apply`), watch to completion, stream logs, and on failure "
        "classify the cause (OOM / timeout / eviction / unschedulable / image / run error). "
        "With max_attempts>1 a TRANSIENT fault (eviction) retries as a fresh, distinct Job; "
        "deterministic faults never retry. Unlike execute_llmdbenchmark (which runs the CLI "
        "locally as a blocking subprocess), use this for K8s-native, restart-resilient, "
        "individually-retryable runs. Needs the orchestrator container image (config "
        "ORCHESTRATOR_IMAGE or `image`). Pass an optional `scheduling` object to request a "
        "GPU type/count and to PLACE the Job so it does not starve the measured llm-d stack "
        "(node affinity / tolerations / pod anti-affinity via `avoid_labels`) — call "
        "read_knowledge('resource_management') for HOW to choose; omit it for the generic "
        "cpu/memory baseline. Call read_knowledge('orchestrator') to choose between this and "
        "execute_llmdbenchmark and to interpret a failure classification."
    ),
}


def build_registry() -> dict[str, ToolSpec]:
    specs = [
        ToolSpec("probe_environment", _DESCRIPTIONS["probe_environment"], ProbeEnvironmentInput, probe.probe_environment),
        ToolSpec("list_catalog", _DESCRIPTIONS["list_catalog"], ListCatalogInput, probe.list_catalog),
        ToolSpec("read_knowledge", _DESCRIPTIONS["read_knowledge"], ReadKnowledgeInput, probe.read_knowledge),
        ToolSpec("read_repo_doc", _DESCRIPTIONS["read_repo_doc"], ReadRepoDocInput, probe.read_repo_doc),
        ToolSpec("fetch_key_docs", _DESCRIPTIONS["fetch_key_docs"], FetchKeyDocsInput, probe.fetch_key_docs),
        ToolSpec("propose_session_plan", _DESCRIPTIONS["propose_session_plan"], SessionPlan, plan.propose_session_plan),
        ToolSpec("check_capacity", _DESCRIPTIONS["check_capacity"], CheckCapacityInput, capacity.check_capacity),
        ToolSpec("ensure_repos", _DESCRIPTIONS["ensure_repos"], EnsureReposInput, repos.ensure_repos),
        ToolSpec("run_setup", _DESCRIPTIONS["run_setup"], RunSetupInput, repos.run_setup),
        ToolSpec("write_and_validate_config", _DESCRIPTIONS["write_and_validate_config"], WriteConfigInput, config_artifact.write_and_validate_config),
        ToolSpec("generate_doe_experiment", _DESCRIPTIONS["generate_doe_experiment"], GenerateDoeInput, doe.generate_doe_experiment),
        ToolSpec("execute_llmdbenchmark", _DESCRIPTIONS["execute_llmdbenchmark"], ExecuteInput, execute.execute_llmdbenchmark),
        ToolSpec("run_command", _DESCRIPTIONS["run_command"], RunCommandInput, command.run_command),
        ToolSpec("locate_and_parse_report", _DESCRIPTIONS["locate_and_parse_report"], LocateReportInput, probe.locate_and_parse_report),
        ToolSpec("compare_reports", _DESCRIPTIONS["compare_reports"], CompareReportsInput, compare.compare_reports),
        ToolSpec("compare_harness_runs", _DESCRIPTIONS["compare_harness_runs"], CompareHarnessRunsInput, multiharness.compare_harness_runs),
        ToolSpec("analyze_results", _DESCRIPTIONS["analyze_results"], AnalyzeResultsInput, analyze.analyze_results),
        ToolSpec("result_history", _DESCRIPTIONS["result_history"], ResultHistoryInput, history.result_history),
        ToolSpec("orchestrate_benchmark_run", _DESCRIPTIONS["orchestrate_benchmark_run"], OrchestrateBenchmarkInput, orchestrate.orchestrate_benchmark_run),
        ToolSpec("observe_run_metrics", _DESCRIPTIONS["observe_run_metrics"], ObserveRunMetricsInput, observe.observe_run_metrics),
        ToolSpec("cancel_run", _DESCRIPTIONS["cancel_run"], CancelRunInput, cancel.cancel_run),
    ]
    return {s.name: s for s in specs}


REGISTRY = build_registry()


def _strip_titles(node: Any) -> Any:
    """Recursively drop Pydantic's auto-generated ``title`` keys from a JSON Schema.
    Titles carry no behavioral meaning for the LLM (the field NAME is the key and the
    ``description`` carries the intent), so removing them cuts tokens with zero semantic
    change. Everything else (description/type/enum/properties/required/$ref/$defs/anyOf/
    items/default/…) is preserved verbatim."""
    if isinstance(node, dict):
        return {k: _strip_titles(v) for k, v in node.items() if k != "title"}
    if isinstance(node, list):
        return [_strip_titles(v) for v in node]
    return node


def tool_definitions() -> list[dict[str, Any]]:
    """Export {name, description, input_schema} for the LLM providers."""
    out = []
    for spec in REGISTRY.values():
        schema = _strip_titles(spec.input_model.model_json_schema())
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
