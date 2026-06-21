"""Pydantic input models for the environment-probe / catalog / endpoint tools."""
from __future__ import annotations

from typing import Literal

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
