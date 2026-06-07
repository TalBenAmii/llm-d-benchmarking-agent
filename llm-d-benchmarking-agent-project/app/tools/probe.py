"""Read-only environment & catalog probes: sense the host/cluster preconditions in one
structured snapshot and enumerate the on-disk catalog. None of these mutate anything, so the
agent loop runs them automatically (no approval).

The knowledge / repo-doc access tools and the Benchmark-Report locating tools used to live in
this module too; they now have their own cohesive homes (app/tools/knowledge_access.py and
app/tools/report_locate.py). This module re-exports them below so existing
``from app.tools.probe import <tool>`` imports keep working.
"""
from __future__ import annotations

import json
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
from app.tools.report_locate import _discover_charts, locate_and_parse_report  # noqa: F401

# Tools the quickstart depends on; presence is checked via PATH (no command run).
_TOOLCHAIN = ["docker", "podman", "kubectl", "kind", "helm", "helmfile", "jq", "yq", "git", "uv", "python3"]

_ALL_CHECKS = [
    "container_runtime", "repos", "tools", "venv",
    "kind_clusters", "kube_context", "cluster_info", "namespaces", "stack",
    "prometheus_crds",
    "node_capacity",
    "cluster_preconditions",
    "provider_detection",
]

# The Prometheus-operator CRDs the benchmark's --monitoring path needs (PodMonitor +
# ServiceMonitor). Both must exist for monitoring resources to apply cleanly; a Kind / vanilla
# cluster without the operator lacks them. Pure DATA used by the read-only probe below — the
# --monitoring vs --no-monitoring DECISION is the agent's (knowledge/observability.md).
_PROMETHEUS_CRDS = ("podmonitors.monitoring.coreos.com", "servicemonitors.monitoring.coreos.com")

# Accelerator extended-resource keys a node may advertise under status.capacity/allocatable.
# Detecting WHICH of these a node advertises (vs CPU-only) is pure MECHANISM; the canonical
# per-vendor key list AND the can-my-hardware-run-this judgment (CUDA/driver minimums,
# Device-Plugin vs DRA, the real-CPU 64c/64GB floor, the Kind/sim exemption) live in
# knowledge/accelerators.yaml — there is NO feasibility branch in this module. These siblings
# mirror the keys already referenced in app/orchestrator/job.py + knowledge/resource_management.md.
_ACCELERATOR_RESOURCE_KEYS = (
    "nvidia.com/gpu",
    "amd.com/gpu",
    "habana.ai/gaudi",
    "google.com/tpu",
    "gpu.intel.com/i915",
    "gpu.intel.com/xe",
)

# Node-label PREFIX -> cloud-provider name. Pure MECHANISM: the provider_detection probe does a
# plain longest-prefix membership lookup against this table (no decision branches). This list
# MIRRORS knowledge/infra_providers.yaml:detection.label_prefix_to_provider — the source of truth
# is the knowledge file; the agent's per-provider PLAYBOOK (which CLI, which toleration, which
# known issue) lives ENTIRELY there, NOT here. A node matching no prefix counts toward the `kind`
# default (kind/local nodes carry no cloud-provider labels). Order does not affect correctness
# (the longest matching prefix wins) but is kept stable for readability. Mirroring tests in
# tests/test_provider_pack.py assert this table stays in lockstep with the knowledge file.
_PROVIDER_LABEL_HINTS: tuple[tuple[str, str], ...] = (
    ("node.openshift.io/", "openshift"),
    ("machine.openshift.io/", "openshift"),
    ("cloud.google.com/", "gke"),
    ("doks.digitalocean.com/", "doks"),
    ("kubernetes.azure.com/", "aks"),
    ("minikube.k8s.io/", "minikube"),
)
_PROVIDER_DEFAULT = "kind"

# Taint keys whose presence names a GPU node. A model-server pod stays Pending against such a
# tainted node until a MATCHING toleration is authored — the per-provider toleration to author is
# JUDGMENT in knowledge/infra_providers.yaml. We reuse the accelerator resource keys (a GPU taint
# is conventionally keyed by the same extended-resource name, e.g. nvidia.com/gpu).
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

def _names_from_json(text: str) -> list[str]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    return [item.get("metadata", {}).get("name", "") for item in data.get("items", [])]


def _parse_cpu_quantity(value: Any) -> float | None:
    """Parse a Kubernetes CPU quantity into whole cores. K8s expresses CPU either as a bare
    number ("4", "0.5") or in millicores ("250m" == 0.25 cores). Returns None for anything
    unparseable (defensive — the agent treats absent CPU as 'unknown', never as zero)."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("m"):
            return float(text[:-1]) / 1000.0
        return float(text)
    except ValueError:
        return None


def _node_cpu_summaries(text: str) -> list[dict[str, Any]]:
    """Per-node {name, allocatable_cpu, capacity_cpu} from `kubectl get nodes -o json`.
    Allocatable is what the scheduler can actually place against (capacity minus reserved);
    it is the figure that decides whether the launcher pod's CPU request fits."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    out: list[dict[str, Any]] = []
    for item in data.get("items", []):
        status = item.get("status", {})
        out.append({
            "name": item.get("metadata", {}).get("name", ""),
            "allocatable_cpu": _parse_cpu_quantity(status.get("allocatable", {}).get("cpu")),
            "capacity_cpu": _parse_cpu_quantity(status.get("capacity", {}).get("cpu")),
        })
    return out


def _server_version(text: str) -> dict[str, Any] | None:
    """Parse ``kubectl version --output json`` into the cluster's server major.minor.

    ``serverVersion.minor`` is often suffixed with a ``+`` on managed clusters (e.g. GKE
    reports ``"29+"``); we strip it to the bare number so the agent can compare it against the
    thresholds in knowledge/. Returns ``{major, minor, git_version, raw}`` or ``None`` when the
    server version is absent/unparseable (e.g. ``--client``-only output, or no reachable
    cluster) — this is a fact extractor, never a verdict, and it never raises."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    sv = data.get("serverVersion") if isinstance(data, dict) else None
    if not isinstance(sv, dict):
        return None
    major = str(sv.get("major", "")).strip()
    minor = str(sv.get("minor", "")).strip().rstrip("+")
    if not major and not minor:
        return None
    return {
        "major": major or None,
        "minor": minor or None,
        "git_version": sv.get("gitVersion"),
        "raw": {"major": sv.get("major"), "minor": sv.get("minor")},
    }


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
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError):
        return []
    tags: list[dict[str, Any]] = []
    _collect_image_tags(data, parent="", dotted="", out=tags)
    return tags


def _collect_image_tags(node: Any, *, parent: str, dotted: str, out: list[dict[str, Any]]) -> None:
    """Recursively collect every ``{repository, tag}`` mapping from a parsed scenario tree.
    Each hit records the image's parent key (``name``), its ``repository``/``tag``, and the
    dotted ``path`` where it was found. De-dups exact repeats so list items don't double-count."""
    if isinstance(node, dict):
        if "repository" in node and "tag" in node:
            entry = {
                "name": parent,
                "repository": _as_str(node.get("repository")),
                "tag": _as_str(node.get("tag")),
                "path": dotted or parent,
            }
            if entry not in out:
                out.append(entry)
        for key, value in node.items():
            child_dotted = f"{dotted}.{key}" if dotted else str(key)
            _collect_image_tags(value, parent=str(key), dotted=child_dotted, out=out)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            child_dotted = f"{dotted}[{i}]" if dotted else f"[{i}]"
            _collect_image_tags(value, parent=parent, dotted=child_dotted, out=out)


def _as_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _node_accelerator_summaries(text: str) -> list[dict[str, Any]]:
    """Per-node advertised-resource facts from ``kubectl get nodes -o json``: cpu (in cores),
    memory (the RAW K8s quantity verbatim, e.g. '64Gi' — NOT converted; mechanism only), and any
    accelerator extended-resource keys (``_ACCELERATOR_RESOURCE_KEYS``) with their advertised
    quantity. ``accelerated`` is True if the node advertises ANY accelerator resource; ``cpu_only``
    is its negation. This is extraction ONLY — the can-it-run-this judgment is the agent's, over
    knowledge/accelerators.yaml (no feasibility threshold is applied here)."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    out: list[dict[str, Any]] = []
    for item in data.get("items", []):
        status = item.get("status", {})
        capacity = status.get("capacity", {}) or {}
        allocatable = status.get("allocatable", {}) or {}

        def _slot(block: dict[str, Any]) -> dict[str, Any]:
            slot: dict[str, Any] = {
                # CPU parsed into whole cores (reuse the CPU-quantity parser); memory kept
                # verbatim as the raw K8s string so we never lossily convert units.
                "cpu": _parse_cpu_quantity(block.get("cpu")),
                "memory": block.get("memory"),
            }
            for key in _ACCELERATOR_RESOURCE_KEYS:
                if key in block:
                    slot[key] = block[key]
            return slot

        # Accelerators are advertised under capacity; allocatable mirrors them. Surface the
        # capacity-advertised quantities (the observable "this node has these devices" fact).
        accelerators = {k: capacity[k] for k in _ACCELERATOR_RESOURCE_KEYS if k in capacity}
        out.append({
            "name": item.get("metadata", {}).get("name", ""),
            "capacity": _slot(capacity),
            "allocatable": _slot(allocatable),
            "accelerators": accelerators,
            "accelerated": bool(accelerators),
            "cpu_only": not accelerators,
        })
    return out


def _detect_provider(labels: dict[str, Any]) -> tuple[str, list[str]]:
    """Map a node's labels to a provider name by LONGEST-prefix membership against
    ``_PROVIDER_LABEL_HINTS`` (mirrored from knowledge/infra_providers.yaml). PURE MECHANISM:
    a plain dict-key prefix scan, no provider decision logic. Returns ``(provider, hits)`` where
    ``hits`` is the sorted set of provider names any label matched (a node could in theory carry
    labels from more than one prefix); ``provider`` is the one whose matched prefix is LONGEST
    (most specific), or ``_PROVIDER_DEFAULT`` (kind) when nothing matches."""
    best_prefix = ""
    best_provider = _PROVIDER_DEFAULT
    hits: set[str] = set()
    for key in labels:
        key_s = str(key)
        for prefix, provider in _PROVIDER_LABEL_HINTS:
            if key_s.startswith(prefix):
                hits.add(provider)
                if len(prefix) > len(best_prefix):
                    best_prefix = prefix
                    best_provider = provider
    return best_provider, sorted(hits)


def _node_provider_summaries(text: str) -> list[dict[str, Any]]:
    """Per-node provider facts from ``kubectl get nodes -o json``: the detected provider (from
    ``metadata.labels`` via ``_detect_provider``) and ``spec.taints`` (each as
    ``{key, value, effect}``). EXTRACTION ONLY — the which-CLI / which-toleration / which-known-
    issue judgment is the agent's, over knowledge/infra_providers.yaml (no provider branch here)."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    out: list[dict[str, Any]] = []
    for item in data.get("items", []):
        labels = item.get("metadata", {}).get("labels", {}) or {}
        provider, labels_seen = _detect_provider(labels)
        taints = []
        for taint in item.get("spec", {}).get("taints", []) or []:
            if not isinstance(taint, dict):
                continue
            taints.append({
                "key": _as_str(taint.get("key")),
                "value": _as_str(taint.get("value")),
                "effect": _as_str(taint.get("effect")),
            })
        out.append({
            "name": item.get("metadata", {}).get("name", ""),
            "provider": provider,
            "labels_seen": labels_seen,
            "taints": taints,
        })
    return out


def _detect_cluster_provider(nodes: list[dict[str, Any]]) -> str:
    """Reduce per-node providers to one cluster-level provider. MECHANISM: prefers the most
    common NON-default provider seen across nodes (the GPU/model-server nodes drive the verdict),
    falling back to ``_PROVIDER_DEFAULT`` (kind) when no node carries a cloud-provider label. The
    mixed-cluster judgment (which provider to ultimately trust) is the agent's via
    ``providers_seen`` + knowledge/infra_providers.yaml — this only picks a sensible default."""
    counts: dict[str, int] = {}
    for n in nodes:
        prov = n.get("provider", _PROVIDER_DEFAULT)
        if prov != _PROVIDER_DEFAULT:
            counts[prov] = counts.get(prov, 0) + 1
    if not counts:
        return _PROVIDER_DEFAULT
    # Most-frequent non-default provider; ties broken by name for determinism.
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def _pod_summaries(text: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    out = []
    for item in data.get("items", []):
        status = item.get("status", {})
        phase = status.get("phase")
        conds = {c.get("type"): c.get("status") for c in status.get("conditions", [])}
        out.append({
            "name": item.get("metadata", {}).get("name", ""),
            "phase": phase,
            "ready": conds.get("Ready") == "True" and phase == "Running",
        })
    return out
