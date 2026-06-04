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
    aggregate_runs as aggregate_runs_tool,
)
from app.tools import (
    analyze,
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
    multiharness,
    observe,
    orchestrate,
    plan,
    probe,
    readiness,
    repos,
)
from app.tools.context import ToolContext
from app.tools.schemas import (
    AdviseAcceleratorsInput,
    AggregateRunsInput,
    AnalyzeResultsInput,
    CancelRunInput,
    CheckCapacityInput,
    CheckEndpointReadinessInput,
    CompareHarnessRunsInput,
    CompareReportsInput,
    ConvertGuideInput,
    DiscoverStackInput,
    EnsureReposInput,
    ExecuteInput,
    FetchKeyDocsInput,
    GenerateDoeInput,
    ListCatalogInput,
    LocateReportInput,
    ObserveRunMetricsInput,
    OrchestrateBenchmarkInput,
    ProbeEnvironmentInput,
    ProvisionHfSecretInput,
    ReadKnowledgeInput,
    ReadRepoDocInput,
    ResultHistoryInput,
    RunCommandInput,
    RunSetupInput,
    SearchKnowledgeInput,
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
        "via install_prereqs.sh. Prefer the dedicated tools (execute_llmdbenchmark, "
        "ensure_repos, run_setup) when one fits."
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
        "experiment YAML you pass via flags.experiments; its per-treatment reports land in "
        "the session workspace. Results from a 'run' are written into the session workspace too. "
        "To serve a NON-DEFAULT model (not the spec's scenario default), set the top-level "
        "`models` field (a HF id/short name) — it emits `-m`; FIRST run "
        "check_capacity(overrides={'model': <same id>}) so the pre-flight validates that EXACT "
        "model (sizing + gated access). WHICH model is your judgment — see "
        "knowledge/model_override.md. "
        "To RE-COLLECT/RE-ANALYZE the results of a prior `run` WITHOUT re-running the benchmark "
        "load, set flags={'skip': True} on a `run` (emits -z; collect-only, read-only/auto-runs) "
        "— see knowledge/collect_only.md for WHEN. "
        "To REPLAY a real dataset instead of a synthetic workload profile (run/experiment only), "
        "set flags.dataset to its URL/path — it emits `-x`; WHEN to replay vs stay synthetic is "
        "your judgment, see knowledge/dataset_replay.md. "
        "For a MULTI-STACK scenario (N model pools behind one gateway, e.g. guides/multi-model-wva), "
        "set flags.stack to a stack name or comma-separated subset (NAME[,NAME...]) to target ONE "
        "pool — it emits `--stack` on standup/smoketest/run/teardown; and set flags.parallel to an "
        "int to CAP how many stacks deploy in parallel (emits `--parallel` on standup/smoketest/"
        "experiment; lower it on a small/Kind node). WHICH stack(s) and HOW MANY at once is your "
        "judgment — see knowledge/multi_stack.md. "
        "To choose the GATEWAY PROVIDER instead of inheriting the spec's gateway.className, set "
        "flags.gateway_class to one of istio/agentgateway/gke/epponly/data-science-gateway-class — "
        "it emits --gateway-class on any subcommand (effective on the modelservice deploy path "
        "only); WHICH provider is your judgment, see knowledge/gateway_class.md. "
        "To give one slow PHASE more rope (or fail it faster), set the CLI's per-phase timeout "
        "keys in flags (seconds): wait_timeout/data_access_timeout on run+experiment; "
        "standalone_deploy_timeout/gateway_deploy_timeout/modelservice_deploy_timeout/"
        "kustomize_deploy_timeout/pvc_bind_timeout on standup; fma_teardown_timeout on teardown. "
        "Each is a DEEPER bound that MUST stay below the runner deadline for that subcommand so "
        "the two timeout layers don't fight — WHEN/WHAT to set is your judgment, see "
        "knowledge/phase_timeouts.md. "
        "To send a `run`'s results to a CLOUD bucket instead of local-only, set flags.output to a "
        "`gs://bucket/...` or `s3://bucket/...` URI (default is `local`); this is OPT-IN — WHETHER "
        "the user has a bucket and WHICH one is their choice, see knowledge/cloud_results_sink.md. "
        "To ALSO generate the CLI's local matplotlib plot families (per-request distributions, "
        "session-lifecycle, Prometheus time-series) beside the harness PNGs, set flags={'analyze': "
        "True} on a `run` — it emits `--analyze`; the plots are surfaced via the artifact route and "
        "are SUPPLEMENTARY (your SLO/goodput/Pareto math is unchanged). WHEN to ask for them is "
        "your judgment, see knowledge/analysis.md. "
        "To round-trip a reusable run-config via the CLI's OWN mechanism (run only): set "
        "flags={'generate_config': True} to GENERATE a run-config YAML from current settings under "
        "the session workspace and exit (emits --generate-config; read-only/auto-runs), then later "
        "set flags={'run_config': '<path>'} to REPLAY it (emits -c; still approval-gated). WHEN to "
        "generate vs reuse vs author in-workspace is your judgment, see "
        "knowledge/runconfig_roundtrip.md. "
        "To launch a DEBUG harness pod that sleeps (`sleep infinity`) INSTEAD of running the "
        "benchmark — so you (or the user) can exec into a misbehaving harness pod — set "
        "flags={'debug': True} on a run/experiment (emits -d; still approval-gated, it launches a "
        "real pod). NOT on teardown (there -d means --deep, a destructive wipe). After it is up, "
        "EXPLAIN how to exec into it (kubectl/oc exec -it <ns> <harness-pod> -- bash) but do NOT "
        "drive the interactive shell yourself — that stays a manual user step. WHEN to use it is "
        "your judgment, see knowledge/harness_debug.md. "
        "For the OPTIONAL git-like Results Store (a TEAM-SHARED store that publishes/pulls runs "
        "via GCS remotes), use subcommand='results' with the top-level `store` field "
        "({command: init/remote/status/add/rm/ls/push/pull, ...}): init/status/ls/remote-ls are "
        "read-only/auto-run; add/rm/push/pull/remote-add/remote-rm are mutating/approval-gated. "
        "This is SEPARATE from your OWN local history (the result_history tool) — that local "
        "store is unchanged; reach for the CLI store ONLY for team GCS sharing. WHICH store and "
        "WHEN is your judgment, see knowledge/history.md."
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
        ToolSpec("advise_accelerators", _DESCRIPTIONS["advise_accelerators"], AdviseAcceleratorsInput, probe.advise_accelerators),
        ToolSpec("read_knowledge", _DESCRIPTIONS["read_knowledge"], ReadKnowledgeInput, probe.read_knowledge),
        ToolSpec("search_knowledge", _DESCRIPTIONS["search_knowledge"], SearchKnowledgeInput, probe.search_knowledge),
        ToolSpec("read_repo_doc", _DESCRIPTIONS["read_repo_doc"], ReadRepoDocInput, probe.read_repo_doc),
        ToolSpec("fetch_key_docs", _DESCRIPTIONS["fetch_key_docs"], FetchKeyDocsInput, probe.fetch_key_docs),
        ToolSpec("propose_session_plan", _DESCRIPTIONS["propose_session_plan"], SessionPlan, plan.propose_session_plan),
        ToolSpec("check_capacity", _DESCRIPTIONS["check_capacity"], CheckCapacityInput, capacity.check_capacity),
        ToolSpec("aggregate_runs", _DESCRIPTIONS["aggregate_runs"], AggregateRunsInput, aggregate_runs_tool.aggregate_runs),
        ToolSpec("provision_hf_secret", _DESCRIPTIONS["provision_hf_secret"], ProvisionHfSecretInput, hf_secret.provision_hf_secret),
        ToolSpec("check_endpoint_readiness", _DESCRIPTIONS["check_endpoint_readiness"], CheckEndpointReadinessInput, readiness.check_endpoint_readiness),
        ToolSpec("discover_stack", _DESCRIPTIONS["discover_stack"], DiscoverStackInput, discover.discover_stack),
        ToolSpec("ensure_repos", _DESCRIPTIONS["ensure_repos"], EnsureReposInput, repos.ensure_repos),
        ToolSpec("run_setup", _DESCRIPTIONS["run_setup"], RunSetupInput, repos.run_setup),
        ToolSpec("write_and_validate_config", _DESCRIPTIONS["write_and_validate_config"], WriteConfigInput, config_artifact.write_and_validate_config),
        ToolSpec("convert_guide_to_scenario", _DESCRIPTIONS["convert_guide_to_scenario"], ConvertGuideInput, convert_guide.convert_guide_to_scenario),
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
