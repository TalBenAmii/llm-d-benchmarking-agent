"""Pydantic input models for every agent tool. These are the single source of truth
for each tool's argument schema: they validate the LLM's tool-call arguments
(determinism gate a) and are exported as JSON Schema in the provider tool definitions.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.validation.session_plan import AutotuneKnob


class ProbeEnvironmentInput(BaseModel):
    checks: list[str] | Literal["all"] = Field(
        default="all",
        description="Which checks to run, or 'all'. Options: container_runtime, repos, "
                    "tools, venv, kind_clusters, kube_context, cluster_info, namespaces, stack, "
                    "prometheus_crds (are the Prometheus-operator PodMonitor/ServiceMonitor CRDs "
                    "installed? read it before deciding --monitoring vs --no-monitoring), "
                    "node_capacity (per-node allocatable/capacity CPU + the min allocatable across "
                    "nodes — read it to right-size LLMDBENCH_HARNESS_CPU_NR for a small/Kind node), "
                    "cluster_preconditions (the K8s server major.minor from `kubectl version` + the "
                    "`spec`'s pinned vLLM/NIXL/UCX/NVSHMEM image tags — read it BEFORE a long "
                    "real-cluster standup for an honest go/no-go: the go/no-go thresholds and "
                    "verdict wording live in knowledge/infrastructure_preconditions.yaml, not here), "
                    "provider_detection (detect the cloud provider — openshift/gke/doks/aks vs kind — "
                    "from node labels + surface each node's GPU taints; read it to adapt commands "
                    "and unstick Pending/PROGRAMMED=False failures: the which-CLI (oc vs kubectl) / "
                    "which-toleration / which-known-issue (GMP / 'Undetected platform' / NVSHMEM) "
                    "judgment lives in knowledge/infra_providers.yaml, not here)",
    )
    namespace: str | None = Field(default=None, description="Namespace to check for an existing stack")
    spec: str | None = Field(
        default=None,
        description="Spec whose scenario image tags to parse for the cluster_preconditions check, "
                    "e.g. 'cicd/kind' (resolves to config/scenarios/<spec>.yaml). Omit it for the "
                    "other checks.",
    )


class AdviseAcceleratorsInput(BaseModel):
    namespace: str | None = Field(
        default=None,
        description="Optional namespace (unused by the node-level extraction; reserved for "
                    "future per-namespace scoping). Node-advertised accelerator facts are "
                    "cluster-wide.",
    )
    # This tool reads each node's ADVERTISED accelerator/CPU/memory facts via the read-only
    # `kubectl get nodes -o json` (which extended-resource key — nvidia.com/gpu or the
    # amd/gaudi/tpu/xpu siblings — a node advertises, vs CPU-only, plus per-node cpu/memory).
    # It returns FACTS ONLY — no can-it-run verdict. To turn the facts into a
    # "can my hardware actually run this?" answer, the agent must call
    # read_knowledge('accelerators') for the CUDA/driver minimums, the Device-Plugin vs DRA
    # choice, and the real (non-sim) CPU-only 64c/64GB-per-replica floor (Kind/CPU-sim exempt).


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


class ReadKnowledgeInput(BaseModel):
    name: str = Field(
        ...,
        description="The knowledge topic to load, by its basename (with or without "
                    "extension), e.g. 'capacity', 'analysis', 'multi_harness'. Must be one "
                    "of the on-demand topics listed in the system prompt's knowledge index. "
                    "No paths, no '..', no absolute paths.",
    )


class SearchKnowledgeInput(BaseModel):
    query: str = Field(
        ...,
        description="Free-text keywords/topic describing the problem or question, e.g. "
                    "'pods stuck pending unschedulable', 'gateway PROGRAMMED false', "
                    "'kv cache hit rate metric', 'how to lower harness cpu'. The search is "
                    "lexical (weighted keyword overlap) over every knowledge guide AND the "
                    "curated upstream repo-doc index — no exact basename needed.",
        min_length=1,
    )
    limit: int = Field(
        default=5, ge=1, le=20,
        description="Max number of ranked results to return (default 5).",
    )
    include_repo_docs: bool = Field(
        default=True,
        description="Also search the curated upstream repo-doc index "
                    "(knowledge/useful_repo_docs.md) and return repo-doc POINTERS you can open "
                    "with read_repo_doc. Set False to search only the agent's own knowledge/ "
                    "guides.",
    )


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
    artifact_type: Literal["workload", "run_config", "scenario"] = Field(
        ...,
        description="What kind of config artifact to author. 'workload'/'run_config' write a "
                    "stock-shaped YAML as-is (MVP, rarely needed). 'scenario' AUTHORS finer "
                    "per-knob vLLM/scheduling/storage edits beyond the parallelism/memory knobs "
                    "that check_capacity + generate_doe_experiment already cover — see `content`.",
    )
    target_filename: str = Field(..., description="Bare *.yaml filename (no path separators)")
    content: dict[str, Any] = Field(
        ...,
        description="The config body. For 'workload'/'run_config' it is written verbatim. "
                    "For 'scenario' it is the set of per-knob OVERRIDES merged onto a minimal "
                    "`scenario: [ {name, ...} ]` skeleton: a REQUIRED 'name' (the scenario item "
                    "name) plus >=1 override keyed by the DOTTED upstream scenario field path. "
                    "Supported knob paths include vllmCommon.flags.* (e.g. enforceEager, "
                    "noPrefixCaching), vllmCommon.kvTransfer.* (enabled/connector/role), "
                    "vllmCommon.kvEvents.* (enabled/publisher/port/topicPrefix), "
                    "vllmCommon.priorityClassName, vllmCommon.ephemeralStorage, "
                    "vllmCommon.networkResource, affinity.* (enabled/nodeSelector/podAffinity/"
                    "podAntiAffinity), schedulerName, routing.servicePort, and per-section "
                    "decode.*/prefill.* (schedulerName, priorityClassName, ...). To author a "
                    "KUSTOMIZE-method deploy (Phase 46), set the kustomize.* family instead: "
                    "kustomize.enabled (true ⇒ deploy the upstream llm-d guide directly — this "
                    "OVERRIDES the rest of the scenario), kustomize.guideName (required; the "
                    "guides/<name> dir), kustomize.repoPath (a local llm-d clone; else upstream "
                    "clones it), kustomize.repoRef, kustomize.acceleratorBackend, "
                    "kustomize.monitoring, kustomize.overlayPath, kustomize.extraHelmValues, "
                    "kustomize.extraHelmSets, kustomize.guideVariableOverrides, and "
                    "kustomize.patches (a LIST of {patch: <inline YAML>} strategic-merge patches "
                    "against the guide's modelserver base). To CONFIGURE OpenTelemetry "
                    "distributed tracing on the deployed modelservice pods (Phase 54), set the "
                    "tracing.* family: tracing.enabled (true ⇒ turn it on), tracing.otlpEndpoint "
                    "(the OTLP gRPC endpoint of the USER'S OTel collector, e.g. "
                    "http://otel-collector:4317), tracing.sampling.sampler (e.g. "
                    "parentbased_traceidratio) + tracing.sampling.samplerArg ('1.0'=100%, "
                    "'0.1'=10%), tracing.serviceNames.{vllmDecode,vllmPrefill,routingProxy}, and "
                    "tracing.vllm.collectDetailedTraces. NOTE: the benchmark only CONFIGURES "
                    "tracing — it never deploys a collector/Jaeger and never collects/shows "
                    "traces in the report (the user views them in their own backend). The knobs "
                    "are SHAPE-validated against the repo's own scenario examples (read live). "
                    "WHICH knobs to set is JUDGMENT — call read_knowledge('vllm_overrides') for "
                    "vLLM tuning, read_knowledge('observability') for the tracing.* block + its "
                    "config-only limitation, or read_knowledge('deploy_path_playbook') for WHICH "
                    "guide/overlay/patches/repo the kustomize block should carry; the repos stay "
                    "read-only (authored into the session workspace). Preview the authored file "
                    "via execute_llmdbenchmark(subcommand='plan'/'run', flags={'dry_run': True}).",
    )


class ConvertGuideInput(BaseModel):
    name: str = Field(
        ...,
        description="The guide/scenario name token (letters/digits/_/-/. only). It becomes "
                    "ai.<name>.sh + ai.<name>.yaml in the SESSION WORKSPACE — the upstream "
                    "'ai.' prefix marks an agent-generated scenario. The read-only repos are "
                    "NEVER written; output goes to the session workspace only.",
    )
    env: dict[str, str] = Field(
        ...,
        description="REQUIRED. The already-resolved LLMDBENCH_* -> value map you derived from "
                    "the guide. Each key MUST start with 'LLMDBENCH_' (e.g. "
                    "{'LLMDBENCH_DEPLOY_MODEL_LIST': 'Qwen/Qwen3-32B', "
                    "'LLMDBENCH_VLLM_MODELSERVICE_DECODE_REPLICAS': '2'}); >=1 entry. The "
                    "mapping JUDGMENT — WHICH Helm/kustomize path maps to WHICH LLMDBENCH_* var, "
                    "the standard practices (DECODE_MODEL_COMMAND=custom, REPLACE_ENV_* "
                    "placeholders, the preprocess command), and which defaults to override — is "
                    "read_knowledge('convert_guide'), NOT this tool. The tool only EMITS the "
                    "sorted, shell-quoted exports into the workspace .sh.",
    )
    sources: dict[str, str] | None = Field(
        default=None,
        description="Optional per-LLMDBENCH_* var -> a short source-trace string (e.g. "
                    "'ms/values.yaml lines 23-24'), emitted as a '# SOURCE:' comment above "
                    "each export for upstream's traceability requirement. Keys not present in "
                    "`env` are ignored.",
    )
    scenario: dict[str, Any] | None = Field(
        default=None,
        description="Optional per-knob dotted-path overrides for the VALIDATABLE companion YAML "
                    "twin (same shape as write_and_validate_config content for "
                    "artifact_type='scenario': a 'name' is forced to <name>, plus >=1 DOTTED "
                    "upstream scenario field path, e.g. {'model.shortName': 'qwen3-32b', "
                    "'decode.parallelism.tensor': 2}). The twin is what the determinism gate "
                    "(plan/--dry-run) actually validates — a bare .sh is NOT gate-able. Omit it "
                    "to derive a minimal twin carrying just the scenario name.",
    )
    harness: str | None = Field(
        default=None,
        description="Optional harness recorded into the .sh as LLMDBENCH_HARNESS_NAME. "
                    "Defaults to 'inference-perf' (the upstream convert-guide default).",
    )
    profile: str | None = Field(
        default=None,
        description="Optional workload profile recorded into the .sh as "
                    "LLMDBENCH_HARNESS_EXPERIMENT_PROFILE. Defaults to 'sanity_random.yaml' "
                    "(the upstream convert-guide default).",
    )
    source_ref: str | None = Field(
        default=None,
        description="Optional guide URL/path, recorded only as a provenance header comment in "
                    "the .sh (e.g. 'https://github.com/llm-d/llm-d/tree/main/guides/"
                    "inference-scheduling'). Not fetched by this tool — you read the guide "
                    "yourself via read_repo_doc / run_command git clone / your own file reads.",
    )


class ExecuteInput(BaseModel):
    subcommand: Literal["plan", "standup", "smoketest", "run", "teardown", "results", "experiment"]
    spec: str | None = Field(default=None, description="Spec name from the catalog, e.g. 'cicd/kind'")
    namespace: str | None = None
    harness: str | None = Field(default=None, description="run/experiment only")
    workload: str | None = Field(default=None, description="run/experiment only")
    models: str | None = Field(
        default=None,
        description="A single model id (HF id or short name, e.g. 'facebook/opt-125m', "
                    "'meta-llama/Llama-3.1-8B') to deploy/serve, OVERRIDING the spec's "
                    "scenario-default model (valid on standup/plan/run/experiment). Emitted as "
                    "`-m <id>` (upstream --models on standup/plan/experiment, --model on run; -m "
                    "works on all). WHICH model is YOUR judgment (knowledge/model_override.md; no "
                    "enumerable catalog). CRITICAL: FIRST pass the SAME id to "
                    "check_capacity(overrides={'model': <id>}) so the pre-flight validates the "
                    "IDENTICAL model (HF config lookup, sizing, gated access). Omit to keep the "
                    "spec's default.",
    )
    kubeconfig: str | None = Field(
        default=None,
        description="Path to a NON-DEFAULT kubeconfig FILE to target a remote cluster for THIS "
                    "command instead of the ambient kube context. Emitted as `-k <path>` (upstream "
                    "--kubeconfig / LLMDBENCH_KUBECONFIG); valid on every subcommand; a non-secret "
                    "path, allowlist value-pinned (no `..`). WHEN/WHICH cluster is YOUR judgment "
                    "(knowledge/preconditions.md; no enumerable catalog). Omit for the ambient "
                    "context (the local Kind cluster for the quickstart). To target by API URL + "
                    "bearer TOKEN instead, see flags.cluster_url / flags.cluster_token — the TOKEN "
                    "is a SECRET, backend-only (never argv, never shown).",
    )
    store: dict[str, Any] | None = Field(
        default=None,
        description="ONLY for subcommand='results': drives the CLI's OPTIONAL git-like, "
                    "TEAM-SHARED Results Store (publishes/pulls runs via GCS remotes) — DISTINCT "
                    "from the agent's OWN local history (the result_history tool); reach for it "
                    "only for team sharing. read_knowledge('history') for WHICH and WHEN. Shape "
                    "{command, ...}: init/status/ls and remote 'ls' are read-only/auto-run; "
                    "add/rm/push/pull and remote add/rm are mutating/approval-gated. `command` one "
                    "of init/remote/status/add/rm/ls/push/pull — init: create local .result_store/; "
                    "status: list local runs; remote: manage remotes (remote_action "
                    "add{name,uri=gs://bucket/prefix} / rm{name} / ls); add/rm: stage/unstage "
                    "`paths` (dirs or run-uids); ls: list a remote (alias + optional model/hardware "
                    "filters; no wildcards); push: publish staged runs to a remote (default "
                    "staging); pull: download a run (default prod remote; REQUIRED run_uid). The "
                    "local history store is unchanged.",
    )
    flags: dict[str, Any] | None = Field(
        default=None,
        description="Optional dict of CLI knobs — each documented below with what it emits, "
                    "which subcommands accept it, whether it is read-only or approval-gated, "
                    "and the knowledge guide carrying the WHEN/WHICH judgment (load that guide "
                    "before relying on a flag). Keys: skip, skip_smoketest, dry_run, "
                    "list_endpoints, methods, repo_path, output, endpoint_url, monitoring, "
                    "harness_cpu_nr, cluster_url, cluster_token, step, dataset, analyze, stack, "
                    "parallel, gateway_class, wait_timeout, data_access_timeout, "
                    "standalone_deploy_timeout, gateway_deploy_timeout, "
                    "modelservice_deploy_timeout, kustomize_deploy_timeout, pvc_bind_timeout, "
                    "fma_teardown_timeout, generate_config, run_config, debug. Per key: `skip` "
                    "=> -z on a run (collect-only re-analysis of a prior run's results; "
                    "read-only/auto-runs; knowledge/collect_only.md). `skip_smoketest` skips "
                    "the smoketest; `dry_run` previews only and `list_endpoints` lists resolved "
                    "endpoints (both read-only). `methods` => -t deploy method "
                    "(standalone/modelservice/kustomize/fma). `repo_path` => --llmd-repo-path: "
                    "a LOCAL llm-d clone for the kustomize method (standup); else upstream "
                    "clones llm-d.git. The kustomize.* config block is authored via "
                    "write_and_validate_config(artifact_type='scenario'), not here; "
                    "knowledge/deploy_path_playbook.md. `output` => results destination keyword "
                    "'local' (default) or a 'gs://...'/'s3://...' bucket URI (cloud is opt-in); "
                    "knowledge/cloud_results_sink.md. `endpoint_url` => benchmark an existing "
                    "OpenAI-compatible endpoint directly. `monitoring` => True emits "
                    "--monitoring (PodMonitor/ServiceMonitor + EPP verbosity on standup; "
                    "scrapes vLLM /metrics on run/experiment); False emits --no-monitoring on "
                    "STANDUP ONLY (opt-out for clusters lacking the Prometheus-operator CRDs); "
                    "omit for scenario default (default ON — probe prometheus_crds first); "
                    "knowledge/observability.md. `harness_cpu_nr` => backend-only env "
                    "LLMDBENCH_HARNESS_CPU_NR (NOT a CLI flag; default 16) — lower on a "
                    "small/Kind node so the launcher pod schedules; "
                    "knowledge/harness_sizing.md. `cluster_url`/`cluster_token` => target a "
                    "remote cluster by API URL + bearer token (alternative to the `kubeconfig` "
                    "file): carried BACKEND-ONLY as "
                    "LLMDBENCH_CLUSTER_URL/LLMDBENCH_CLUSTER_TOKEN env (never argv/event/log; "
                    "scrubbed like HF_TOKEN); the token is a SECRET — never echo it; "
                    "knowledge/preconditions.md. `step` => -s step-list (e.g. '5', '5-9', "
                    "'3-5,9') on standup/smoketest/run/teardown to re-run one failed "
                    "step/range; does NOT change a command's mutating mode; "
                    "knowledge/step_select.md. `dataset` => -x URL/path on run/experiment to "
                    "REPLAY a real dataset instead of the synthetic profile; "
                    "knowledge/dataset_replay.md. `analyze` => --analyze on a run for "
                    "SUPPLEMENTARY matplotlib plots (distributions/session/graphs); your "
                    "SLO/goodput/Pareto math is unchanged; knowledge/analysis.md. `stack` => "
                    "--stack NAME[,NAME...] restricts a multi-stack scenario to a subset "
                    "(standup/smoketest/run/teardown); `parallel` => --parallel <int> caps how "
                    "many stacks deploy in parallel (standup/smoketest/experiment; DISTINCT "
                    "from parallelism/-j harness pods); knowledge/multi_stack.md. "
                    "`gateway_class` => --gateway-class <provider> "
                    "(istio/agentgateway/gke/epponly/data-science-gateway-class) on any "
                    "subcommand, overriding the spec's gateway.className (modelservice deploy "
                    "path only); knowledge/gateway_class.md. Per-phase CLI timeouts are "
                    "positive-int SECONDS, each emitting the matching --*-timeout and MUST stay "
                    "below the runner's per-command deadline: "
                    "`wait_timeout`/`data_access_timeout` on run+experiment; "
                    "`standalone_deploy_timeout` / `gateway_deploy_timeout` / "
                    "`modelservice_deploy_timeout` / `kustomize_deploy_timeout` / "
                    "`pvc_bind_timeout` on standup; `fma_teardown_timeout` on teardown; "
                    "knowledge/phase_timeouts.md. `generate_config` => --generate-config on a "
                    "run (writes a reusable run-config YAML and exits; read-only/auto-runs); "
                    "`run_config` => -c <path> to REPLAY one (run-only; still approval-gated); "
                    "knowledge/runconfig_roundtrip.md. `debug` => -d on run/experiment ONLY "
                    "(harness pods sleep instead of running the load; still approval-gated; on "
                    "teardown -d means --deep so it is NOT emitted there) — explain how to exec "
                    "in but do not drive the shell; knowledge/harness_debug.md. For "
                    "subcommand='experiment': {experiments (path to the experiment YAML), "
                    "workspace, parallelism (int), overrides ('p=v,...'), stop_on_error, "
                    "skip_teardown}.",
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


class DiscoverStackInput(BaseModel):
    endpoint_url: str = Field(
        ...,
        description="REQUIRED. The OpenAI-compatible endpoint URL of the deployed stack to trace, "
                    "e.g. 'https://model.example.com/v1' or an in-cluster service URL. Phase 56: "
                    "this OPTIONAL tool runs the standalone stack-discovery tool "
                    "(`llm-d-discover <url> -f benchmark-report`) to capture the LIVE stack as "
                    "BR-v0.2 scenario.stack components (model/role/replicas/parallelism/"
                    "accelerator) for richer ENVIRONMENT capture than the agent's own endpoint "
                    "probing. It COMPLEMENTS — it does NOT replace — probe_environment / "
                    "check_endpoint_readiness, which remain the default. WHEN to use it is "
                    "read_knowledge('stack_discovery'). Value-pinned by the allowlist endpoint_url "
                    "constraint (same as `run -U/--endpoint-url`).",
    )
    kubeconfig: str | None = Field(
        default=None,
        description="Optional path to a NON-DEFAULT kubeconfig FILE to target a remote cluster "
                    "(emitted as `-k`). A plain, NON-SECRET file path, value-pinned by the "
                    "allowlist (no `..` traversal); omit it to use the ambient kube context. The "
                    "secret cluster-by-URL+TOKEN route is NOT exposed here — it stays backend-only "
                    "(as for execute_llmdbenchmark).",
    )
    context: str | None = Field(
        default=None,
        description="Optional Kubernetes context name to use (emitted as `-c`). Omit to use the "
                    "current context.",
    )
    filter_type: str | None = Field(
        default=None,
        description="Optional component-type filter to narrow the discovered components (emitted "
                    "as `--filter`, e.g. 'Pod', 'Service', 'vllm'). Omit to capture all "
                    "components.",
    )


class CheckEndpointReadinessInput(BaseModel):
    namespace: str = Field(
        ...,
        description="Kubernetes namespace whose inference endpoint to check for readiness "
                    "(the namespace you intend to benchmark).",
    )
    spec: str | None = Field(
        default=None,
        description="Optional llm-d spec from the catalog (e.g. 'cicd/kind'); used only to "
                    "scope the corroborating benchmark-CLI endpoint probe.",
    )
    probe_cli_endpoints: bool = Field(
        default=True,
        description="Also corroborate via the benchmark CLI's read-only `run --list-endpoints` "
                    "(best-effort; the Kubernetes endpoint-address readiness is the gate). Set "
                    "False to skip it (e.g. the benchmark venv isn't installed yet).",
    )
    check_gateway: bool = Field(
        default=True,
        description="In gateway-mode deploys, ALSO read the Gateway-API control plane "
                    "(gateway/gatewayclass/inferencepool/httproute) and surface the PROGRAMMED + "
                    "Accepted/ResolvedRefs/Reconciled condition FACTS (read-only). This tells "
                    "'the model pods are Ready' apart from 'traffic can actually reach them' — "
                    "pods can be Ready while the Gateway is still PROGRAMMED:False. Set False on "
                    "non-gateway/Kind deploys to skip the four extra kubectl reads.",
    )


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


class AggregateRunsInput(BaseModel):
    results_prefix: str = Field(
        ...,
        description="An EXISTING results dir holding the per-run result directories from "
                    "completed runs (the upstream naming convention is "
                    "'{results_prefix}/{harness}_{run_id}_{stack}'). This tool READS the "
                    "Benchmark Report v0.2 files there; it does NOT run a benchmark.",
    )
    harness: str = Field(
        ...,
        description="The harness whose repeated runs to aggregate (e.g. 'inference-perf') — "
                    "part of the per-run directory name.",
    )
    stack: str = Field(
        ...,
        description="The stack name the runs targeted (e.g. 'llm-d-7b-base') — part of the "
                    "per-run directory name.",
    )
    run_ids: list[str] = Field(
        ...,
        description="The run IDs to combine (>=2 — aggregation needs repeated runs of the SAME "
                    "benchmark to report run-to-run mean/std/min/max).",
    )
    output_name: str | None = Field(
        default=None,
        description="Optional subdir name (under the session workspace) to write the "
                    "aggregated_summary.{txt,json} into. Defaults to 'aggregated'. Must stay "
                    "within the workspace (no '..').",
    )


class ProvisionHfSecretInput(BaseModel):
    namespace: str = Field(
        ...,
        description="The target Kubernetes namespace (an RFC1123 label, e.g. the plan's "
                    "namespace) to create/update the HuggingFace token Secret in. This is the "
                    "APPROVAL-GATED MUTATING step that materializes the cluster HF Secret a "
                    "GATED-model standup needs (so a `standup` doesn't fail minutes in with an "
                    "opaque image-pull/weights error). The token itself is BACKEND-ONLY (read "
                    "from the backend HF_TOKEN env, never shown, never an input here). WHEN to "
                    "call this is knowledge/capacity.md, NOT your guess: ONLY after a "
                    "check_capacity GATED+UNAUTHORIZED verdict whose reason says NO token is "
                    "configured cluster-side — never for a public model, and never when a token "
                    "merely LACKS access (that needs a HuggingFace access request, not a secret).",
    )
    name: str | None = Field(
        default=None,
        description="The Secret name (an RFC1123 object name). Omit to use the upstream "
                    "default 'llm-d-hf-token' (HF_TOKEN_NAME) that the llm-d standup expects; "
                    "only override it if the deployment was configured with a different name.",
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
    start_date: str | None = Field(
        default=None,
        description="action=list/trend: only include results STORED on or after this date "
                    "(inclusive). Accepts an ISO-8601 date ('2026-05-01') or datetime "
                    "('2026-05-01T00:00:00'); a bare date is treated as 00:00:00 UTC that day. "
                    "Filters on each record's stored_at (when it was persisted to history), the "
                    "only timestamp every record carries. Omit for no lower bound.",
    )
    end_date: str | None = Field(
        default=None,
        description="action=list/trend: only include results STORED on or before this date. A "
                    "bare date ('2026-06-15') is treated as the END of that day (23:59:59.999 "
                    "UTC) so the day is inclusive; a full datetime is used as-is. Filters on "
                    "stored_at. Omit for no upper bound.",
    )


class AutotuneSearchInput(BaseModel):
    """Search-state tracker for the closed-loop autotuner. MECHANISM ONLY — it tracks the
    trial log, VALIDATES the candidate YOU computed, and exposes FACTS. It NEVER computes the
    next config and NEVER returns a converge/stop verdict. The search STRATEGY and the STOP
    decision are YOURS, grounded in read_knowledge('autotune_strategy'). All actions auto-run
    (read/write only the session workspace; nothing touches the cluster or the repos)."""

    action: Literal["record_trial", "propose_next_config", "status"] = Field(
        ...,
        description="record_trial = log the result of a trial you just ran (validates the "
                    "Benchmark Report, evaluates it against the plan's SLO, appends it). "
                    "propose_next_config = ask the tool to VALIDATE the candidate config YOU "
                    "computed (in-bounds? duplicate? budget left?) BEFORE you run it — it does "
                    "NOT compute the value for you. status = read the convergence FACTS "
                    "(incumbent, feasible frontier, budget left, recent improvement, whether the "
                    "SLO boundary is bracketed) so YOU can decide converge/continue per "
                    "read_knowledge('autotune_strategy'). The tool returns NO converge/stop verdict.",
    )
    search_id: str = Field(
        ...,
        min_length=1,
        description="A stable id you pick for THIS goal-seeking session (e.g. "
                    "'chat-ttft-concurrency'). Keys the trial log; reuse it across every action "
                    "of the same search.",
    )
    slo: dict[str, Any] | None = Field(
        default=None,
        description="The SLO constraint from the approved SessionPlan's `slo` block (same keys "
                    "as analyze_results.slo: ttft_ms/tpot_ms/itl_ms/request_latency_ms/"
                    "throughput_floor_tok_s/min_success_rate_pct/percentile). Used to evaluate "
                    "each trial's feasibility and the SLO-feasible frontier (REUSES the analyzer). "
                    "Pass it on record_trial and status; the trial's feasibility comes from it.",
    )
    objective: str | None = Field(
        default=None,
        description="The objective metric to optimize, from the autotune plan (e.g. "
                    "'output_token_rate', 'ttft', 'tpot', 'request_latency'). Used to compute "
                    "each trial's objective_value and to pick the incumbent. Required for "
                    "record_trial and status to be meaningful.",
    )
    direction: Literal["max", "min"] | None = Field(
        default=None,
        description="'max' or 'min' for the objective (from the autotune plan). Used to choose "
                    "the incumbent and to sign the recent-improvement fact. Pure facts — never a "
                    "stop decision.",
    )
    config: dict[str, Any] | None = Field(
        default=None,
        description="action=record_trial: the knob value(s) used THIS trial, e.g. "
                    "{'max-concurrency': 16}. Keyed by each knob's dotted `key`.",
    )
    report_source: str | None = Field(
        default=None,
        description="action=record_trial: the run dir or Benchmark Report file this trial "
                    "produced. The report is schema-validated before it is recorded — an "
                    "unvalidated report is REFUSED, never logged (determinism gate d).",
    )
    candidate: dict[str, Any] | None = Field(
        default=None,
        description="action=propose_next_config: the next config YOU computed (per your "
                    "strategy), e.g. {'max-concurrency': 24}. The tool only VALIDATES it "
                    "(bounds/duplicate/budget); it does not produce or alter the value.",
    )
    knobs: list[AutotuneKnob] | None = Field(
        default=None,
        description="The knob bounds to validate a candidate against (the autotune plan's "
                    "knobs). Required for propose_next_config so out-of-bounds candidates are "
                    "rejected. Mirror the approved plan's bounds.",
    )
    budget: int | None = Field(
        default=None,
        ge=1,
        description="The trial budget from the approved autotune plan, so the tool can report "
                    "budget_remaining and reject a candidate once it's exhausted. Pass it on "
                    "propose_next_config and status.",
    )


class ExportRunBundleInput(BaseModel):
    """Capture a reproducibility PROVENANCE BUNDLE for a validated run (read-only: git reads +
    a workspace write). See read_knowledge('reproducibility')."""

    source: str = Field(
        ...,
        description="A Benchmark Report file OR a run directory (its newest report is used). The "
                    "report is schema-validated FIRST — an unvalidated report is refused (a bundle "
                    "only ever certifies a schema-valid run).",
        min_length=1,
    )
    namespace: str | None = Field(
        default=None,
        description="The namespace the run targeted (from the approved SessionPlan). Used to "
                    "build the copy-paste regenerate command (llmdbenchmark run -c <cfg> -p <ns>).",
    )
    spec: str | None = Field(default=None, description="spec used (provenance / re-derive a rerun plan).")
    harness: str | None = Field(default=None, description="harness used (provenance); falls back to the report's own.")
    workload: str | None = Field(default=None, description="workload used (provenance).")
    model: str | None = Field(default=None, description="model served (provenance); falls back to the report's own.")
    slo: dict[str, Any] | None = Field(
        default=None,
        description="The approved SessionPlan's SLO block, if any, so a reproduce can re-derive "
                    "the SLO verdicts.",
    )
    label: str | None = Field(default=None, description="A short human label for this bundle, e.g. '8B baseline'.")
    attach_to_history: bool = Field(
        default=False,
        description="Also attach this bundle's id + provenance to the matching stored history "
                    "record (if one exists). The result is stored separately via result_history; "
                    "this just links them.",
    )
    session_id: str | None = Field(default=None, description="Originating chat id (provenance).")


class ReproduceRunInput(BaseModel):
    """Read a saved provenance bundle and return a structured rerun PROPOSAL — it mutates
    NOTHING. The agent then drives propose_session_plan -> dry-run -> approved -c replay. See
    read_knowledge('reproducibility')."""

    bundle_id: str = Field(
        ...,
        description="The id of a previously exported provenance bundle (from export_run_bundle or "
                    "a history record). reproduce_run returns the captured spec/harness/workload/"
                    "namespace/slo + run-config path + the dry-run-FIRST sequence; it emits NO "
                    "mutating command. Replay still goes through SessionPlan approval + the CLI "
                    "--dry-run gate, never around them.",
        min_length=1,
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


class CancelRunInput(BaseModel):
    session_id: str = Field(
        ...,
        description="The chat/session id whose in-flight run to cancel (as shown in "
                    "/api/sessions or the `ready` event's session_id). Cancelling frees the "
                    "run's concurrency slot and cleans up its subprocess. You cannot cancel the "
                    "run you are calling from — cancel a DIFFERENT session's run.",
        min_length=1,
    )


class DoEFactor(BaseModel):
    """One swept parameter in a DoE matrix: a human `name`, the dotted config `key` the
    level overrides, and the list of `levels` to sweep. The cross-product of all factors'
    levels becomes the treatments. WHICH factor/levels to pick is your judgment (see
    knowledge/sweep_playbook.md) — this only declares one axis of the grid."""

    name: str = Field(
        ...,
        description="Short token naming this factor, used to build treatment names "
                    "(e.g. 'tp', 'rep', 'numCpuBlocks'). Letters/digits/_/-/. only.",
    )
    key: str = Field(
        ...,
        description="The DOTTED override key this factor sets in each treatment, e.g. "
                    "'decode.parallelism.tensor' or 'data.shared_prefix.num_groups' (setup "
                    "factors override the scenario config; run factors override the workload "
                    "profile). Read the repo's experiment examples to pick real keys.",
    )
    levels: list[Any] = Field(
        ...,
        description="The scalar values to sweep for this factor, e.g. [2, 4, 8]. The "
                    "cross-product of every factor's levels yields the treatments. Non-empty.",
        min_length=1,
    )


class GenerateDoeInput(BaseModel):
    name: str = Field(
        ...,
        description="Experiment name (a token: letters/digits/_/-/. only). Also the default "
                    "output filename (<name>.yaml).",
    )
    run_factors: list[DoEFactor] = Field(
        ...,
        description="REQUIRED. The workload/run factors to sweep against a single stood-up "
                    "stack (each: name + dotted key + levels). The cross-product of these is "
                    "the run treatments. Prefer a run-parameter sweep on kind/CPU-sim.",
        min_length=1,
    )
    setup_factors: list[DoEFactor] | None = Field(
        default=None,
        description="Optional infrastructure factors that change the DEPLOYMENT (replicas, "
                    "tensor parallelism, prefill/decode split, model). Each setup treatment "
                    "triggers its own standup/teardown — a full DoE. Omit for a run-only sweep "
                    "(one standup, N runs). The full matrix is setup × run treatments.",
    )
    run_constants: dict[str, Any] | None = Field(
        default=None,
        description="Optional dotted-key → value pairs held FIXED across every run treatment "
                    "(e.g. {'data.shared_prefix.output_len': 256}). Keep everything not being "
                    "swept fixed so deltas are attributable.",
    )
    setup_constants: dict[str, Any] | None = Field(
        default=None,
        description="Optional dotted-key → value pairs merged into every setup treatment "
                    "(e.g. {'model.maxModelLen': 16000}).",
    )
    harness: str | None = Field(
        default=None,
        description="Optional harness override recorded in the experiment metadata (e.g. "
                    "'inference-perf', 'vllm-benchmark'). Match the swept keys to the harness/"
                    "workload. Usually set on the scenario instead; omit if so.",
    )
    profile: str | None = Field(
        default=None, description="Optional workload-profile override recorded in the metadata.",
    )
    description: str | None = Field(
        default=None, description="Optional human description recorded in the experiment metadata.",
    )
    target_filename: str | None = Field(
        default=None,
        description="Optional bare *.yaml filename to write into the session workspace "
                    "(no path separators). Defaults to '<name>.yaml'.",
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
