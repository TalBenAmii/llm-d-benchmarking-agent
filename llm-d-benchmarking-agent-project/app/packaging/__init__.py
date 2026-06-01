"""Phase 8 — packaging contract.

Mechanism only: this package does NOT decide *whether* or *how* to deploy (that judgment
lives in ``knowledge/packaging.md`` and the LLM). It exposes the small, factual contract the
deploy artifacts (Dockerfile, Helm chart, Kustomize base) must agree on — the container
port, the health and scrape paths, and the exact least-privilege Kubernetes RBAC the
orchestrator needs — so the artifacts and the running app can be checked for consistency in
hermetic tests instead of drifting silently.
"""
from app.packaging.assets import (
    AGENT_CONTAINER_PORT,
    AGENT_HEALTH_PATH,
    AGENT_METRICS_PATH,
    ORCHESTRATOR_RBAC_RULES,
    deploy_dir,
    helm_chart_dir,
    kustomize_base_dir,
    required_rbac_rules,
)

__all__ = [
    "AGENT_CONTAINER_PORT",
    "AGENT_HEALTH_PATH",
    "AGENT_METRICS_PATH",
    "ORCHESTRATOR_RBAC_RULES",
    "deploy_dir",
    "helm_chart_dir",
    "kustomize_base_dir",
    "required_rbac_rules",
]
