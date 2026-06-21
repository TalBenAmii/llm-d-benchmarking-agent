"""Read-only environment & catalog probes: sense the host/cluster preconditions in one
structured snapshot and enumerate the on-disk catalog. None of these mutate anything, so the
agent loop runs them automatically (no approval).

The knowledge / repo-doc access tools and the Benchmark-Report locating tools used to live in
this module too; they now have their own cohesive homes (app/tools/knowledge_access.py and
app/tools/report_locate.py). This module re-exports them below so existing
``from app.tools.probe import <tool>`` imports keep working.
"""
from __future__ import annotations

import shutil
from typing import Any

import yaml

from app.security.runner import RunResult
from app.tools.context import ToolContext, ToolError

# Re-exports for backwards compat: these tools moved to sibling modules (this file had grown to
# ~1,100 lines spanning three unrelated tool families); keep the old import paths working.
from app.tools.knowledge_access import (  # noqa: F401
    fetch_key_docs,
    read_knowledge,
    read_repo_doc,
    search_knowledge,
)

# Pure JSON/text parsers + the constants they own live in the sibling module; import them back so
# the `_probe_*` orchestration functions below are unchanged in behavior, and so existing
# `from app.tools.probe import <parser>` imports (tests + callers) keep resolving. Import direction
# is one-way (probe -> probe_parse); probe_parse never imports from this module. `_ACCELERATOR_
# RESOURCE_KEYS` and `_PROVIDER_DEFAULT` are defined there but also used by staying probes below,
# so they're imported (single source of truth).
from app.tools.probe_parse import (  # noqa: F401
    _ACCELERATOR_RESOURCE_KEYS,
    _PROVIDER_DEFAULT,
    _PROVIDER_LABEL_HINTS,
    _as_str,
    _collect_image_tags,
    _detect_cluster_provider,
    _detect_provider,
    _items_from_json,
    _names_from_json,
    _node_accelerator_summaries,
    _node_cpu_summaries,
    _node_provider_summaries,
    _parse_cpu_quantity,
    _pod_summaries,
    _server_version,
)
from app.tools.report_locate import _discover_charts, locate_and_parse_report  # noqa: F401

# Tools the quickstart depends on; presence is checked via PATH (no command run).
_TOOLCHAIN = ["docker", "podman", "kubectl", "kind", "helm", "helmfile", "jq", "yq", "git", "uv", "python3"]

_ALL_CHECKS = [
    "container_runtime", "repos", "tools", "venv",
    "kind_clusters", "kube_context", "cluster_info", "namespaces", "stack",
    "prometheus_crds",
    "metrics_server",
    "grafana_dashboard",
    "node_capacity",
    "cluster_preconditions",
    "provider_detection",
]

# The Prometheus-operator CRDs the benchmark's --monitoring path needs (PodMonitor +
# ServiceMonitor). Both must exist for monitoring resources to apply cleanly; a Kind / vanilla
# cluster without the operator lacks them. Pure DATA used by the read-only probe below — the
# --monitoring vs --no-monitoring DECISION is the agent's (knowledge/observability.md).
_PROMETHEUS_CRDS = ("podmonitors.monitoring.coreos.com", "servicemonitors.monitoring.coreos.com")

# Taint keys whose presence names a GPU node. A model-server pod stays Pending against such a
# tainted node until a MATCHING toleration is authored — the per-provider toleration to author is
# JUDGMENT in knowledge/infra_providers.yaml. We reuse the accelerator resource keys (a GPU taint
# is conventionally keyed by the same extended-resource name, e.g. nvidia.com/gpu) imported from
# probe_parse, where `_ACCELERATOR_RESOURCE_KEYS` is defined.
_GPU_TAINT_KEYS = frozenset(_ACCELERATOR_RESOURCE_KEYS)


async def probe_environment(
    ctx: ToolContext,
    *,
    checks: list[str] | str = "all",
    namespace: str | None = None,
    spec: str | None = None,
) -> dict[str, Any]:
    """Gather precondition signals in one structured snapshot."""
    wanted = _ALL_CHECKS if (checks == "all" or not checks) else [c for c in checks if c in _ALL_CHECKS]
    out: dict[str, Any] = {}

    if "container_runtime" in wanted:
        out["container_runtime"] = await _probe_container_runtime(ctx)
    if "repos" in wanted:
        out["repos"] = _probe_repos(ctx)
    if "tools" in wanted:
        out["tools"] = {t: bool(shutil.which(t)) for t in _TOOLCHAIN}
    if "venv" in wanted:
        out["venv"] = _probe_venv(ctx)
    if "kind_clusters" in wanted:
        out["kind_clusters"] = await _probe_kind(ctx)
    if "kube_context" in wanted:
        out["kube_context"] = await _probe_kube_context(ctx)
    if "cluster_info" in wanted:
        out["cluster_info"] = await _probe_cluster_info(ctx)
    if "namespaces" in wanted:
        out["namespaces"] = await _probe_namespaces(ctx)
    if "stack" in wanted:
        out["stack"] = await _probe_stack(ctx, namespace)
    if "prometheus_crds" in wanted:
        out["prometheus_crds"] = await _probe_prometheus_crds(ctx)
    if "metrics_server" in wanted:
        out["metrics_server"] = await _probe_metrics_server(ctx)
    if "grafana_dashboard" in wanted:
        out["grafana_dashboard"] = _probe_grafana_dashboard(ctx)
    # node_capacity + provider_detection BOTH parse `kubectl get nodes -o json`; fetch it ONCE
    # here and hand the SAME RunResult to both probes so a single probe_environment call runs the
    # node list query just once. The probes still accept the pre-fetched result OPTIONALLY (None =>
    # fetch themselves), so each remains correct when called standalone; the kubectl-missing /
    # non-zero-exit degraded defaults are unchanged. Pure mechanism — no behavior change.
    nodes_res = None
    if shutil.which("kubectl") and ({"node_capacity", "provider_detection"} & set(wanted)):
        nodes_res = await ctx.run_readonly(["kubectl", "get", "nodes", "-o", "json"], timeout=12.0)
    if "node_capacity" in wanted:
        out["node_capacity"] = await _probe_node_capacity(ctx, nodes_res=nodes_res)
    if "cluster_preconditions" in wanted:
        out["cluster_preconditions"] = await _probe_cluster_preconditions(ctx, spec)
    if "provider_detection" in wanted:
        out["provider_detection"] = await _probe_provider_detection(ctx, nodes_res=nodes_res)
    return out


async def _probe_container_runtime(ctx: ToolContext) -> dict[str, Any]:
    for rt in ("docker", "podman"):
        if not shutil.which(rt):
            continue
        try:
            res = await ctx.run_readonly([rt, "info"], timeout=15.0)
        except ToolError as exc:
            return {"type": rt, "present": True, "daemon_up": False, "error": str(exc)}
        if res.exit_code == 0:
            return {"type": rt, "present": True, "daemon_up": True}
        socket_err = "permission denied" in res.output.lower()
        return {
            "type": rt,
            "present": True,
            "daemon_up": False,
            "socket_permission_error": socket_err,
            "error_tail": res.output[-400:],
        }
    return {"type": None, "present": False, "daemon_up": False}


def _probe_repos(ctx: ToolContext) -> dict[str, Any]:
    s = ctx.settings
    out = {}
    for name, path in s.repo_paths.items():
        out[name] = {
            "present": (path / ".git").exists() or path.is_dir(),
            "is_git": (path / ".git").exists(),
            "path": str(path),
        }
    return out


def _probe_venv(ctx: ToolContext) -> dict[str, Any]:
    venv_py = ctx.settings.bench_repo / ".venv" / "bin" / "python"
    return {"exists": venv_py.exists(), "path": str(venv_py.parent.parent)}


async def _probe_kind(ctx: ToolContext) -> dict[str, Any]:
    if not shutil.which("kind"):
        return {"available": False, "clusters": []}
    res = await ctx.run_readonly(["kind", "get", "clusters"], timeout=15.0)
    clusters = [c for c in res.output.splitlines() if c.strip() and "No kind clusters" not in c]
    return {"available": True, "clusters": clusters, "exit_code": res.exit_code}


async def _probe_kube_context(ctx: ToolContext) -> dict[str, Any]:
    if not shutil.which("kubectl"):
        return {"available": False, "context": None}
    res = await ctx.run_readonly(["kubectl", "config", "current-context"], timeout=10.0)
    ctx_name = res.output.strip() if res.exit_code == 0 else None
    return {"available": True, "context": ctx_name or None}


async def _probe_cluster_info(ctx: ToolContext) -> dict[str, Any]:
    if not shutil.which("kubectl"):
        return {"reachable": False}
    res = await ctx.run_readonly(["kubectl", "cluster-info"], timeout=12.0)
    return {"reachable": res.exit_code == 0, "timed_out": res.timed_out}


async def _probe_namespaces(ctx: ToolContext) -> dict[str, Any]:
    if not shutil.which("kubectl"):
        return {"available": False, "namespaces": []}
    res = await ctx.run_readonly(["kubectl", "get", "ns", "-o", "json"], timeout=12.0)
    names = _names_from_json(res.output) if res.exit_code == 0 else []
    return {"available": res.exit_code == 0, "namespaces": names}


async def _probe_stack(ctx: ToolContext, namespace: str | None) -> dict[str, Any]:
    """Lightweight 'is something already running here?' check for a namespace."""
    if not namespace or not shutil.which("kubectl"):
        return {"namespace": namespace, "checked": False}
    res = await ctx.run_readonly(["kubectl", "get", "pods", "-n", namespace, "-o", "json"], timeout=12.0)
    if res.exit_code != 0:
        return {"namespace": namespace, "checked": True, "exists": False, "pods": []}
    pods = _pod_summaries(res.output)
    running = [p for p in pods if p["ready"]]
    return {
        "namespace": namespace,
        "checked": True,
        "exists": True,
        "pod_count": len(pods),
        "ready_count": len(running),
        "detected": len(running) > 0,
        "pods": pods[:25],
    }


async def _probe_prometheus_crds(ctx: ToolContext) -> dict[str, Any]:
    """Detect whether the Prometheus-operator CRDs (PodMonitor + ServiceMonitor) exist.

    Read-only: lists the cluster's CRDs (``kubectl get crd -o name``) and reports which of the
    two monitoring CRDs are present. This is PURE MECHANISM — it reports facts only. WHETHER to
    pass ``--monitoring`` (default) or ``--no-monitoring`` / ``monitoring.installPrometheusCrds``
    on a cluster missing them is the agent's judgment, grounded in knowledge/observability.md.
    NB: the bundled ``cicd/kind`` scenario already sets ``monitoring.installPrometheusCrds: true``
    (the CRDs get installed during standup), so absence here is not by itself a reason to opt out
    of monitoring on the quickstart path — only on truly CRD-less clusters that won't install them.
    """
    if not shutil.which("kubectl"):
        return {"available": False, "present": False}
    res = await ctx.run_readonly(["kubectl", "get", "crd", "-o", "name"], timeout=12.0)
    if res.exit_code != 0:
        return {"available": False, "present": False, "timed_out": res.timed_out}
    # `-o name` prints one CRD per line as ``customresourcedefinition.apiextensions.k8s.io/<name>``;
    # match on the trailing CRD name so the apiVersion prefix doesn't matter.
    have = {line.split("/", 1)[-1].strip() for line in res.output.splitlines() if line.strip()}
    per_crd = {name: (name in have) for name in _PROMETHEUS_CRDS}
    return {
        "available": True,
        "podmonitors_crd": per_crd["podmonitors.monitoring.coreos.com"],
        "servicemonitors_crd": per_crd["servicemonitors.monitoring.coreos.com"],
        "present": all(per_crd.values()),
    }


async def _probe_metrics_server(ctx: ToolContext) -> dict[str, Any]:
    """Detect whether the in-cluster **metrics-server** is present and serving — the add-on that
    powers the live CPU/memory panel (``kubectl top``). kind and the ``cicd/kind`` spec do NOT
    install it, so on a fresh kind cluster live stats are unavailable until it is added (on kind,
    with ``--kubelet-insecure-tls``).

    PURE MECHANISM — facts only, never a verdict and never the install decision:
      - ``available``       ``kubectl top nodes`` exits 0 (metrics actually flowing — the SAME
                            signal the live resource poller uses during a run).
      - ``installed``       the metrics-server Deployment exists in kube-system (queried by LABEL,
                            since ``kubectl get`` permits a single positional — ``get deployment
                            metrics-server`` would be two positionals and the allowlist rejects it).
      - ``ready_replicas``  the Deployment's ``status.availableReplicas`` (0 == installed-but-
                            NotReady, the kind missing-``--kubelet-insecure-tls`` case), else None.

    WHETHER/when to OFFER the install (and the ``--kubelet-insecure-tls`` / GKE-OpenShift SKIP
    judgment) is the agent's, grounded in knowledge/observability.md — there is NO install branch
    here. Mirrors ``_probe_prometheus_crds``: never raises, the cluster is only read, and it
    degrades to all-absent when kubectl is missing / no cluster is reachable."""
    if not shutil.which("kubectl"):
        return {"available": False, "installed": False, "ready_replicas": None}
    top = await ctx.run_readonly(["kubectl", "top", "nodes"], timeout=12.0)
    dep = await ctx.run_readonly(
        ["kubectl", "get", "deployment", "-n", "kube-system",
         "-l", "k8s-app=metrics-server", "-o", "json"], timeout=12.0)
    installed = False
    ready_replicas: int | None = None
    if dep.exit_code == 0:
        items = _items_from_json(dep.output)
        if items:
            installed = True
            ready_replicas = items[0].get("status", {}).get("availableReplicas", 0) or 0
    return {
        "available": top.exit_code == 0,
        "installed": installed,
        "ready_replicas": ready_replicas,
    }


def _probe_grafana_dashboard(ctx: ToolContext) -> dict[str, Any]:
    """Report whether an external **Grafana** dashboard is wired up for the live run panel — i.e.
    whether the operator set ``GRAFANA_DASHBOARD_URL`` (``Settings.metrics_dashboard_url``). PURE
    MECHANISM: config introspection only — no cluster read, never raises. The agent uses this to
    TAILOR its pre-run observability offer: when ``configured`` it can say "your Grafana embeds in
    the run panel"; when not, it advises setting the env var. WHETHER/how to suggest Grafana (the
    richer view) vs the metrics-server alternative (the convenient one it can install) is the agent's
    judgment, grounded in knowledge/observability.md — there is NO suggestion branch here. The actual
    URL is the operator's and is surfaced to the UI by the resource poller, not echoed in this fact."""
    return {"configured": bool(ctx.settings.metrics_dashboard_url)}


async def _probe_node_capacity(ctx: ToolContext, *, nodes_res: RunResult | None = None) -> dict[str, Any]:
    """Report each node's allocatable/capacity CPU so the agent can right-size the harness
    launcher's CPU request for a small/single-node cluster (e.g. Kind). MECHANISM ONLY: it
    reports the numbers; WHETHER to lower LLMDBENCH_HARNESS_CPU_NR and to WHAT value is the
    agent's judgment, grounded in knowledge/harness_sizing.md — never a Python branch here.

    Uses the already-allowlisted read-only ``kubectl get nodes -o json``. ``nodes_res`` may carry
    a PRE-FETCHED result (so probe_environment runs the node list query once for both node probes);
    when None we fetch it ourselves so the probe still works standalone. Returns a structured,
    never-raising result; ``min_allocatable_cpu`` (the binding constraint for scheduling) is the
    minimum allocatable CPU across nodes, or None when it can't be determined."""
    if not shutil.which("kubectl"):
        return {"available": False, "nodes": [], "min_allocatable_cpu": None}
    res = nodes_res if nodes_res is not None else await ctx.run_readonly(
        ["kubectl", "get", "nodes", "-o", "json"], timeout=12.0
    )
    if res.exit_code != 0:
        return {"available": False, "nodes": [], "min_allocatable_cpu": None}
    nodes = _node_cpu_summaries(res.output)
    allocs = [n["allocatable_cpu"] for n in nodes if n["allocatable_cpu"] is not None]
    return {
        "available": True,
        "nodes": nodes,
        "min_allocatable_cpu": min(allocs) if allocs else None,
    }


async def _probe_cluster_preconditions(ctx: ToolContext, spec: str | None) -> dict[str, Any]:
    """Report the FACTS an infra precondition gate needs BEFORE a long real-cluster standup:
    the probed Kubernetes **server** major.minor (from the already-allowlisted read-only
    ``kubectl version --output json``) and the chosen spec's pinned **image tags** (vLLM / NIXL /
    UCX / NVSHMEM and any other ``{repository, tag}`` in the scenario YAML on disk).

    MECHANISM ONLY — it reports numbers, never a verdict. WHETHER the probed K8s version can run
    the sidecar-based P/D guide (>=1.29 runs, 1.33+ for full sidecar support, <=1.28 stalls in
    Init:0/1), and whether the image tags clear the tested minimums (vLLM 0.10.0+ / NIXL 0.5.0+ /
    UCX 0.19.0+ / NVSHMEM 3.3.9+), is the agent's judgment, grounded in
    knowledge/infrastructure_preconditions.yaml (+ prose in knowledge/preconditions.md) — there
    is no version-comparison ``if/elif`` here. Never raises; repos stay read-only.

    Sourced from docs/infrastructure.md: on K8s 1.27 the sidecar guide gets stuck in Init:0/1, so
    reporting the server version up front turns that opaque post-standup stall into an honest
    go/no-go."""
    server_version: dict[str, Any] | None = None
    if shutil.which("kubectl"):
        res = await ctx.run_readonly(["kubectl", "version", "--output", "json"], timeout=12.0)
        if res.exit_code == 0:
            server_version = _server_version(res.output)
    image_tags = _parse_image_tags(ctx, spec)
    return {
        "available": server_version is not None,
        "spec": spec,
        "server_version": server_version,
        "image_tags": image_tags,
    }


async def _probe_provider_detection(ctx: ToolContext, *, nodes_res: RunResult | None = None) -> dict[str, Any]:
    """Report the cloud-PROVIDER facts a provider-aware precondition pack needs: which provider
    the cluster is (openshift / gke / doks / aks / minikube vs kind) inferred from node LABELS,
    and each node's GPU TAINTS — the taints that leave a model-server pod Pending until a matching
    toleration is authored.

    MECHANISM ONLY — it reports facts, never a verdict and never a command choice. WHICH CLI to
    prefer (``oc`` on OpenShift vs ``kubectl``), WHICH toleration to author for a GPU taint, and
    WHICH known issue (GKE Google-Managed-Prometheus / "Undetected platform" / NVSHMEM) applies is
    the agent's judgment, grounded in knowledge/infra_providers.yaml — there is NO provider
    ``if/elif`` here. The label-prefix→provider mapping is a plain membership lookup against
    ``_PROVIDER_LABEL_HINTS`` (mirrored from that same knowledge file).

    Uses the already-allowlisted read-only ``kubectl get nodes -o json`` (no allowlist change).
    Never raises; the sibling repos stay read-only. Returns:
      - ``provider``        the single detected provider (the GPU-relevant one if mixed, else the
                            most common; ``kind`` when no node carries a cloud-provider label).
      - ``providers_seen``  the sorted set of every provider hint seen across nodes.
      - ``gpu_taints``      per-node ``{node, key, value, effect}`` for each taint whose key names
                            a GPU — what the agent authors a toleration against.
      - ``nodes``           per-node ``{name, provider, labels_seen, taints}`` facts.

    ``nodes_res`` may carry a PRE-FETCHED ``kubectl get nodes -o json`` result (so probe_environment
    runs that query once for both node probes); when None we fetch it ourselves so the probe still
    works standalone. The kubectl-missing / non-zero-exit degraded defaults are unchanged."""
    if not shutil.which("kubectl"):
        return {
            "available": False,
            "provider": _PROVIDER_DEFAULT,
            "providers_seen": [],
            "gpu_taints": [],
            "nodes": [],
        }
    res = nodes_res if nodes_res is not None else await ctx.run_readonly(
        ["kubectl", "get", "nodes", "-o", "json"], timeout=12.0
    )
    if res.exit_code != 0:
        return {
            "available": False,
            "provider": _PROVIDER_DEFAULT,
            "providers_seen": [],
            "gpu_taints": [],
            "nodes": [],
        }
    nodes = _node_provider_summaries(res.output)
    providers_seen = sorted({n["provider"] for n in nodes if n["provider"] != _PROVIDER_DEFAULT})
    gpu_taints: list[dict[str, Any]] = []
    for n in nodes:
        for taint in n["taints"]:
            if taint.get("key") in _GPU_TAINT_KEYS:
                gpu_taints.append({"node": n["name"], **taint})
    return {
        "available": True,
        "provider": _detect_cluster_provider(nodes),
        "providers_seen": providers_seen,
        "gpu_taints": gpu_taints,
        "nodes": nodes,
    }


async def advise_accelerators(ctx: ToolContext, *, namespace: str | None = None) -> dict[str, Any]:
    """Report each node's ADVERTISED accelerator / CPU / memory facts so the agent can answer
    "can my hardware actually run this?" BEFORE a standup — complementing check_capacity's
    GPU-memory sizing. MECHANISM ONLY: it extracts which extended-resource key each node
    advertises (nvidia.com/gpu or the amd/gaudi/tpu/xpu siblings) vs CPU-only, plus per-node
    status.capacity/allocatable cpu + memory. The can-it-run JUDGMENT — the CUDA/driver
    minimums, Device-Plugin vs DRA, the real (non-sim) CPU-only 64c/64GB-per-replica floor, and
    the Kind/CPU-sim exemption — lives in knowledge/accelerators.yaml (read_knowledge), NEVER as
    a Python branch here.

    Uses the already-allowlisted read-only ``kubectl get nodes -o json`` (no allowlist change).
    Returns a structured, never-raising result: ``any_accelerator`` is True if ANY node
    advertises a known accelerator resource, ``cpu_only`` is True if NO node does, and
    ``advertised_resources`` is the sorted union of accelerator keys seen across nodes."""
    if not shutil.which("kubectl"):
        return {
            "available": False,
            "nodes": [],
            "any_accelerator": False,
            "cpu_only": True,
            "advertised_resources": [],
        }
    res = await ctx.run_readonly(["kubectl", "get", "nodes", "-o", "json"], timeout=12.0)
    if res.exit_code != 0:
        return {
            "available": False,
            "nodes": [],
            "any_accelerator": False,
            "cpu_only": True,
            "advertised_resources": [],
        }
    nodes = _node_accelerator_summaries(res.output)
    advertised: set[str] = set()
    for n in nodes:
        advertised.update(n["accelerators"].keys())
    any_accel = bool(advertised)
    return {
        "available": True,
        "nodes": nodes,
        "any_accelerator": any_accel,
        "cpu_only": (not any_accel) if nodes else True,
        "advertised_resources": sorted(advertised),
    }


def list_catalog(ctx: ToolContext, *, kinds: list[str] | None = None, refresh: bool = True) -> dict[str, Any]:
    """Enumerate specs/harnesses/workloads/scenarios from the repo on disk."""
    cat = ctx.catalog(refresh=refresh)
    if not kinds:
        return cat
    return {k: cat.get(k) for k in kinds if k in cat} | {"present": cat["present"]}


# ---- helpers --------------------------------------------------------------
# The pure JSON/text parsers (`_names_from_json`, `_items_from_json`, `_parse_cpu_quantity`,
# `_node_cpu_summaries`, `_server_version`, `_collect_image_tags`, `_as_str`,
# `_node_accelerator_summaries`, `_detect_provider`, `_node_provider_summaries`,
# `_detect_cluster_provider`, `_pod_summaries`) now live in app/tools/probe_parse.py and are
# imported at the top. `_parse_image_tags` stays here because it is NOT pure: it reads the chosen
# spec's scenario YAML off disk via `ctx.settings.bench_repo`.

def _parse_image_tags(ctx: ToolContext, spec: str | None) -> list[dict[str, Any]]:
    """Read the chosen spec's on-disk scenario YAML and extract every pinned image tag.

    A spec name like ``cicd/kind`` resolves to ``config/scenarios/cicd/kind.yaml`` under the
    (read-only) benchmark repo. The scenarios pin images as nested ``{repository, tag}`` blocks
    (``scenario[].images.vllm``, ``standalone.image``, …); we walk the parsed YAML and collect
    EVERY mapping carrying both keys, recording the parent key name (``vllm``/``image``/…) and a
    dotted path so the agent can match the vLLM/NIXL/UCX/NVSHMEM tags against the tested minimums
    in knowledge/. PURE MECHANISM — it parses tags, never judges them. Returns ``[]`` when no spec
    is given, the file is missing, or it doesn't parse (the agent treats absent tags as
    'unknown', never as a pass)."""
    if not spec:
        return []
    rel = spec if spec.endswith((".yaml", ".yml")) else f"{spec}.yaml"
    candidates = [
        ctx.settings.bench_repo / "config" / "scenarios" / rel,
        ctx.settings.bench_repo / "config" / "scenarios" / f"{spec}.yml",
    ]
    path = next((c for c in candidates if c.is_file()), None)
    if path is None:
        return []
    # Containment: `spec` is a free-form string, so "../../../../etc/hosts" would otherwise resolve
    # to and parse an arbitrary host file into image_tags. Reject anything that escapes the
    # scenarios dir (read-only, but never read outside the benchmark repo's scenarios).
    scenarios_root = (ctx.settings.bench_repo / "config" / "scenarios").resolve()
    try:
        if not path.resolve().is_relative_to(scenarios_root):
            return []
    except (OSError, ValueError):
        return []
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError):
        return []
    tags: list[dict[str, Any]] = []
    _collect_image_tags(data, parent="", dotted="", out=tags)
    return tags
