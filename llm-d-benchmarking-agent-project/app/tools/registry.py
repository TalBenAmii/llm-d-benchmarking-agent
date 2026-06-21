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
    command,
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
    resilience,
    shell,
    suggest,
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
    RunCommandInput,
    RunResilienceDrillInput,
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
        "PREVIEW what a workload profile actually SENDS, before running it, so a non-expert "
        "can see what they're about to benchmark. Read-only; auto-runs. Pass the profile name "
        "as the agent uses it (e.g. 'chatbot_synthetic.yaml', 'guide_pd-disaggregation_1.yaml') "
        "and an optional harness (omitted = search every harness dir, inference-perf first). It "
        "locates the profile under the read-only benchmark repo's workload/profiles/<harness>/, "
        "parses the YAML, and returns a NORMALIZED, AUDITABLE factual summary across the differing "
        "harness layouts: token shape (input/output length distribution; shared/system-prefix "
        "reuse), load shape (request rate/concurrency/QPS, sweep stages, per-stage + total "
        "duration), and the prompt/dataset source (synthetic vs a staged dataset, and whether one "
        "is required) — each field tagged with the raw key it came from (`_from`). On a name that "
        "doesn't exist it returns an error listing the profiles that DO exist for that harness "
        "(via list_catalog's enumeration). FACTS ONLY — WHICH workload to pick is your judgment; "
        "read_knowledge('welllit_path_advisor')/sweep_playbook, not this tool."
    ),
    "estimate_run_duration": (
        "Rough PRE-RUN wall-clock estimate for a workload profile. Read-only; auto-runs. Reads "
        "the same profile as inspect_workload_profile and computes a clearly-labeled HEURISTIC "
        "estimate from the load shape (sum of inference-perf sweep-stage durations; or guidellm "
        "max_seconds × number of rate stages; or request-count / mean rate), ALWAYS returning the "
        "`basis`, the stated `assumption`, and `approximate=True` (it excludes standup/warmup/"
        "teardown). If the profile has no duration/rate/request-count fields it returns "
        "`estimable=False` and SAYS what's missing rather than inventing a number. The arithmetic "
        "is all that lives here — whether the duration is acceptable is your judgment, not this tool."
    ),
    "advise_accelerators": (
        "Accelerator / CPU-inferencing PRE-FLIGHT: \"can my hardware actually run this?\" "
        "Read-only; auto-runs. Reads each node's ADVERTISED resources via the already-"
        "allowlisted `kubectl get nodes -o json` and reports which extended-resource key a node "
        "advertises — `nvidia.com/gpu` or the amd.com/gpu / habana.ai/gaudi / google.com/tpu / "
        "Intel XPU (gpu.intel.com/i915|xe) siblings — vs CPU-only, plus each node's "
        "capacity/allocatable cpu + memory. It returns FACTS ONLY (any_accelerator, cpu_only, "
        "advertised_resources, per-node accelerators/cpu/memory) — no verdict. It COMPLEMENTS "
        "check_capacity, which sizes model weights + KV cache against GPU MEMORY: this answers "
        "whether a node even ADVERTISES the accelerator (or, if CPU-only, meets the floor). Call "
        "read_knowledge('accelerators') to turn the facts into a verdict: the CUDA 12.9.1 / "
        "driver minimums (>= 525.60.13, < 580; 575.x rec.) and the planned CUDA 13.0.2 / 580.65.06 "
        "minimum, the Device-Plugin vs DRA choice, and the real (NON-sim) CPU-only "
        "64-core/64GB-per-replica floor — with the Kind/CPU-sim path (cicd/kind) supported and "
        "EXEMPT from that floor. Use this at the plan gate alongside check_capacity, BEFORE a "
        "standup. The judgment is in knowledge/accelerators.yaml; this tool is only the mechanism."
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
        "deterministic (weighted lexical keyword overlap — NO model call). Returns the most "
        "relevant guides + a snippet from each, each with a ready load_with hint "
        "(read_knowledge('<topic>') or read_repo_doc('<path>')). Reach for this at a "
        "TROUBLESHOOTING / problem moment — a user hits a failure, an unfamiliar error, or asks "
        "'how do I…' and no specific tool already points you at the guide. It COMPLEMENTS "
        "read_knowledge (load a guide you can name) and the system prompt's knowledge index "
        "(the enumerated topic list): use search_knowledge to FIND the right doc, then "
        "read_knowledge / read_repo_doc to load it in full. WHEN to use it is "
        "knowledge/conversation_style.md."
    ),
    "suggest_next_steps": (
        "Offer the user 2-4 concrete next steps as CLICKABLE BUTTONS instead of asking "
        "'want me to…?' in prose. Whenever you would end a turn by proposing what to do next "
        "(save a baseline, compare runs, sweep, tear down, run again, dig into the latency "
        "tail, …), do NOT phrase those options as a prose question — CALL this tool with each "
        "option as {label, prompt}: a short button label plus the first-person message sent "
        "when the user clicks it. The UI renders them as the same floating suggestion pills as "
        "the welcome chips; clicking one submits its prompt as the user's next message. This is "
        "your FINAL action of the turn — call it with NO lead-in introducing the buttons and NO "
        "line about them afterward (they speak for themselves); the call ends the turn. Use it "
        "for DISCRETIONARY follow-ups only; it is NOT an approval "
        "gate — a mutating action still needs run_command / propose_session_plan (those raise "
        "the Approve card). See read_knowledge('conversation_style') for the offer cadence."
    ),
    "read_repo_doc": (
        "Read a documentation or spec file from inside the (read-only) repos, e.g. the "
        "quickstart guide. Use to confirm the authoritative flow/flags before acting. Also "
        "use this to SURFACE the benchmark repo's exploratory analysis tooling to the user — "
        "read_repo_doc('llm-d-benchmark/docs/analysis/README.md') (how to set up the venv + "
        "launch the interactive analysis.ipynb notebook against their results) or "
        "read_repo_doc('llm-d-benchmark/docs/analysis.md') (the full analysis-pipeline overview). "
        "Those notebook/scripts are user-driven power-user exploration you POINT AT, not part of "
        "the automated run flow — see knowledge/analysis.md (and the aggregate_runs tool for the "
        "one script the agent itself may run)."
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
        "via install_prereqs.sh, OR the UPSTREAM llm-d guide client toolchain "
        "(helm/helmfile/kustomize/yq/kubectl) via ['install-deps.sh'] before a guide-based "
        "deploy (see knowledge/preconditions.md + deploy_path_playbook.md for which installer "
        "to offer when). Prefer the dedicated tools (execute_llmdbenchmark, "
        "ensure_repos, run_setup) when one fits."
    ),
    "run_shell": (
        "Run an ARBITRARY shell command verbatim via `bash -lc` (pipes, redirects, globs, and "
        "env expansion all work) — this BYPASSES the allowlist, so use it only when no dedicated "
        "tool and no allowlisted run_command argv fits. Read-only commands (ls/cat/grep/kubectl "
        "get/git log/…) auto-run; anything that writes or isn't recognized as read-only requires "
        "the user's Approve before it executes — so do not also ask in prose, just call this and "
        "let the card collect the decision. Available only when the operator enabled "
        "UNRESTRICTED_TOOLS."
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
        "Capacity PRE-FLIGHT: will this deployment fit AND can your token pull the weights? "
        "Runs the benchmark repo's OWN capacity planner over the spec's rendered config "
        "(model weights + activation + KV cache vs GPU memory, valid tensor-parallelism, "
        "max-context limits) for a feasible/infeasible verdict, AND the repo's OWN gated-"
        "model access check — returning `gated`/`authorized`/`gated_reason`: PUBLIC (no "
        "token needed), GATED+AUTHORIZED (your token can pull it, proceed), or "
        "GATED+UNAUTHORIZED (your token can't — knowledge says how to fix it / provision the "
        "secret). Read-only; auto-runs; the HF token never appears in the result. Pass "
        "`overrides` to reflect what the user actually asked for (a bigger model, longer "
        "context, a real GPU). Call this right after propose_session_plan and BEFORE "
        "standing anything up — it catches OOM / won't-load / can't-serve AND can't-pull-"
        "weights cases before a long standup fails opaquely. Call read_knowledge('capacity') "
        "to interpret BOTH verdicts. (Needs the benchmark venv: run_setup installs it.)"
    ),
    "aggregate_runs": (
        "OPTIONAL cross-run aggregation: when the user has run the SAME benchmark MULTIPLE "
        "times and wants the run-to-run variance (mean/std/min/max across repeats), run the "
        "benchmark repo's OWN standalone docs/analysis/aggregate_runs.py against an EXISTING "
        "results dir. Read-only; auto-runs. Pass results_prefix (the existing results dir), "
        "harness, stack, and run_ids (>=2). It reads the BR v0.2 reports for those runs and "
        "writes aggregated_summary.{txt,json} ONLY into a session-workspace subdir (the "
        "read-only repos and the results dir are never written), returning the per-metric "
        "mean/std/min/max. This is EXPLORATORY (it does NOT run a benchmark and does NOT "
        "replace analyze_results' SLO/goodput/Pareto verdicts) — it is the ONE exploratory "
        "analysis script the agent runs itself; the interactive Jupyter notebook and the "
        "to_be_incorporated/ plot templates are POINTER-ONLY (surface them with read_repo_doc, "
        "never run them). WHEN to aggregate is your judgment — read_knowledge('analysis')."
    ),
    "provision_hf_secret": (
        "APPROVAL-GATED MUTATING step: create/update the cluster's HuggingFace token Secret "
        "(default name 'llm-d-hf-token') in a namespace so a GATED-model standup can pull the "
        "gated weights — the natural follow-on to check_capacity's gated-access pre-flight. "
        "The HF token stays BACKEND-ONLY: it is read from the backend HF_TOKEN env by the "
        "vetted script and is NEVER an input here, never in the argv, never in any command "
        "event or log. Requires approval (it writes a Secret to the cluster). Call this ONLY "
        "after a check_capacity GATED+UNAUTHORIZED verdict whose reason says NO token is "
        "configured cluster-side — NEVER for a public model, and NEVER when a token merely "
        "LACKS access (that needs a HuggingFace access request, not a secret). After it "
        "succeeds, re-run check_capacity to confirm authorization, THEN stand up. WHEN to use "
        "it is knowledge/capacity.md ('Gated-model access pre-flight'); this is only the "
        "mechanism."
    ),
    "check_endpoint_readiness": (
        "Endpoint READINESS gate: is the inference endpoint in a namespace actually SERVING — "
        "not just 'a pod exists'? Read-only; auto-runs. It checks the authoritative Kubernetes "
        "signal (`kubectl get endpoints` — does a Service have a READY backing endpoint, which "
        "a pod failing its readiness probe does NOT) and corroborates with the benchmark CLI's "
        "own read-only `run --list-endpoints`. Returns a structured `ready` verdict with the "
        "per-service ready/not-ready endpoint counts; when NOT ready it includes a "
        "`standup_suggestion` you can OFFER the user (standing up is mutating and needs "
        "approval — never do it unprompted). When a Service exists but is Running-but-NotReady, "
        "it ALSO classifies WHY via `serving_readiness`: it folds the pod readiness conditions / "
        "restartCount / age with two constrained GET probes — `/v1/models` (model-serving-ready) "
        "vs `/health` (process-alive) — so you can tell 'still loading model weights (legitimate "
        "— keep waiting)' from 'wedged/broken (stop)' BEFORE submitting a benchmark; "
        "read_knowledge('readiness_probes') for that judgment. In GATEWAY-mode deploys it ALSO "
        "reads the Gateway-API control plane (gateway/gatewayclass/inferencepool/httproute) and "
        "surfaces the PROGRAMMED + Accepted/ResolvedRefs/Reconciled condition FACTS on `gateway` "
        "(Phase 65) — telling 'the model pods are Ready' apart from 'traffic can actually reach "
        "them' (pods can be Ready while the Gateway is PROGRAMMED:False). When the control plane "
        "isn't wired the result carries `gateway_readiness_guidance`; "
        "read_knowledge('gateway_readiness') for the wait-vs-stand-up-vs-config-error judgment "
        "(set check_gateway=False on non-gateway/Kind deploys). Call this BEFORE running a "
        "benchmark against an existing stack; orchestrate_benchmark_run also gates on it "
        "automatically. This is the mechanism; WHEN to stand up is your judgment — "
        "read_knowledge('orchestrator')."
    ),
    "discover_stack": (
        "OPTIONAL richer ENVIRONMENT capture: trace the LIVE llm-d stack behind an "
        "OpenAI-compatible endpoint URL and capture it as BR-v0.2 scenario.stack components. "
        "Read-only; auto-runs. It runs the standalone stack-discovery tool "
        "(`llm-d-discover <url> -f benchmark-report`), which connects with its OWN read-only "
        "Kubernetes RBAC + env-var redaction and emits the deployed stack's components "
        "(model / role / replicas / parallelism / accelerator) — then writes a "
        "`{scenario: {stack: [...]}}` capture into the session workspace and returns structured "
        "stack FACTS (component/model/role counts, per-engine replicas + parallelism). This "
        "COMPLEMENTS — it does NOT replace — probe_environment / check_endpoint_readiness, which "
        "stay the unconditional default for sensing the environment; use this only when you want "
        "a precise capture of what is actually deployed (e.g. a remote/pre-existing stack you did "
        "not stand up). It needs llm_d_stack_discovery installed in the benchmark venv (a "
        "self-contained subpackage install.sh does NOT install — `pip install -e "
        "llm-d-benchmark/llm_d_stack_discovery`); if absent it returns ran=False with how to "
        "install it. WHEN to use it (vs endpoint probing) is your judgment — "
        "read_knowledge('stack_discovery')."
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
        "read-only repos). artifact_type='workload'/'run_config' write a stock-shaped YAML "
        "as-is (MVP, rarely needed). artifact_type='scenario' AUTHORS finer per-knob vLLM/"
        "scheduling/storage scenario edits beyond the parallelism/memory knobs check_capacity "
        "+ generate_doe_experiment already cover: pass `content` as the per-knob OVERRIDES — a "
        "REQUIRED 'name' plus >=1 DOTTED upstream field path (vllmCommon.flags.*, "
        "vllmCommon.kvTransfer.*, vllmCommon.kvEvents.*, vllmCommon.priorityClassName, "
        "vllmCommon.ephemeralStorage, vllmCommon.networkResource, affinity.*, schedulerName, "
        "routing.servicePort, decode.*/prefill.* schedulerName/priorityClassName). To author a "
        "KUSTOMIZE-method deploy (Phase 46) set the kustomize.* family instead — "
        "kustomize.enabled/guideName/repoPath/repoRef/acceleratorBackend/monitoring/overlayPath/"
        "extraHelmValues/extraHelmSets/guideVariableOverrides and kustomize.patches (a list of "
        "{patch: ...} strategic-merge patches). The tool deep-merges them onto a minimal "
        "`scenario: [ {name, ...} ]` skeleton and SHAPE-validates the knobs against the repo's "
        "own scenario examples (read live). Read-only (only writes the workspace), auto-runs. "
        "WHICH knobs to set is YOUR judgment — call read_knowledge('vllm_overrides') for vLLM "
        "tuning, or read_knowledge('deploy_path_playbook') for the kustomize guide/overlay/"
        "patches/repo choice. Then preview the returned `path` via the determinism gate: "
        "execute_llmdbenchmark(subcommand='plan'/'run', flags.dry_run=True)."
    ),
    "convert_guide_to_scenario": (
        "Convert an arbitrary llm-d deployment guide into a benchmark scenario, authored "
        "WORKSPACE-ONLY (the agent's variant of upstream's skills/convert-guide, which writes "
        "ai.<name>.sh + ai.<name>.yaml INTO the read-only benchmark repo — this NEVER does "
        "that). FIRST read the guide yourself (read_repo_doc / run_command git clone / your own "
        "file reads) and resolve its Helm/kustomize config to the LLMDBENCH_* env map using "
        "read_knowledge('convert_guide') — WHICH vars map to what, the standard practices "
        "(DECODE_MODEL_COMMAND=custom, REPLACE_ENV_* placeholders, preprocess) and the default "
        "inference-perf / sanity_random.yaml are KNOWLEDGE, not this tool. Then pass the "
        "resolved `env` map (+ optional per-var `sources` provenance, `harness`/`profile`, "
        "`source_ref`, and an optional `scenario` dotted-knob override for the validatable "
        "twin). It writes FOUR files into the session workspace — ai.<name>.sh (the "
        "upstream-shaped scenario of sorted, shell-quoted LLMDBENCH_* exports), ai.<name>.yaml "
        "(a structurally-validated scenario twin), and ai.<name>.spec.yaml (its companion "
        "--spec) — NEVER the read-only repo. A bare .sh is NOT consumable by the determinism "
        "gate, so the YAML+spec twin is the gate-able artifact: GATE it via "
        "execute_llmdbenchmark(subcommand='plan', spec=<spec_path>, flags={'dry_run': True}) "
        "BEFORE any standup. To then deploy the converted guide, stand up directly off the "
        "workspace YAML via --spec=<spec_path> (the standup -c/--scenario .sh route is not "
        "modeled here)."
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
        "experiment YAML you pass via flags.experiments; its per-treatment reports — and a "
        "`run`'s results — land in the session workspace. "
        "To serve a NON-DEFAULT model (not the spec's scenario default) set the top-level "
        "`models` field (a HF id/short name; emits `-m`) — FIRST run "
        "check_capacity(overrides={'model': <same id>}) so the pre-flight validates that EXACT "
        "model (sizing + gated access); WHICH model is your judgment, see "
        "knowledge/model_override.md. "
        "All other behavior is configured through the `flags` object — consult THAT field's own "
        "documentation for each option's exact CLI mapping, valid subcommands, "
        "read-only-vs-mutating, and knowledge pointer; do not act from memory. In brief, flags "
        "cover: collect-only re-analysis of a prior run (skip; knowledge/collect_only.md); "
        "dataset replay instead of a synthetic profile (dataset; knowledge/dataset_replay.md); "
        "MULTI-STACK targeting via `--stack` plus a parallel-deploy cap "
        "(knowledge/multi_stack.md); the GATEWAY PROVIDER via `--gateway-class` (gateway_class; "
        "knowledge/gateway_class.md); per-phase CLI timeouts, each a DEEPER bound that MUST stay "
        "below the runner deadline (knowledge/phase_timeouts.md); a CLOUD results sink via "
        "output=`gs://…`/`s3://…` (knowledge/cloud_results_sink.md); the CLI run-config "
        "round-trip generate_config / run_config (knowledge/runconfig_roundtrip.md); "
        "supplementary matplotlib plots (analyze; knowledge/analysis.md); a debug harness pod "
        "(debug; knowledge/harness_debug.md); single-step re-run (step; knowledge/step_select.md); "
        "and the --monitoring toggle (knowledge/observability.md). "
        "For the OPTIONAL git-like, TEAM-SHARED results store (publishes/pulls runs via GCS "
        "remotes) use subcommand='results' with the top-level `store` field — SEPARATE from your "
        "OWN local history (the result_history tool), which is unchanged; see knowledge/history.md."
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
        "Also returns `next_steps`: a RANKED list of recommended follow-ups over the validated "
        "facts + your saved history, prioritizing save-to-trend / compare-to-baseline over "
        "teardown/run-again — offer the best 2-4 of them as CLICKABLE BUTTONS by calling "
        "suggest_next_steps (don't recite the list in prose; see "
        "read_knowledge('conversation_style') for the offer cadence). "
        "Call read_knowledge('analysis') to interpret it (and read_knowledge('sweep_playbook') "
        "when designing or reading a sweep/A-B)."
    ),
    "observe_run_metrics": (
        "Read LIVE cluster resource usage (CPU/memory) during a run via `kubectl top`. "
        "scope='pods' shows pod usage in a namespace (optionally narrowed to one orchestrated "
        "run by run_id, or per-container); scope='nodes' shows node usage. Read-only "
        "(auto-runs). Use it WHILE a benchmark is running to see if the model server / harness "
        "is near its CPU or memory limit (a leading indicator of an OOM/throttle). Requires the "
        "in-cluster metrics-server, which kind and the cicd/kind spec do NOT install (add it to "
        "the cluster separately); if it is missing the tool reports that and changes nothing. "
        "Distinct from /metrics, which exposes the agent's "
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
    "autotune_search": (
        "CLOSED-LOOP GOAL-SEEKING search-state tracker. Use it when the user states a GOAL "
        "('hit p95 TTFT under 300ms at the best output throughput you can, spend at most 6 "
        "runs') rather than asking to compare N fixed configs. It tracks the trial log, "
        "VALIDATES the next candidate YOU computed, and surfaces convergence FACTS — it does "
        "NOT pick the next config and does NOT decide whether to stop. The search STRATEGY and "
        "the STOP decision are YOURS, grounded in read_knowledge('autotune_strategy'). All "
        "actions auto-run (read/write only the session workspace; nothing touches the cluster "
        "or the repos). Three actions: (1) action='record_trial' (config + report_source + the "
        "plan's slo/objective/direction) validates the Benchmark Report, evaluates it against "
        "the SLO via the SAME analyzer the rest of the agent uses, and appends the trial — it "
        "REFUSES an unvalidated report. (2) action='propose_next_config' (candidate + the "
        "plan's knobs + budget) PURELY VALIDATES the config you computed: in-bounds? duplicate "
        "of a prior trial? budget left? — it never produces the value. (3) action='status' "
        "(slo/objective/direction/budget) returns the incumbent best_feasible, the "
        "slo_feasible_frontier (REUSES pareto_analysis), budget_remaining, recent_improvement_pct, "
        "and slo_boundary_bracketed — FACTS ONLY, with NO converge/stop verdict. Ride ONE "
        "upfront SessionPlan approval (the plan's `autotune` block bounds the whole search); the "
        "per-trial runs still go through execute_llmdbenchmark/orchestrate_benchmark_run + their "
        "normal approval gate. WHEN to goal-seek vs sweep, which strategy, the start point/step, "
        "and the convergence rubric are ALL in read_knowledge('autotune_strategy')."
    ),
    "export_run_bundle": (
        "Capture a one-click REPRODUCIBILITY PROVENANCE BUNDLE for a VALIDATED run: both "
        "read-only repo SHAs (+ dirty flags), the exact resolved run-config the CLI wrote, an "
        "environment snapshot, the knowledge hash, the agent version, and the schema-validated "
        "Benchmark Report digest + summary. Read-only; auto-runs (git reads + a workspace write; "
        "it mutates no cluster/repo). Pass `source` (a report file or run dir) plus the "
        "namespace/spec/harness/workload/model/slo from the approved SessionPlan for an accurate "
        "regenerate command + a re-derivable rerun. It REFUSES an unvalidated report (a bundle "
        "only certifies a schema-valid run) and NEVER fabricates a SHA — an empty/absent sibling "
        "repo is recorded as `unavailable` and the bundle is flagged non-reproducible-as-captured. "
        "Returns `bundle_id` + `regenerate_command` + a `dirty` flag. If no CLI run-config was "
        "generated this session, run execute_llmdbenchmark(subcommand='run', "
        "flags={'generate_config': True}) FIRST so the replay is byte-identical. Offer this AFTER "
        "you've parsed/analyzed a run the user wants to keep or share. Call "
        "read_knowledge('reproducibility') for WHEN to offer it and how to explain a dirty repo "
        "to a non-expert; this is only the mechanism."
    ),
    "reproduce_run": (
        "REPRODUCE a previously captured run from its provenance bundle. Reads the bundle and "
        "returns a STRUCTURED RERUN PROPOSAL (spec/harness/workload/namespace/slo + the captured "
        "run-config path + the dry-run-FIRST sequence + any dirty/unavailable-SHA caveat). It "
        "EMITS NO MUTATING COMMAND — auto-runs, proposes, mutates nothing. Then DRIVE the existing "
        "gates IN ORDER: propose_session_plan (catalog-validated, approval-gated — gate 1) -> "
        "execute_llmdbenchmark(subcommand='run', flags={'run_config': <path>, 'dry_run': True}) "
        "to PREVIEW (the CLI --dry-run gate) -> only on a clean dry-run, the approval-gated "
        "execute_llmdbenchmark(subcommand='run', flags={'run_config': <path>}) -c replay (run-only; "
        "needs a live stack serving the captured model). Reproduce reuses these gates — never a "
        "direct subprocess. Warn the user if the current repo SHAs differ from the captured ones. "
        "Call read_knowledge('reproducibility') for the sequence + env-drift judgment "
        "(and read_knowledge('runconfig_roundtrip') for the -c run-only boundary)."
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
    "orchestrate_sweep": (
        "Run a multi-treatment DoE sweep as PARALLEL Kubernetes Jobs the orchestrator manages "
        "end-to-end — the proposal's parallel-treatment scheduling. Pass `treatments` (each a "
        "named run with its own spec/harness/workload/command; usually the run treatments from "
        "generate_doe_experiment) and a `max_parallel` concurrency cap: treatments run "
        "concurrently up to the cap, each as its own retry/dead-letter Job, so one persistently-"
        "failing treatment dead-letters WITHOUT sinking the rest. Progress is checkpointed to a "
        "cluster ConfigMap (set checkpoint=false to opt out), so an interrupted sweep RESUMES — "
        "pass back the returned `sweep_id` with the same treatments and completed ones are "
        "skipped. All treatments share ONE stood-up stack in `namespace` (gated once on endpoint "
        "readiness). Use this instead of execute_llmdbenchmark(subcommand='experiment'), which "
        "runs the CLI's SEQUENTIAL DoE, when you want K8s-native parallel, restart-resilient, "
        "individually-retryable treatment runs. Needs the orchestrator image (ORCHESTRATOR_IMAGE "
        "or `image`). Pass `scheduling` to place every Job (GPU/affinity/anti-starvation). Call "
        "read_knowledge('orchestrator') to choose between this and the CLI path, and "
        "read_knowledge('sweep_playbook') to design the treatments."
    ),
    "manage_orchestrated_runs": (
        "List, stop, or reap the orchestrator's Kubernetes benchmark Jobs ON THE CLUSTER — the "
        "manage surface for orchestrate_benchmark_run / orchestrate_sweep. action='list' "
        "classifies each agent-managed Job fresh from the cluster (phase + which run/sweep/"
        "treatment/session it is); read-only, auto-runs. action='stop' DELETES the still-running "
        "Jobs in scope (approval-gated `kubectl delete job`) — use this to ACTUALLY stop cluster "
        "work, because cancel_run only stops the agent's in-process WATCH task and a submitted Job "
        "keeps running after that. action='cleanup' reaps only TERMINAL Jobs to tidy the namespace "
        "(in-flight runs untouched). Deleting a Job never touches the results PVC, so artifacts "
        "survive. Scope with session_id (one chat's runs) and/or sweep_id (one sweep's "
        "treatments); omit both to span the namespace. Judgment on WHEN to stop/reap is in "
        "knowledge/run_lifecycle.md; choosing this vs cancel_run is in knowledge/orchestrator.md."
    ),
    "run_resilience_drill": (
        "RESILIENCE / CHAOS DRILL: prove the orchestrator correctly classifies + recovers from "
        "injected faults AND survives its own restart mid-run. OPT-IN and DOUBLE-gated — it "
        "refuses unless the backend CHAOS_ENABLED flag is set, and it is NEVER reachable from "
        "orchestrate_benchmark_run. It runs hermetically against an in-process cluster (it does "
        "NOT touch or break a real cluster). You supply a `chaos_plan` (the faults to inject: "
        "evicted/oom/unschedulable/image_error/run_error/timeout/unknown, each at_attempt N) — "
        "those flow through the UNMODIFIED classify→retry/dead-letter→reconstruct path, so the "
        "returned resilience report is a genuine proof: per-fault injected vs classified vs "
        "recovery (evicted/unknown retry to a fresh Job; the rest dead-letter by design), a "
        "restart-durability proof (a fresh orchestrator resumes a partial sweep from the cluster "
        "checkpoint with 0 duplicate Jobs), and SLO met/missed. WHICH faults to inject for a "
        "scenario, and how to read the verdict, is YOUR judgment — call "
        "read_knowledge('resilience') first (it cross-links read_knowledge('orchestrator'))."
    ),
}


def build_registry(*, unrestricted: bool = False) -> dict[str, ToolSpec]:
    """Build the name→ToolSpec map. When ``unrestricted`` is True (UNRESTRICTED_TOOLS), the
    allowlist-bypassing ``run_shell`` tool is added; otherwise it is NOT registered at all, so
    the default tool surface is unchanged."""
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
        ToolSpec("run_command", _DESCRIPTIONS["run_command"], RunCommandInput, command.run_command),
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
        ToolSpec("run_resilience_drill", _DESCRIPTIONS["run_resilience_drill"], RunResilienceDrillInput, resilience.run_resilience_drill),
        ToolSpec("suggest_next_steps", _DESCRIPTIONS["suggest_next_steps"], SuggestNextStepsInput, suggest.suggest_next_steps),
    ]
    if unrestricted:
        # Opt-in only: the allowlist-bypassing shell tool is appended (and exposed to the LLM)
        # ONLY when the operator set UNRESTRICTED_TOOLS — see app/tools/shell.py.
        specs.append(ToolSpec("run_shell", _DESCRIPTIONS["run_shell"], RunShellInput, shell.run_shell))
    return {s.name: s for s in specs}


# The DEFAULT registry (UNRESTRICTED_TOOLS off) — used at import for the stable tool surface.
REGISTRY = build_registry()


def _registry_for(ctx: ToolContext | None) -> dict[str, ToolSpec]:
    """The tool surface for this context: the default REGISTRY, or one that additionally
    includes ``run_shell`` when the context's settings enable UNRESTRICTED_TOOLS. Falls back to
    the default registry when no context is supplied (e.g. callers with no settings)."""
    if ctx is not None and ctx.settings.unrestricted_tools:
        return build_registry(unrestricted=True)
    return REGISTRY


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


def tool_definitions(ctx: ToolContext | None = None) -> list[dict[str, Any]]:
    """Export {name, description, input_schema} for the LLM providers. Pass the per-session
    ``ctx`` so UNRESTRICTED_TOOLS adds ``run_shell`` to the exposed surface (default: off)."""
    out = []
    for spec in _registry_for(ctx).values():
        schema = _strip_titles(spec.input_model.model_json_schema())
        out.append({"name": spec.name, "description": spec.description, "input_schema": schema})
    return out


async def dispatch(ctx: ToolContext, name: str, raw_input: dict[str, Any]) -> dict[str, Any]:
    """Validate args against the tool's schema, then run the handler. Validation errors
    are returned (not raised) so the agent can self-correct and retry."""
    registry = _registry_for(ctx)
    spec = registry.get(name)
    if spec is None:
        return {"error": f"unknown tool {name!r}", "valid_tools": sorted(registry)}

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
        details = exc.errors(include_url=False, include_context=False)
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
