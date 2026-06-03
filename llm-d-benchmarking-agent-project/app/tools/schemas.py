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
                    "decode.*/prefill.* (schedulerName, priorityClassName, ...). The knobs are "
                    "SHAPE-validated against the repo's own scenario examples (read live). WHICH "
                    "knobs to set is JUDGMENT — call read_knowledge('vllm_overrides') first; the "
                    "repos stay read-only (authored into the session workspace). Preview the "
                    "authored file via execute_llmdbenchmark(subcommand='plan'/'run', "
                    "flags={'dry_run': True}).",
    )


class ExecuteInput(BaseModel):
    subcommand: Literal["plan", "standup", "smoketest", "run", "teardown", "results", "experiment"]
    spec: str | None = Field(default=None, description="Spec name from the catalog, e.g. 'cicd/kind'")
    namespace: str | None = None
    harness: str | None = Field(default=None, description="run/experiment only")
    workload: str | None = Field(default=None, description="run/experiment only")
    models: str | None = Field(
        default=None,
        description="Phase 28 — a single model id (a HuggingFace id or short name, e.g. "
                    "'facebook/opt-125m', 'meta-llama/Llama-3.1-8B') to deploy/serve for THIS "
                    "standup (also valid on plan/run/experiment), OVERRIDING the spec's "
                    "scenario-default model. Emitted as `-m <id>` (upstream spells it --models on "
                    "standup/plan/experiment, --model on run; -m works on all). WHICH model is "
                    "YOUR judgment, grounded in knowledge/model_override.md — there is no enumerable "
                    "models catalog. CRITICAL: pass the SAME id to check_capacity(overrides="
                    "{'model': <id>}) FIRST so the pre-flight validates the IDENTICAL model "
                    "(HF config lookup, sizing, gated-access) you are about to deploy. Omit it to "
                    "keep the spec's default model.",
    )
    kubeconfig: str | None = Field(
        default=None,
        description="Phase 29 — path to a NON-DEFAULT kubeconfig FILE to target a remote cluster "
                    "for THIS command instead of the ambient kube context. Emitted as `-k <path>` "
                    "(upstream --kubeconfig, sourced from LLMDBENCH_KUBECONFIG). Valid on every "
                    "subcommand. It is a plain (non-secret) file path, value-pinned by the "
                    "allowlist (no `..` traversal). WHEN/WHICH cluster to target is YOUR judgment, "
                    "grounded in knowledge/preconditions.md — there is no enumerable cluster "
                    "catalog. Omit it to use the ambient context (the local Kind cluster for the "
                    "quickstart). To target a cluster by API URL + bearer TOKEN instead of a "
                    "kubeconfig file, see flags.cluster_url / flags.cluster_token below — the "
                    "TOKEN is a SECRET and travels backend-only (never argv, never shown).",
    )
    flags: dict[str, Any] | None = Field(
        default=None,
        description="Optional: {skip_smoketest, dry_run, list_endpoints, methods, output, "
                    "endpoint_url, monitoring, harness_cpu_nr, cluster_url, cluster_token, step}. "
                    "`cluster_url`/`cluster_token` (Phase 29) target a remote cluster by its "
                    "API-server URL + bearer token (an alternative to the `kubeconfig` file above). "
                    "They are carried BACKEND-ONLY as the LLMDBENCH_CLUSTER_URL / "
                    "LLMDBENCH_CLUSTER_TOKEN child-env vars — NEVER as a CLI flag/argv — so the "
                    "TOKEN never reaches the browser, a `command` event, or a log (it is scrubbed "
                    "exactly like HF_TOKEN). The cluster URL is non-secret; the token is a SECRET, "
                    "so never echo it back to the user. WHEN to target a remote cluster is "
                    "knowledge-driven (knowledge/preconditions.md). `output` is a DESTINATION "
                    "KEYWORD — 'local' (default), 'gs://bucket/...', or 's3://bucket/...' — NOT a "
                    "filesystem path; a `run` defaults to local output anchored under the session "
                    "workspace. `monitoring` activates results.observability (metrics scraping): "
                    "True => emit --monitoring (creates PodMonitor/ServiceMonitor + EPP verbosity "
                    "on standup; scrapes vLLM /metrics on run/experiment) so KV-cache hit rate / "
                    "queue depth / GPU util appear in the report; False => --no-monitoring on "
                    "STANDUP ONLY (a clean opt-out for clusters lacking the Prometheus-operator "
                    "CRDs — run/experiment have no such flag and simply skip scraping); omit to use "
                    "scenario defaults. WHEN to set it is knowledge-driven, default ON (see "
                    "knowledge/observability.md; probe prometheus_crds first to decide the opt-out). "
                    "`harness_cpu_nr` is a backend-only INT that sets the LLMDBENCH_HARNESS_CPU_NR "
                    "ENV VAR (NOT a CLI flag) on the launcher subprocess — the harness default is "
                    "16; lower it to what a small/single-node cluster (e.g. Kind) can actually "
                    "schedule so the launcher pod doesn't sit in FailedScheduling/Pending. WHEN and "
                    "to WHAT (given probe_environment's node_capacity and the harness) is judgment "
                    "in knowledge/harness_sizing.md; omit it to keep the default 16. It never "
                    "reaches the browser. `step` is a step-list STRING (comma-separated numbers "
                    "and/or N-M ranges, e.g. '5', '5-9', '3,7', '3-5,9') emitted as `-s <spec>`, "
                    "valid on standup/smoketest/run/teardown — use it to RE-RUN a single failed "
                    "step (or range) after a mid-phase failure instead of tearing down and "
                    "redoing the whole phase; omit it to run the whole phase. WHICH step to "
                    "re-run and the per-phase step numbering are judgment in "
                    "knowledge/step_select.md (read_knowledge('step_select') first); -s does NOT "
                    "change a command's mode, so re-running mutating steps stays approval-gated. "
                    "For subcommand='experiment' (a DoE sweep over a "
                    "treatments file): {experiments (path to the experiment YAML), workspace (dir "
                    "for outputs), parallelism (int), overrides ('p=v,...'), stop_on_error, "
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
