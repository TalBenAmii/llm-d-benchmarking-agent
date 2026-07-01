"""Tool registry + dispatch.

Maps each tool name to its input model and handler. Dispatch validates the LLM's
arguments against the Pydantic model (gate a) before calling the handler. Tool
definitions (name/description/JSON-Schema) are exported for the LLM providers.
"""
from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from app.readiness import check_endpoint_readiness
from app.tools import (
    aggregate_runs as aggregate_runs_tool,
)
from app.tools import (
    analyze,
    autotune,
    cancel,
    capacity,
    compare,
    config_artifact,
    convert_guide,
    discover,
    doe,
    execute,
    hf_secret,
    history,
    knowledge_access,
    manage_runs,
    multiharness,
    observe,
    orchestrate,
    plan,
    probe,
    report_locate,
    repos,
    reproducibility,
    shell,
    suggest,
    tool_loader,
    workload_profile,
)
from app.tools.context import ToolContext
from app.tools.schemas import (
    AdviseAcceleratorsInput,
    AggregateRunsInput,
    AnalyzeResultsInput,
    AutotuneSearchInput,
    CancelRunInput,
    CheckCapacityInput,
    CheckEndpointReadinessInput,
    CompareHarnessRunsInput,
    CompareReportsInput,
    ConvertGuideInput,
    DiscoverStackInput,
    EnsureReposInput,
    EstimateRunDurationInput,
    ExecuteInput,
    ExportRunBundleInput,
    FetchKeyDocsInput,
    GenerateDoeInput,
    InspectWorkloadProfileInput,
    ListCatalogInput,
    LoadToolsInput,
    LocateReportInput,
    ManageOrchestratedRunsInput,
    ObserveRunMetricsInput,
    OrchestrateBenchmarkInput,
    OrchestrateSweepInput,
    ProbeEnvironmentInput,
    ProvisionHfSecretInput,
    ReadKnowledgeInput,
    ReadRepoDocInput,
    ReproduceRunInput,
    ResultHistoryInput,
    RunSetupInput,
    RunShellInput,
    SearchKnowledgeInput,
    SuggestNextStepsInput,
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
    "load_tools": (
        "Load one or more tool GROUPS for the rest of this session, then call the tool you need "
        "(the group's tools appear in your tool list THIS same turn). Most tools are grouped and "
        "hidden by default to keep your list lean; the groups are: 'setup' (deploy & pre-flight), "
        "'run' (execute & monitor a benchmark), 'analyze' (results), and 'advanced' (power features "
        "— sweeps, autotuning, DoE, run export/reproduce, cross-run/-harness "
        "comparison, scenario authoring). Pass `groups` (e.g. ['run'] or ['setup','run']). Call "
        "this the MOMENT the user's request needs a grouped tool — whether their stack is already "
        "up, they have prior results to analyze, or they want to reproduce a run. Read-only, no "
        "side effect, no approval — never tell the user you cannot do something; just load the "
        "group and do it."
    ),
    "probe_environment": (
        "Sense the local environment in one structured snapshot: container runtime, "
        "repos present, toolchain, venv, kind clusters, kube context/cluster reachability, "
        "namespaces, and whether a stack is already running in a namespace. Read-only; "
        "ALWAYS run this first before proposing or doing anything. Add checks=['provider_detection'] "
        "to detect the cloud provider (openshift/gke/doks/aks vs kind) from node labels + surface "
        "GPU taints, then read_knowledge('infra_providers') to pick the right CLI/toleration/fix."
    ),
    "list_catalog": (
        "Enumerate the valid specs, harnesses, workload profiles, and scenarios that "
        "actually exist in the llm-d-benchmark repo on disk. Use this to ground every "
        "choice — never invent a spec/harness/workload name."
    ),
    "inspect_workload_profile": (
        "PREVIEW what a workload profile actually SENDS, before running it, so a non-expert can "
        "see what they're about to benchmark. Read-only; auto-runs. Pass the profile name as the "
        "agent uses it (e.g. 'chatbot_synthetic.yaml') and an optional harness (omitted = search "
        "every harness dir, inference-perf first). Returns a NORMALIZED, AUDITABLE factual summary "
        "— token shape, load shape, and prompt/dataset source — each field tagged with the raw key "
        "it came from (`_from`); on a name that doesn't exist it lists the profiles that DO exist. "
        "FACTS ONLY — WHICH workload to pick is your judgment; "
        "read_knowledge('welllit_path_advisor')/sweep_playbook, not this tool."
    ),
    "estimate_run_duration": (
        "Rough PRE-RUN wall-clock estimate for a workload profile. Read-only; auto-runs. Reads the "
        "same profile as inspect_workload_profile and computes a clearly-labeled HEURISTIC "
        "estimate from the load shape, ALWAYS returning the `basis`, the stated `assumption`, and "
        "`approximate=True` (it excludes standup/warmup/teardown). If the profile lacks "
        "duration/rate/request-count fields it returns `estimable=False` and SAYS what's missing "
        "rather than inventing a number. Whether the duration is acceptable is your judgment."
    ),
    "advise_accelerators": (
        "Accelerator / CPU-inferencing PRE-FLIGHT: \"can my hardware actually run this?\" "
        "Read-only; auto-runs. Reads each node's ADVERTISED resources via the already-allowlisted "
        "`kubectl get nodes -o json` and reports which extended-resource key a node advertises "
        "(nvidia.com/gpu or the amd/habana/google-tpu/Intel-XPU siblings) vs CPU-only, plus each "
        "node's cpu + memory. FACTS ONLY — no verdict. It COMPLEMENTS check_capacity (which sizes "
        "weights + KV cache against GPU MEMORY): this answers whether a node even ADVERTISES the "
        "accelerator (or, if CPU-only, meets the floor). Call read_knowledge('accelerators') to "
        "turn the facts into a verdict (driver/CUDA minimums, Device-Plugin vs DRA, the CPU-only "
        "floor + the Kind/CPU-sim exemption). Use this at the plan gate alongside check_capacity, "
        "BEFORE a standup; the judgment is in the guide, this tool is only the mechanism."
    ),
    "read_knowledge": (
        "Load the FULL text of ONE of the agent's on-demand knowledge guides by topic name "
        "(e.g. read_knowledge('capacity')). The system prompt inlines the core guides and "
        "lists the rest in a knowledge index with their topics; call this to pull in the "
        "relevant guide BEFORE interpreting that kind of result or making that decision. "
        "Read-only; auto-runs. On an unknown name it returns the valid topics."
    ),
    "search_knowledge": (
        "SEARCH the agent's knowledge base (and the curated upstream repo-doc index) by "
        "keyword/topic when you do NOT know the exact guide basename. Read-only; auto-runs; "
        "deterministic (weighted lexical overlap — NO model call). Returns the most relevant "
        "guides + a snippet, each with a ready load_with hint (read_knowledge('<topic>') or "
        "read_repo_doc('<path>')). Reach for this at a TROUBLESHOOTING moment — a failure, an "
        "unfamiliar error, or a 'how do I…' where no specific tool already points you at the guide "
        "— then read_knowledge / read_repo_doc to load it in full. WHEN to use it is "
        "knowledge/conversation_style.md."
    ),
    "suggest_next_steps": (
        "Offer the user concrete next steps as CLICKABLE BUTTONS — YOU choose how many fit the "
        "moment (a few is typical; up to 6) — instead of asking "
        "'want me to…?' in prose. Whenever you would end a turn by proposing what to do next "
        "(save a baseline, compare runs, sweep, tear down, run again, …), CALL this tool with "
        "each option as {label, prompt}: a short button label plus the first-person message sent "
        "when the user clicks it. This is your FINAL action of the turn — call it with NO lead-in "
        "and NO line about the buttons afterward (they speak for themselves); the call ends the "
        "turn. DISCRETIONARY follow-ups only; it is NOT an approval gate — a mutating action still "
        "needs a command tool (run_shell / execute_llmdbenchmark) / propose_session_plan. See "
        "read_knowledge('conversation_style') for the offer cadence."
    ),
    "read_repo_doc": (
        "Read a documentation or spec file from inside the (read-only) repos, e.g. the quickstart "
        "guide. Use to confirm the authoritative flow/flags before acting. Also use this to "
        "SURFACE the benchmark repo's exploratory analysis tooling to the user — "
        "read_repo_doc('llm-d-benchmark/docs/analysis/README.md') (venv + interactive "
        "analysis.ipynb) or read_repo_doc('llm-d-benchmark/docs/analysis.md') (the pipeline "
        "overview). Those notebooks/scripts are user-driven exploration you POINT AT, not part of "
        "the automated run flow — see knowledge/analysis.md (and aggregate_runs for the one script "
        "the agent itself may run)."
    ),
    "fetch_key_docs": (
        "Fetch the LIVE content of the authoritative docs pinned in knowledge/key_docs.yaml "
        "(filter by task, e.g. 'quickstart'). The list of docs is fixed; content is read "
        "fresh from the clone. Read-only. Call this to ground yourself in the real procedure "
        "BEFORE proposing a deployment SessionPlan."
    ),
    "run_shell": (
        "Run an ARBITRARY shell command verbatim via `bash -lc` (pipes, redirects, globs, env "
        "expansion all work) — your general-purpose tool for any CLI step with no dedicated tool: "
        "notably creating/deleting the kind cluster (`kind create cluster --name llmd-quickstart`), "
        "installing prerequisites via `install_prereqs.sh --all`, or the upstream llm-d client "
        "toolchain (helm/helmfile/kustomize/yq/kubectl) via `install-deps.sh` before a guide-based "
        "deploy (see knowledge/preconditions.md + deploy_path_playbook.md for which installer "
        "when). Read-only commands (ls/cat/grep/kubectl get/…) auto-run; anything that writes "
        "requires the user's Approve before it executes — so don't also ask in prose, just call "
        "this and let the card collect the decision. Prefer the dedicated tools "
        "(execute_llmdbenchmark, ensure_repos, run_setup) when one fits."
    ),
    "propose_session_plan": (
        "Propose a structured SessionPlan (use case, spec, namespace, harness, workload, "
        "flags, steps) for the user to APPROVE. Enum fields are checked against the live "
        "catalog. Required and approved before any mutating step "
        "(ensure_repos/run_setup/standup/run/teardown). Call "
        "read_knowledge('welllit_path_advisor') BEFORE choosing a well-lit path / spec / "
        "profile so the plan follows the recommended path."
    ),
    "check_capacity": (
        "Capacity PRE-FLIGHT: will this deployment fit AND can your token pull the weights? Runs "
        "the benchmark repo's OWN capacity planner over the spec's rendered config (weights + "
        "activation + KV cache vs GPU memory, valid tensor-parallelism, max-context) for a "
        "feasible/infeasible verdict, AND the repo's gated-model access check — returning "
        "`gated`/`authorized`/`gated_reason`: PUBLIC, GATED+AUTHORIZED (proceed), or "
        "GATED+UNAUTHORIZED (knowledge says how to fix it / provision the secret). Read-only; "
        "auto-runs; the HF token never appears in the result. Pass `overrides` to reflect what the "
        "user actually asked for (bigger model, longer context, a real GPU). Call this right after "
        "propose_session_plan and BEFORE standing anything up. Call read_knowledge('capacity') to "
        "interpret BOTH verdicts. (Needs the benchmark venv: run_setup installs it.)"
    ),
    "aggregate_runs": (
        "OPTIONAL cross-run aggregation: when the user has run the SAME benchmark MULTIPLE times "
        "and wants run-to-run variance (mean/std/min/max across repeats), run the benchmark repo's "
        "OWN docs/analysis/aggregate_runs.py against an EXISTING results dir. Read-only; auto-runs. "
        "Pass results_prefix, harness, stack, and run_ids (>=2); it reads the BR v0.2 reports and "
        "writes aggregated_summary.{txt,json} ONLY into a session-workspace subdir. EXPLORATORY "
        "(it does NOT run a benchmark and does NOT replace analyze_results' SLO/goodput/Pareto "
        "verdicts) — it is the ONE exploratory analysis script the agent runs itself; the Jupyter "
        "notebook + plot templates are POINTER-ONLY (surface via read_repo_doc, never run). WHEN "
        "to aggregate is your judgment — read_knowledge('analysis')."
    ),
    "provision_hf_secret": (
        "APPROVAL-GATED MUTATING step: create/update the cluster's HuggingFace token Secret "
        "(default name 'llm-d-hf-token') in a namespace so a GATED-model standup can pull the "
        "gated weights — the natural follow-on to check_capacity's gated-access pre-flight. The HF "
        "token stays BACKEND-ONLY: read from the backend HF_TOKEN env by the vetted script, NEVER "
        "an input here, never in argv/events/logs. Call this ONLY after a check_capacity "
        "GATED+UNAUTHORIZED verdict whose reason says NO token is configured cluster-side — NEVER "
        "for a public model, and NEVER when a token merely LACKS access (that needs a HuggingFace "
        "access request, not a secret). After it succeeds, re-run check_capacity to confirm "
        "authorization, THEN stand up. WHEN to use it is knowledge/capacity.md."
    ),
    "check_endpoint_readiness": (
        "Endpoint READINESS gate: is the inference endpoint in a namespace actually SERVING — not "
        "just 'a pod exists'? Read-only; auto-runs. Checks the authoritative Kubernetes signal "
        "(`kubectl get endpoints`) and corroborates with the CLI's read-only "
        "`run --list-endpoints`. Returns a structured `ready` verdict with per-service "
        "ready/not-ready counts; when NOT ready it includes a `standup_suggestion` you can OFFER "
        "(standing up is mutating — never do it unprompted). When a Service is Running-but-NotReady "
        "it classifies WHY via `serving_readiness` (still-loading-weights vs wedged/broken); "
        "read_knowledge('readiness_probes') for that judgment. In GATEWAY-mode deploys it ALSO "
        "reads the Gateway-API control plane and surfaces the PROGRAMMED + "
        "Accepted/ResolvedRefs/Reconciled FACTS on `gateway` (pods can be Ready while the Gateway "
        "is PROGRAMMED:False); when the control plane isn't wired the result carries "
        "`gateway_readiness_guidance`, read_knowledge('gateway_readiness') for the judgment (set "
        "check_gateway=False on non-gateway/Kind deploys). Call this BEFORE running a benchmark "
        "against an existing stack; orchestrate_benchmark_run also gates on it automatically. WHEN "
        "to stand up is your judgment — read_knowledge('orchestrator')."
    ),
    "discover_stack": (
        "OPTIONAL richer ENVIRONMENT capture: trace the LIVE llm-d stack behind an "
        "OpenAI-compatible endpoint URL and capture it as BR-v0.2 scenario.stack components. "
        "Read-only; auto-runs. Runs the standalone stack-discovery tool (`llm-d-discover <url> -f "
        "benchmark-report`) with its OWN read-only RBAC + env redaction, writes a "
        "`{scenario: {stack: [...]}}` capture into the workspace, and returns structured stack "
        "FACTS (component/model/role counts, per-engine replicas + parallelism). COMPLEMENTS — does "
        "NOT replace — probe_environment / check_endpoint_readiness (the unconditional default); "
        "use this only when you want a precise capture of what is actually deployed (e.g. a "
        "remote/pre-existing stack you did not stand up). Needs llm_d_stack_discovery in the "
        "benchmark venv; if absent it returns ran=False with how to install it. WHEN to use it "
        "(vs endpoint probing) is your judgment — read_knowledge('stack_discovery')."
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
        "Write/validate a generated config artifact in the session workspace (never the "
        "read-only repos). artifact_type='workload'/'run_config' write a stock-shaped YAML as-is "
        "(MVP, rarely needed). artifact_type='scenario' AUTHORS finer per-knob vLLM/scheduling/"
        "storage edits beyond the parallelism/memory knobs check_capacity + "
        "generate_doe_experiment already cover: pass `content` as the per-knob OVERRIDES (a "
        "REQUIRED 'name' plus >=1 DOTTED upstream field path, e.g. vllmCommon.flags.*, affinity.*, "
        "decode.*/prefill.*). To author a KUSTOMIZE-method deploy set the kustomize.* family "
        "instead (enabled/guideName/repoPath/overlayPath/patches/…). The tool deep-merges onto a "
        "minimal scenario skeleton and SHAPE-validates against the repo's own scenario examples "
        "(read live). Read-only (only writes the workspace), auto-runs. WHICH knobs to set is YOUR "
        "judgment — read_knowledge('vllm_overrides') for vLLM tuning, or "
        "read_knowledge('deploy_path_playbook') for the kustomize guide/overlay/patches/repo "
        "choice. Then preview the returned `path`: "
        "execute_llmdbenchmark(subcommand='plan'/'run', flags.dry_run=True)."
    ),
    "convert_guide_to_scenario": (
        "Convert an arbitrary llm-d deployment guide into a benchmark scenario, authored "
        "WORKSPACE-ONLY (the agent's variant of upstream's skills/convert-guide, which writes into "
        "the read-only benchmark repo — this NEVER does that). FIRST read the guide yourself "
        "(read_repo_doc / run_shell 'git clone …' / your own file reads) and resolve its "
        "Helm/kustomize config to the LLMDBENCH_* env map using read_knowledge('convert_guide') — "
        "WHICH vars map to what, the standard practices, and the default harness/profile are "
        "KNOWLEDGE, not this tool. Then pass the resolved `env` map (+ optional `sources`, "
        "`harness`/`profile`, `source_ref`, and a `scenario` dotted-knob override for the "
        "validatable twin). It writes the scenario .sh plus a structurally-validated YAML+spec "
        "twin into the workspace — NEVER the read-only repo. A bare .sh is NOT gate-able, so GATE "
        "the YAML+spec twin via execute_llmdbenchmark(subcommand='plan', spec=<spec_path>, "
        "flags={'dry_run': True}) BEFORE any standup. To deploy, stand up off the workspace YAML "
        "via --spec=<spec_path>."
    ),
    "generate_doe_experiment": (
        "AUTHOR a Design-of-Experiments (DoE) experiment YAML: you supply the FACTORS to sweep — "
        "`run_factors` (workload knobs swept against one stack; REQUIRED) and optional "
        "`setup_factors` (infra knobs, each its own standup/teardown) — each factor {name, dotted "
        "`key`, list of `levels`}. The tool CROSS-PRODUCTS the levels into the full deduped named "
        "treatments matrix, writes a valid experiment YAML into the workspace, and validates it "
        "against the repo's own experiment examples. Read-only (only writes the workspace), "
        "auto-runs. Then pass the returned `path` as flags.experiments to execute_llmdbenchmark "
        "(subcommand='experiment' for a full DoE, or 'run' for a run-parameter sweep) — preview "
        "with flags.dry_run first. WHICH factors/levels to sweep is YOUR judgment: "
        "read_knowledge('sweep_playbook') to pick them. Read the repo's experiment examples "
        "(read_repo_doc) to pick real override keys."
    ),
    "execute_llmdbenchmark": (
        "Run the llmdbenchmark CLI: subcommand is one of plan/standup/smoketest/run/"
        "teardown/results/experiment. plan and --dry-run/--list-endpoints are read-only and "
        "auto-run; standup/run/teardown/experiment are mutating and require approval. "
        "'experiment' runs a full DoE sweep (standup+run+teardown per treatment) over an "
        "experiment YAML you pass via flags.experiments; its per-treatment reports — and a "
        "`run`'s results — land in the session workspace. "
        "To serve a NON-DEFAULT model (not the spec's scenario default) set the top-level "
        "`models` field (a HF id/short name; emits `-m`) — FIRST run "
        "check_capacity(overrides={'model': <same id>}) so the pre-flight validates that EXACT "
        "model (sizing + gated access); WHICH model is your judgment, see "
        "knowledge/model_override.md. "
        "All other behavior is configured through the `flags` object — consult THAT field's own "
        "documentation for each option's exact CLI mapping, valid subcommands, and knowledge "
        "pointer; do not act from memory. In brief: collect-only re-analysis "
        "(knowledge/collect_only.md); dataset replay vs a synthetic profile (dataset; "
        "knowledge/dataset_replay.md); MULTI-STACK targeting via `--stack` (knowledge/"
        "multi_stack.md); the GATEWAY PROVIDER via `--gateway-class` (gateway_class; "
        "knowledge/gateway_class.md); per-phase CLI timeouts, each a DEEPER bound that MUST stay "
        "below the runner deadline (knowledge/phase_timeouts.md); a CLOUD results sink via "
        "output=`gs://…`/`s3://…` (knowledge/cloud_results_sink.md); the run-config round-trip "
        "generate_config / run_config (knowledge/runconfig_roundtrip.md); plots (analyze; "
        "knowledge/analysis.md); a debug harness pod (debug; knowledge/harness_debug.md); "
        "single-step re-run (step; knowledge/step_select.md); and --monitoring "
        "(knowledge/observability.md). "
        "For the OPTIONAL TEAM-SHARED results store (publishes/pulls runs via GCS remotes) use "
        "subcommand='results' with the top-level `store` field — SEPARATE from your OWN local "
        "history (the result_history tool); see knowledge/history.md."
    ),
    "locate_and_parse_report": (
        "Find the newest Benchmark Report from a completed run, validate it against the "
        "repo schema, and return a plain-language metric summary. Read-only. Use after a run. "
        "Call read_knowledge('results_interpretation') BEFORE interpreting or summarizing the "
        "report for the user."
    ),
    "compare_reports": (
        "Compare 2+ Benchmark Reports side by side (an A/B of separate runs, or every "
        "report from a DoE 'experiment' sweep) and return per-metric deltas vs a baseline "
        "plus the winning run for each metric (latency: lower is better; throughput: higher). "
        "Read-only. Pass `sources` (run dirs/files, with optional `labels`) OR `experiment_dir` "
        "(scans for all reports under it). Use after a sweep or to compare two configurations."
    ),
    "compare_harness_runs": (
        "Cross-harness comparison for a MULTI-HARNESS session: contrast Benchmark Reports produced "
        "by DIFFERENT harnesses (e.g. an inference-perf SLO run and a guidellm throughput sweep) "
        "against the SAME stack. Read-only. Pass `sources` (2+ run dirs/files); the harness that "
        "produced each is read from the report itself. Returns each harness's runs + metric "
        "families, which metrics ≥2 harnesses both measured (cross-validate) vs only one, and the "
        "per-harness values side by side WITHOUT a winner (different load generators aren't "
        "directly comparable). Use AFTER running both harnesses. compare_reports contrasts configs "
        "of the SAME harness; this contrasts the harnesses. read_knowledge('multi_harness') to "
        "interpret it."
    ),
    "analyze_results": (
        "Results Analyzer: SLO-aware filtering, goodput estimation, and Pareto/DoE analysis over "
        "one or more validated Benchmark Reports. Read-only. Pass the SLO targets from the "
        "approved SessionPlan (`slo`) plus either `sources` (1+ run dirs/files) or "
        "`experiment_dir` (a whole sweep). Returns, per run, whether it MEETS the SLOs and an "
        "honest goodput ESTIMATE (fraction of requests meeting the SLOs, from aggregate "
        "percentiles, flagged as such); for a sweep it also returns the Pareto-optimal configs and "
        "the SLO-feasible frontier. Use after a run/sweep when the user has QoS targets or wants "
        "the best config. compare_reports gives raw deltas; this adds SLO/goodput/Pareto. Also "
        "returns `next_steps`: a RANKED list of follow-ups — offer the most useful of them as "
        "CLICKABLE BUTTONS via suggest_next_steps (now the ONLY path to post-analysis buttons; you "
        "choose how many — don't recite them in prose; see "
        "read_knowledge('conversation_style')). Call read_knowledge('analysis') to interpret it "
        "(and read_knowledge('sweep_playbook') when designing or reading a sweep/A-B)."
    ),
    "observe_run_metrics": (
        "Read LIVE cluster resource usage (CPU/memory) during a run via `kubectl top`. "
        "scope='pods' shows pod usage in a namespace (optionally narrowed to one orchestrated run "
        "by run_id, or per-container); scope='nodes' shows node usage. Read-only (auto-runs). Use "
        "it WHILE a benchmark is running to see if the model server / harness is near its CPU or "
        "memory limit (a leading indicator of OOM/throttle). Requires the in-cluster "
        "metrics-server, which kind and the cicd/kind spec do NOT install; if missing the tool "
        "reports that and changes nothing. Distinct from /metrics (the agent's OWN Prometheus "
        "counters). Call read_knowledge('observability') to interpret the numbers."
    ),
    "result_history": (
        "Persist VALIDATED Benchmark Report summaries across sessions and read trends over time. "
        "All actions auto-run (nothing here touches the cluster or repos). action='store' saves a "
        "report (pass `source` = a report file or run dir, plus optional "
        "`label`/`tags`/`spec`/`harness`/`workload`); it validates first and is idempotent. 'list' "
        "shows stored results newest first (filter by `filter_tag`/`filter_model`); 'get' returns "
        "one record by `record_id`; 'trend' returns the time-series of ONE `metric` "
        "(ttft/tpot/itl/request_latency/output_token_rate/total_token_rate/request_rate/"
        "success_rate_pct) across runs; 'delete' forgets a record. Store a result the user wants "
        "to keep AFTER you've analyzed it; use 'trend' to answer 'has performance regressed?'. "
        "Returns facts (values + direction); call read_knowledge('history') to interpret trends "
        "and give the verdict."
    ),
    "autotune_search": (
        "CLOSED-LOOP GOAL-SEEKING search-state tracker. Use it when the user states a GOAL ('hit "
        "p95 TTFT under 300ms at the best throughput, spend at most 6 runs') rather than asking to "
        "compare N fixed configs. It tracks the trial log, VALIDATES the next candidate YOU "
        "computed, and surfaces convergence FACTS — it does NOT pick the next config and does NOT "
        "decide whether to stop. The search STRATEGY and the STOP decision are YOURS, grounded in "
        "read_knowledge('autotune_strategy'). All actions auto-run (read/write only the "
        "workspace). Three actions: (1) 'record_trial' (config + report_source + the plan's "
        "slo/objective/direction) validates the report, evaluates it against the SLO via the "
        "agent's analyzer, and appends the trial — it REFUSES an unvalidated report. (2) "
        "'propose_next_config' (candidate + knobs + budget) PURELY VALIDATES the config you "
        "computed (in-bounds? duplicate? budget left?) — it never produces the value. (3) 'status' "
        "returns the incumbent best_feasible, the slo_feasible_frontier, budget_remaining, "
        "recent_improvement_pct, and slo_boundary_bracketed — FACTS ONLY, NO stop verdict. Ride "
        "ONE upfront SessionPlan approval (the plan's `autotune` block bounds the search); "
        "per-trial runs still go through execute_llmdbenchmark/orchestrate_benchmark_run + their "
        "normal approval gate. WHEN to goal-seek vs sweep, the strategy, and the convergence rubric "
        "are ALL in read_knowledge('autotune_strategy')."
    ),
    "export_run_bundle": (
        "Capture a one-click REPRODUCIBILITY PROVENANCE BUNDLE for a VALIDATED run: both read-only "
        "repo SHAs (+ dirty flags), the exact resolved run-config, an environment snapshot, the "
        "knowledge hash, the agent version, and the schema-validated Benchmark Report digest + "
        "summary. Read-only; auto-runs (git reads + a workspace write). Pass `source` (a report "
        "file or run dir) plus the namespace/spec/harness/workload/model/slo from the approved "
        "SessionPlan for an accurate regenerate command. It REFUSES an unvalidated report and "
        "NEVER fabricates a SHA — an empty/absent sibling repo is recorded `unavailable` and the "
        "bundle flagged non-reproducible-as-captured. Returns `bundle_id` + `regenerate_command` + "
        "a `dirty` flag. If no CLI run-config was generated this session, run "
        "execute_llmdbenchmark(subcommand='run', flags={'generate_config': True}) FIRST so the "
        "replay is byte-identical. Offer this AFTER you've analyzed a run the user wants to keep or "
        "share. Call read_knowledge('reproducibility') for WHEN to offer it and how to explain a "
        "dirty repo; this is only the mechanism."
    ),
    "reproduce_run": (
        "REPRODUCE a previously captured run from its provenance bundle. Reads the bundle and "
        "returns a STRUCTURED RERUN PROPOSAL (spec/harness/workload/namespace/slo + the captured "
        "run-config path + the dry-run-FIRST sequence + any dirty/unavailable-SHA caveat). It "
        "EMITS NO MUTATING COMMAND — auto-runs, proposes, mutates nothing. Then DRIVE the existing "
        "gates IN ORDER: propose_session_plan (catalog-validated, approval-gated) -> "
        "execute_llmdbenchmark(subcommand='run', flags={'run_config': <path>, 'dry_run': True}) "
        "to PREVIEW -> only on a clean dry-run, the approval-gated "
        "execute_llmdbenchmark(subcommand='run', flags={'run_config': <path>}) replay (run-only; "
        "needs a live stack serving the captured model). Warn the user if the current repo SHAs "
        "differ from the captured ones. Call read_knowledge('reproducibility') for the sequence + "
        "env-drift judgment (and read_knowledge('runconfig_roundtrip') for the -c run-only "
        "boundary)."
    ),
    "cancel_run": (
        "Cancel a still-running background run/turn in ANOTHER chat by its session id (from "
        "/api/sessions or a `ready` event). Use this to free a concurrency slot held by an "
        "abandoned or clearly-stuck run, or when the user changed their mind about a run they "
        "navigated away from. Frees the run's concurrency-cap slot AND cleans up its subprocess "
        "(no orphaned process / leaked Job). Auto-runs (it STOPS work; starts no mutation). "
        "Idempotent — a session with no live run reports cancelled=false. You cannot cancel the "
        "run you are calling from. WHEN to cancel is in knowledge/run_lifecycle.md."
    ),
    "orchestrate_benchmark_run": (
        "Run a benchmark as a Kubernetes Job the orchestrator manages end-to-end: submit "
        "(approval-gated `kubectl apply`), watch to completion, stream logs, and on failure "
        "classify the cause (OOM / timeout / eviction / unschedulable / image / run error). With "
        "max_attempts>1 a TRANSIENT fault (eviction) retries as a fresh Job; deterministic faults "
        "never retry. Unlike execute_llmdbenchmark (which runs the CLI locally as a blocking "
        "subprocess), use this for K8s-native, restart-resilient, individually-retryable runs. "
        "Needs the orchestrator container image (config ORCHESTRATOR_IMAGE or `image`). Pass an "
        "optional `scheduling` object to request a GPU type/count and to PLACE the Job so it does "
        "not starve the measured stack (affinity / tolerations / `avoid_labels`) — "
        "read_knowledge('resource_management') for HOW; omit it for the generic baseline. Call "
        "read_knowledge('orchestrator') to choose between this and execute_llmdbenchmark and to "
        "interpret a failure classification."
    ),
    "orchestrate_sweep": (
        "Run a multi-treatment DoE sweep as PARALLEL Kubernetes Jobs the orchestrator manages "
        "end-to-end. Pass `treatments` (each a named run with its own spec/harness/workload/"
        "command; usually the run treatments from generate_doe_experiment) and a `max_parallel` "
        "cap: treatments run concurrently up to the cap, each as its own retry/dead-letter Job, so "
        "one persistently-failing treatment dead-letters WITHOUT sinking the rest. Progress is "
        "checkpointed to a cluster ConfigMap (checkpoint=false to opt out), so an interrupted "
        "sweep RESUMES — pass back the returned `sweep_id` with the same treatments and completed "
        "ones are skipped. All treatments share ONE stood-up stack in `namespace` (gated once on "
        "endpoint readiness). Use this instead of execute_llmdbenchmark(subcommand='experiment') "
        "(the CLI's SEQUENTIAL DoE) when you want K8s-native parallel, restart-resilient runs. "
        "Needs the orchestrator image. Pass `scheduling` to place every Job "
        "(GPU/affinity/anti-starvation). Call read_knowledge('orchestrator') to choose between "
        "this and the CLI path, and read_knowledge('sweep_playbook') to design the treatments."
    ),
    "manage_orchestrated_runs": (
        "List, stop, or reap the orchestrator's Kubernetes benchmark Jobs ON THE CLUSTER — the "
        "manage surface for orchestrate_benchmark_run / orchestrate_sweep. action='list' "
        "classifies each agent-managed Job fresh from the cluster (phase + which "
        "run/sweep/treatment/session); read-only, auto-runs. action='stop' DELETES the "
        "still-running Jobs in scope (approval-gated `kubectl delete job`) — use this to ACTUALLY "
        "stop cluster work, because cancel_run only stops the agent's in-process WATCH task and a "
        "submitted Job keeps running. action='cleanup' reaps only TERMINAL Jobs (in-flight runs "
        "untouched). Deleting a Job never touches the results PVC, so artifacts survive. Scope "
        "with session_id and/or sweep_id; omit both to span the namespace. WHEN to stop/reap is in "
        "knowledge/run_lifecycle.md; choosing this vs cancel_run is in knowledge/orchestrator.md."
    ),
}


def build_registry() -> dict[str, ToolSpec]:
    """Build the name→ToolSpec map. ``run_shell`` is the agent's always-on ad-hoc command tool
    (an arbitrary ``bash -lc`` string, gated by the read-only/mutating classifier + approval,
    NOT the allowlist). The allowlist governs the DEDICATED command tools (execute_llmdbenchmark,
    probes, orchestrator) via ``ctx.run_command``/``ctx.run_readonly``, not this tool."""
    specs = [
        ToolSpec("probe_environment", _DESCRIPTIONS["probe_environment"], ProbeEnvironmentInput, probe.probe_environment),
        ToolSpec("list_catalog", _DESCRIPTIONS["list_catalog"], ListCatalogInput, probe.list_catalog),
        ToolSpec("inspect_workload_profile", _DESCRIPTIONS["inspect_workload_profile"], InspectWorkloadProfileInput, workload_profile.inspect_workload_profile),
        ToolSpec("estimate_run_duration", _DESCRIPTIONS["estimate_run_duration"], EstimateRunDurationInput, workload_profile.estimate_run_duration),
        ToolSpec("advise_accelerators", _DESCRIPTIONS["advise_accelerators"], AdviseAcceleratorsInput, probe.advise_accelerators),
        ToolSpec("read_knowledge", _DESCRIPTIONS["read_knowledge"], ReadKnowledgeInput, knowledge_access.read_knowledge),
        ToolSpec("search_knowledge", _DESCRIPTIONS["search_knowledge"], SearchKnowledgeInput, knowledge_access.search_knowledge),
        ToolSpec("read_repo_doc", _DESCRIPTIONS["read_repo_doc"], ReadRepoDocInput, knowledge_access.read_repo_doc),
        ToolSpec("fetch_key_docs", _DESCRIPTIONS["fetch_key_docs"], FetchKeyDocsInput, knowledge_access.fetch_key_docs),
        ToolSpec("propose_session_plan", _DESCRIPTIONS["propose_session_plan"], SessionPlan, plan.propose_session_plan),
        ToolSpec("check_capacity", _DESCRIPTIONS["check_capacity"], CheckCapacityInput, capacity.check_capacity),
        ToolSpec("aggregate_runs", _DESCRIPTIONS["aggregate_runs"], AggregateRunsInput, aggregate_runs_tool.aggregate_runs),
        ToolSpec("provision_hf_secret", _DESCRIPTIONS["provision_hf_secret"], ProvisionHfSecretInput, hf_secret.provision_hf_secret),
        ToolSpec("check_endpoint_readiness", _DESCRIPTIONS["check_endpoint_readiness"], CheckEndpointReadinessInput, check_endpoint_readiness),
        ToolSpec("discover_stack", _DESCRIPTIONS["discover_stack"], DiscoverStackInput, discover.discover_stack),
        ToolSpec("ensure_repos", _DESCRIPTIONS["ensure_repos"], EnsureReposInput, repos.ensure_repos),
        ToolSpec("run_setup", _DESCRIPTIONS["run_setup"], RunSetupInput, repos.run_setup),
        ToolSpec("write_and_validate_config", _DESCRIPTIONS["write_and_validate_config"], WriteConfigInput, config_artifact.write_and_validate_config),
        ToolSpec("convert_guide_to_scenario", _DESCRIPTIONS["convert_guide_to_scenario"], ConvertGuideInput, convert_guide.convert_guide_to_scenario),
        ToolSpec("generate_doe_experiment", _DESCRIPTIONS["generate_doe_experiment"], GenerateDoeInput, doe.generate_doe_experiment),
        ToolSpec("execute_llmdbenchmark", _DESCRIPTIONS["execute_llmdbenchmark"], ExecuteInput, execute.execute_llmdbenchmark),
        ToolSpec("run_shell", _DESCRIPTIONS["run_shell"], RunShellInput, shell.run_shell),
        ToolSpec("locate_and_parse_report", _DESCRIPTIONS["locate_and_parse_report"], LocateReportInput, report_locate.locate_and_parse_report),
        ToolSpec("compare_reports", _DESCRIPTIONS["compare_reports"], CompareReportsInput, compare.compare_reports),
        ToolSpec("compare_harness_runs", _DESCRIPTIONS["compare_harness_runs"], CompareHarnessRunsInput, multiharness.compare_harness_runs),
        ToolSpec("analyze_results", _DESCRIPTIONS["analyze_results"], AnalyzeResultsInput, analyze.analyze_results),
        ToolSpec("result_history", _DESCRIPTIONS["result_history"], ResultHistoryInput, history.result_history),
        ToolSpec("autotune_search", _DESCRIPTIONS["autotune_search"], AutotuneSearchInput, autotune.autotune_search),
        ToolSpec("export_run_bundle", _DESCRIPTIONS["export_run_bundle"], ExportRunBundleInput, reproducibility.export_run_bundle),
        ToolSpec("reproduce_run", _DESCRIPTIONS["reproduce_run"], ReproduceRunInput, reproducibility.reproduce_run),
        ToolSpec("orchestrate_benchmark_run", _DESCRIPTIONS["orchestrate_benchmark_run"], OrchestrateBenchmarkInput, orchestrate.orchestrate_benchmark_run),
        ToolSpec("orchestrate_sweep", _DESCRIPTIONS["orchestrate_sweep"], OrchestrateSweepInput, orchestrate.orchestrate_sweep),
        ToolSpec("observe_run_metrics", _DESCRIPTIONS["observe_run_metrics"], ObserveRunMetricsInput, observe.observe_run_metrics),
        ToolSpec("cancel_run", _DESCRIPTIONS["cancel_run"], CancelRunInput, cancel.cancel_run),
        ToolSpec("manage_orchestrated_runs", _DESCRIPTIONS["manage_orchestrated_runs"], ManageOrchestratedRunsInput, manage_runs.manage_orchestrated_runs),
        ToolSpec("suggest_next_steps", _DESCRIPTIONS["suggest_next_steps"], SuggestNextStepsInput, suggest.suggest_next_steps),
        ToolSpec("load_tools", _DESCRIPTIONS["load_tools"], LoadToolsInput, tool_loader.load_tools),
    ]
    return {s.name: s for s in specs}


# The single, stable tool surface (built once at import).
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


# Phase-grouped tools (load-on-demand). Each tool's JSON schema rides in the prompt-cached prefix
# on EVERY step, so showing all 37 up front is the bulk of the per-step tool cost. Instead, only
# the STARTER_KIT (below) is shown by default; the groups here are HIDDEN until the model calls
# ``load_tools(['<group>'])`` — which the loop folds into ``session.loaded_groups`` and then
# re-opens the provider turn with the expanded set (callable the SAME turn). The unlock is
# MODEL-DRIVEN, not a fixed phase gate, precisely because a user can enter directly at the
# sweep/analyze/reproduce phase with no in-session deploy — only the model reliably knows which
# group a request needs. Keep this in sync with ``prompt.py::GROUP_CATALOG_NOTE`` (a test enforces
# it). ``load_tools`` itself is in the STARTER_KIT (never grouped — it is how the rest are reached).
_TOOL_GROUPS: dict[str, frozenset[str]] = {
    # deploy & pre-flight
    "setup": frozenset({
        "check_capacity", "advise_accelerators", "ensure_repos", "run_setup",
        "write_and_validate_config", "provision_hf_secret", "check_endpoint_readiness",
        "discover_stack",
    }),
    # execute & monitor a benchmark
    "run": frozenset({
        "execute_llmdbenchmark", "orchestrate_benchmark_run", "observe_run_metrics",
        "cancel_run", "manage_orchestrated_runs",
    }),
    # results analysis
    "analyze": frozenset({
        "locate_and_parse_report", "analyze_results", "compare_reports", "result_history",
    }),
    # power features (the former _ADVANCED_TOOLS set)
    "advanced": frozenset({
        "orchestrate_sweep", "autotune_search", "generate_doe_experiment",
        "export_run_bundle", "reproduce_run", "aggregate_runs", "compare_harness_runs",
        "convert_guide_to_scenario",
    }),
}

# Every tool that belongs to some load-on-demand group (the inverse of the starter kit).
_GROUPED_TOOLS: frozenset[str] = frozenset().union(*_TOOL_GROUPS.values())

# Back-compat alias — the former "advanced tier" is now just one group.
_ADVANCED_TOOLS = _TOOL_GROUPS["advanced"]

# Always-resident tools: everything NOT in a group. These start a session, ground choices, gate
# mutations (propose_session_plan), preview workloads, run ad-hoc commands, and reach the groups
# (load_tools) — so the model is never stuck without an entry point.
STARTER_KIT: frozenset[str] = frozenset(REGISTRY) - _GROUPED_TOOLS


def _group_of(name: str) -> str | None:
    """The load-on-demand group a tool belongs to, or None if it is a starter-kit tool."""
    for group, members in _TOOL_GROUPS.items():
        if name in members:
            return group
    return None


def tool_definitions(loaded: frozenset[str] | None = None) -> list[dict[str, Any]]:
    """Export {name, description, input_schema} for the LLM providers.

    ``loaded`` is the set of currently-loaded group names. ``None`` (the default) returns the FULL
    registered set — every no-arg caller (the schema/registry tests, ad-hoc lookups) sees all tools.
    The agent loop passes ``loaded=frozenset(session.loaded_groups)`` so a grouped tool's heavy
    schema stays hidden until the model has called ``load_tools`` for its group; starter-kit tools
    are always included."""
    out = []
    for spec in REGISTRY.values():
        if loaded is not None:
            group = _group_of(spec.name)
            if group is not None and group not in loaded:
                continue
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
        # ``details`` is fed straight back to the model (so it can self-correct) AND serialized by
        # the loop (``clamp_tool_result_content`` -> ``json.dumps``) before it is appended to the
        # transcript. A custom field/model validator that ``raise``s ``ValueError``/``AssertionError``
        # (e.g. AutotuneKnob's ``max > min`` check) makes Pydantic embed the raised EXCEPTION OBJECT
        # in each entry's ``ctx`` — which is NOT JSON-serializable. Left in, that ``json.dumps`` would
        # raise ``TypeError`` OUTSIDE the loop's per-tool guard, crashing the turn AND leaving an
        # orphaned tool_call with no matching tool_result (poisoning the next turn). Drop ``ctx``
        # (``include_context=False``) — the human-readable ``msg`` already carries the validator's
        # message — then JSON-roundtrip as a belt-and-braces guarantee the result is serializable.
        details: list[Any] = exc.errors(include_url=False, include_context=False)
        try:
            details = json.loads(json.dumps(details, default=str))
        except (TypeError, ValueError):  # pragma: no cover — defensive last resort
            details = [{"msg": e.get("msg", "invalid value")} for e in details]
        return {"error": "invalid arguments", "details": details}

    kwargs = model.model_dump()
    result = spec.handler(ctx, **kwargs)
    if inspect.isawaitable(result):
        result = await result
    return result
