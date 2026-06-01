"""The packaging contract (mechanism, no judgment).

These constants are the *single source of truth* the deploy artifacts under ``deploy/``
must agree with. Tests assert that the shipped Dockerfile / Helm chart / Kustomize base
actually expose this port, probe these paths, and grant exactly this RBAC — so the
artifacts can't silently drift from the app (``app/main.py`` ``/healthz`` + ``/metrics``,
default port 8000) or from what the orchestrator really does to the cluster.

The RBAC is derived from the kubectl verbs the orchestrator's :class:`RealKubeClient` runs
(``app/orchestrator/kube.py``): apply/get/delete Jobs, get Pods, get Pod logs — and nothing
more. This is what lets an *orchestrated* benchmark Job actually run live when the agent runs
in-cluster (the Phase-3 deferral), under a least-privilege ServiceAccount rather than a
cluster-admin token.
"""
from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# --- the app's network contract (matches app/main.py + app/config.py defaults) ----------
AGENT_CONTAINER_PORT = 8000          # uvicorn bind port inside the container
AGENT_HEALTH_PATH = "/healthz"       # LIVENESS probe target (app/main.py) — minimal, deps-free
AGENT_READY_PATH = "/readyz"         # READINESS probe target (app/main.py, Phase 16) — per-component
AGENT_METRICS_PATH = "/metrics"      # Prometheus scrape target (app/main.py)

# Distribution / artifact names (kept consistent across the image tag, chart, and SA name).
HELM_CHART_NAME = "llm-d-benchmarking-agent"

# --- least-privilege RBAC the orchestrator requires when the agent runs in-cluster -------
# Each rule = (apiGroups, resources, verbs). Namespaced (a Role, not a ClusterRole): the
# agent only ever touches benchmark Jobs and their Pods in the namespaces it deploys into.
#
#   RealKubeClient.apply()      -> create/patch Jobs            (kubectl apply)
#   RealKubeClient.delete_job() -> delete/get Jobs              (kubectl delete job)
#   RealKubeClient.list_jobs()  -> get/list/watch Jobs          (kubectl get jobs -w)
#   RealKubeClient.list_pods()  -> get/list/watch Pods          (kubectl get pods)
#   RealKubeClient.logs()       -> get Pods/log                 (kubectl logs)
ORCHESTRATOR_RBAC_RULES: tuple[dict[str, tuple[str, ...]], ...] = (
    {
        "apiGroups": ("batch",),
        "resources": ("jobs",),
        "verbs": ("create", "get", "list", "watch", "patch", "delete"),
    },
    {
        "apiGroups": ("",),
        "resources": ("pods",),
        "verbs": ("get", "list", "watch"),
    },
    {
        "apiGroups": ("",),
        "resources": ("pods/log",),
        "verbs": ("get",),
    },
)


def required_rbac_rules() -> list[dict[str, list[str]]]:
    """The RBAC rules as plain (JSON/YAML-comparable) dicts of lists — the exact shape a
    Kubernetes Role's ``rules:`` entries take."""
    return [
        {"apiGroups": list(r["apiGroups"]), "resources": list(r["resources"]), "verbs": list(r["verbs"])}
        for r in ORCHESTRATOR_RBAC_RULES
    ]


# --- locating the shipped artifacts ------------------------------------------------------
def deploy_dir() -> Path:
    """Root of the deploy assets (data, not code)."""
    return PROJECT_ROOT / "deploy"


def helm_chart_dir() -> Path:
    return deploy_dir() / "helm" / HELM_CHART_NAME


def kustomize_base_dir() -> Path:
    return deploy_dir() / "kustomize" / "base"
