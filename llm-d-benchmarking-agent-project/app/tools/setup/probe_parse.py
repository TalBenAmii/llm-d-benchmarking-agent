"""Pure parsing helpers for the environment probes (no ``ctx``, no I/O).

Extracted from ``app/tools/setup/probe.py`` (which had grown to ~750 lines): the ``_probe_*``
orchestration functions there call ``ctx.run_readonly`` then feed the captured stdout to these
parsers. Keeping the parsers in their own cohesive module makes them trivially unit-testable and
shrinks the probe module to its I/O-bearing surface. Each function here takes already-captured
text / parsed data and returns parsed dicts/lists/values — none of them touch ``ctx`` or the
filesystem or the network.

Import direction is one-way: ``probe.py`` imports FROM here; this module never imports from
``probe.py`` (avoids a circular import). The module-level constants that are needed by BOTH a
moved parser AND a staying ``_probe_*`` function are DEFINED here and imported back into
``probe.py`` (single source of truth).
"""
from __future__ import annotations

import json
from typing import Any

# Accelerator extended-resource keys a node may advertise under status.capacity/allocatable.
# Detecting WHICH of these a node advertises (vs CPU-only) is pure MECHANISM; the canonical
# per-vendor key list AND the can-my-hardware-run-this judgment (CUDA/driver minimums,
# Device-Plugin vs DRA, the real-CPU 64c/64GB floor, the Kind/sim exemption) live in
# knowledge/accelerators.yaml — there is NO feasibility branch in this module. These siblings
# mirror the keys already referenced in app/orchestrator/job.py + knowledge/resource_management.md.
# Defined here (used by `_node_accelerator_summaries` below) and imported by probe.py, which
# derives `_GPU_TAINT_KEYS` from it for the provider-detection probe.
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
# Default provider when no node carries a cloud-provider label (kind/local). Defined here (used by
# `_detect_provider`/`_detect_cluster_provider` below) and imported by probe.py's
# provider-detection probe for its degraded defaults.
_PROVIDER_DEFAULT = "kind"


# ---- helpers --------------------------------------------------------------

def _names_from_json(text: str) -> list[str]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    return [item.get("metadata", {}).get("name", "") for item in data.get("items", [])]


def _items_from_json(text: str) -> list[dict[str, Any]]:
    """``.items`` from a ``kubectl get … -o json`` list, defensively (returns [] on bad JSON)."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    items = data.get("items", []) if isinstance(data, dict) else []
    return [it for it in items if isinstance(it, dict)]


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
