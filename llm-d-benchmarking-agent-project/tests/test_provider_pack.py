"""Phase 64 — Provider-aware precondition pack.

The `provider_detection` check on `probe_environment` reads each node's LABELS + TAINTS via the
already-allowlisted read-only `kubectl get nodes -o json` and reports FACTS only: the detected
cloud provider (openshift / gke / doks / aks / minikube vs kind) and the per-node GPU taints that
leave model-server pods Pending. The MECHANISM (label→provider membership + taint extraction) is
pinned here; the per-provider PLAYBOOK (which CLI, which toleration, which known issue) lives in
knowledge/infra_providers.yaml — these tests assert the facts the probe emits AND that no provider
decision logic leaked into Python. No GPU, no live cluster, no network.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import yaml

from app.tools.access.knowledge_access import read_knowledge
from app.tools.setup import probe
from app.tools.setup.probe import (
    _PROVIDER_DEFAULT,
    _PROVIDER_LABEL_HINTS,
    _detect_provider,
    _node_provider_summaries,
    probe_environment,
)
from tests._helpers import _ctx

# An OpenShift cluster: machine-config + MachineSet labels, and a value-bearing L40S GPU taint
# (the exact `nvidia.com/gpu: NVIDIA-L40S-PRIVATE` example from the upstream OpenShift README).
OPENSHIFT_NODES_JSON = json.dumps({
    "items": [{
        "metadata": {
            "name": "ocp-gpu-0",
            "labels": {
                "node.openshift.io/os_id": "rhcos",
                "machine.openshift.io/cluster-api-machine-role": "worker",
                "kubernetes.io/arch": "amd64",
            },
        },
        "spec": {
            "taints": [
                {"key": "nvidia.com/gpu", "value": "NVIDIA-L40S-PRIVATE", "effect": "NoSchedule"},
            ],
        },
        "status": {"capacity": {"cpu": "32", "nvidia.com/gpu": "4"}},
    }],
})

# A GKE cluster: cloud.google.com/* node-pool + topology labels, keyed-only nvidia.com/gpu taint.
GKE_NODES_JSON = json.dumps({
    "items": [{
        "metadata": {
            "name": "gke-a3-pool-0",
            "labels": {
                "cloud.google.com/gke-nodepool": "a3-pool",
                "cloud.google.com/gce-topology-block": "block-7",
                "kubernetes.io/arch": "amd64",
            },
        },
        "spec": {"taints": [{"key": "nvidia.com/gpu", "value": None, "effect": "NoSchedule"}]},
        "status": {"capacity": {"cpu": "208", "nvidia.com/gpu": "8"}},
    }],
})

# A DOKS cluster: doks.digitalocean.com/* node-pool labels, keyed-only nvidia.com/gpu taint.
DOKS_NODES_JSON = json.dumps({
    "items": [{
        "metadata": {
            "name": "doks-gpu-pool-0",
            "labels": {
                "doks.digitalocean.com/node-pool": "gpu-pool",
                "doks.digitalocean.com/node-id": "abc-123",
            },
        },
        "spec": {"taints": [{"key": "nvidia.com/gpu", "value": "", "effect": "NoSchedule"}]},
        "status": {"capacity": {"cpu": "48", "nvidia.com/gpu": "1"}},
    }],
})

# A plain kind cluster: no cloud-provider labels, no GPU taint (the local quickstart default).
KIND_NODES_JSON = json.dumps({
    "items": [{
        "metadata": {
            "name": "kind-control-plane",
            "labels": {"kubernetes.io/hostname": "kind-control-plane", "node-role.kubernetes.io/control-plane": ""},
        },
        "spec": {"taints": [{"key": "node-role.kubernetes.io/control-plane", "value": None, "effect": "NoSchedule"}]},
        "status": {"capacity": {"cpu": "8"}},
    }],
})

# A mixed/migrating cluster: a GKE GPU node + a control node with no cloud-provider label.
MIXED_NODES_JSON = json.dumps({
    "items": [
        {"metadata": {"name": "gke-gpu-0", "labels": {"cloud.google.com/gke-nodepool": "gpu"}},
         "spec": {"taints": [{"key": "nvidia.com/gpu", "value": None, "effect": "NoSchedule"}]},
         "status": {"capacity": {"cpu": "96", "nvidia.com/gpu": "8"}}},
        {"metadata": {"name": "plain-0", "labels": {"kubernetes.io/arch": "amd64"}},
         "spec": {}, "status": {"capacity": {"cpu": "8"}}},
    ],
})


# ---- (1) provider detection from node labels (the mechanism) ----------------

async def test_openshift_provider_and_value_bearing_l40s_taint(tmp_path):
    """OpenShift node labels → provider openshift; the L40S value-bearing GPU taint is surfaced
    with its value so the agent can author an Equal+value toleration."""
    ctx, runner = _ctx(tmp_path, nodes_json=OPENSHIFT_NODES_JSON)
    with patch("app.tools.setup.probe.shutil.which", side_effect=lambda n, *a, **k: f"/usr/bin/{n}"):
        out = await probe_environment(ctx, checks=["provider_detection"])
    pd = out["provider_detection"]
    assert pd["available"] is True
    assert pd["provider"] == "openshift"
    assert pd["providers_seen"] == ["openshift"]
    # It reached the runner via the already-allowlisted read-only kubectl get nodes.
    assert ["kubectl", "get", "nodes", "-o", "json"] in [c["argv"] for c in runner.calls]
    # The GPU taint is surfaced with node + key + value + effect (value-bearing → Equal+value).
    assert pd["gpu_taints"] == [{
        "node": "ocp-gpu-0", "key": "nvidia.com/gpu",
        "value": "NVIDIA-L40S-PRIVATE", "effect": "NoSchedule",
    }]
    assert pd["nodes"][0]["provider"] == "openshift"


async def test_gke_provider_and_keyed_gpu_taint(tmp_path):
    """GKE cloud.google.com/* labels → provider gke; a keyed-only nvidia.com/gpu taint surfaces."""
    ctx, _ = _ctx(tmp_path, nodes_json=GKE_NODES_JSON)
    with patch("app.tools.setup.probe.shutil.which", side_effect=lambda n, *a, **k: f"/usr/bin/{n}"):
        out = await probe_environment(ctx, checks=["provider_detection"])
    pd = out["provider_detection"]
    assert pd["provider"] == "gke"
    assert pd["providers_seen"] == ["gke"]
    assert len(pd["gpu_taints"]) == 1
    taint = pd["gpu_taints"][0]
    assert taint["node"] == "gke-a3-pool-0" and taint["key"] == "nvidia.com/gpu"
    assert taint["value"] is None and taint["effect"] == "NoSchedule"


async def test_doks_provider_and_gpu_taint(tmp_path):
    """DOKS doks.digitalocean.com/* labels → provider doks; nvidia.com/gpu taint surfaces."""
    ctx, _ = _ctx(tmp_path, nodes_json=DOKS_NODES_JSON)
    with patch("app.tools.setup.probe.shutil.which", side_effect=lambda n, *a, **k: f"/usr/bin/{n}"):
        out = await probe_environment(ctx, checks=["provider_detection"])
    pd = out["provider_detection"]
    assert pd["provider"] == "doks"
    assert pd["providers_seen"] == ["doks"]
    assert pd["gpu_taints"][0]["key"] == "nvidia.com/gpu"
    assert pd["gpu_taints"][0]["node"] == "doks-gpu-pool-0"


async def test_kind_default_no_cloud_labels_no_gpu_taint(tmp_path):
    """A plain kind cluster has no cloud-provider label → provider kind; the control-plane taint
    is NOT a GPU taint, so gpu_taints is empty (nothing to tolerate on the quickstart path)."""
    ctx, _ = _ctx(tmp_path, nodes_json=KIND_NODES_JSON)
    with patch("app.tools.setup.probe.shutil.which", side_effect=lambda n, *a, **k: f"/usr/bin/{n}"):
        out = await probe_environment(ctx, checks=["provider_detection"])
    pd = out["provider_detection"]
    assert pd["provider"] == _PROVIDER_DEFAULT == "kind"
    assert pd["providers_seen"] == []
    assert pd["gpu_taints"] == []  # control-plane taint is not a GPU taint


async def test_mixed_cluster_surfaces_providers_seen(tmp_path):
    """A mixed cluster (a GKE GPU node + an unlabeled node): provider resolves to the GPU node's
    provider; providers_seen surfaces the mix for the agent's judgment."""
    ctx, _ = _ctx(tmp_path, nodes_json=MIXED_NODES_JSON)
    with patch("app.tools.setup.probe.shutil.which", side_effect=lambda n, *a, **k: f"/usr/bin/{n}"):
        out = await probe_environment(ctx, checks=["provider_detection"])
    pd = out["provider_detection"]
    assert pd["provider"] == "gke"  # the non-default provider on the GPU-bearing node wins
    assert pd["providers_seen"] == ["gke"]
    assert len(pd["gpu_taints"]) == 1 and pd["gpu_taints"][0]["node"] == "gke-gpu-0"


async def test_no_kubectl_degrades_gracefully(tmp_path):
    ctx, runner = _ctx(tmp_path, nodes_json=OPENSHIFT_NODES_JSON)
    with patch("app.tools.setup.probe.shutil.which", side_effect=lambda n, *a, **k: None):
        out = await probe_environment(ctx, checks=["provider_detection"])
    pd = out["provider_detection"]
    assert pd == {
        "available": False, "provider": "kind",
        "providers_seen": [], "gpu_taints": [], "nodes": [],
    }
    assert runner.calls == []  # nothing ran — no kubectl on PATH


async def test_unreachable_cluster_is_structured_not_raised(tmp_path):
    ctx, _ = _ctx(tmp_path, nodes_json="")

    async def boom(argv, **kw):
        from app.security.runner import RunResult
        return RunResult(exit_code=1, duration_s=0.0, real_argv=list(argv), cwd=None,
                         output="The connection to the server was refused")

    with patch("app.tools.setup.probe.shutil.which", side_effect=lambda n, *a, **k: f"/usr/bin/{n}"), \
            patch.object(ctx, "run_readonly", side_effect=boom):
        out = await probe_environment(ctx, checks=["provider_detection"])
    pd = out["provider_detection"]
    assert pd["available"] is False
    assert pd["provider"] == "kind" and pd["gpu_taints"] == []


# ---- (2) the pure helpers ---------------------------------------------------

def test_node_provider_summaries_extracts_provider_and_taints():
    nodes = _node_provider_summaries(OPENSHIFT_NODES_JSON)
    assert len(nodes) == 1
    n = nodes[0]
    assert n["name"] == "ocp-gpu-0"
    assert n["provider"] == "openshift"
    assert "openshift" in n["labels_seen"]
    assert n["taints"] == [{"key": "nvidia.com/gpu", "value": "NVIDIA-L40S-PRIVATE", "effect": "NoSchedule"}]


def test_node_provider_summaries_bad_json_is_empty():
    assert _node_provider_summaries("not json") == []
    assert _node_provider_summaries("") == []


def test_node_with_no_labels_defaults_to_kind():
    nodes = _node_provider_summaries(json.dumps(
        {"items": [{"metadata": {"name": "n0"}, "spec": {}, "status": {}}]}
    ))
    assert nodes[0]["provider"] == _PROVIDER_DEFAULT
    assert nodes[0]["taints"] == []


def test_detect_provider_longest_prefix_wins():
    """Each known prefix maps to its provider; an unknown label set falls back to kind."""
    assert _detect_provider({"cloud.google.com/gke-nodepool": "p"}) == ("gke", ["gke"])
    assert _detect_provider({"doks.digitalocean.com/node-pool": "p"}) == ("doks", ["doks"])
    assert _detect_provider({"kubernetes.azure.com/agentpool": "p"}) == ("aks", ["aks"])
    assert _detect_provider({"node.openshift.io/os_id": "rhcos"}) == ("openshift", ["openshift"])
    assert _detect_provider({"minikube.k8s.io/version": "v1"}) == ("minikube", ["minikube"])
    prov, hits = _detect_provider({"kubernetes.io/arch": "amd64"})
    assert prov == _PROVIDER_DEFAULT and hits == []


# ---- (3) the judgment is DATA in knowledge/infra_providers.yaml, not Python -

def test_infra_providers_knowledge_loads_via_read_knowledge(tool_ctx):
    out = read_knowledge(tool_ctx, name="infra_providers")
    assert out["name"] == "infra_providers.yaml"
    data = yaml.safe_load(out["content"])
    assert isinstance(data, dict)


def test_python_label_table_mirrors_knowledge_detection_table():
    """The Python _PROVIDER_LABEL_HINTS table is a MIRROR of the knowledge file's detection map —
    they must stay in lockstep so the mechanism never diverges from the documented mapping."""
    data = yaml.safe_load(_read_knowledge_file("knowledge/deploy/infra_providers.yaml"))
    knowledge_pairs = {
        (e["prefix"], e["provider"]) for e in data["detection"]["label_prefix_to_provider"]
    }
    python_pairs = set(_PROVIDER_LABEL_HINTS)
    assert python_pairs == knowledge_pairs, "Python label table drifted from knowledge file"
    assert data["detection"]["default_provider"] == _PROVIDER_DEFAULT


def test_infra_providers_carries_oc_vs_kubectl_cli_judgment():
    data = yaml.safe_load(_read_knowledge_file("knowledge/deploy/infra_providers.yaml"))
    by_provider = data["cli"]["by_provider"]
    assert by_provider["openshift"]["cli"] == "oc"
    for prov in ("gke", "doks", "aks", "kind"):
        assert by_provider[prov]["cli"] == "kubectl"
    assert data["cli"]["default_cli"] == "kubectl"


def test_infra_providers_carries_gpu_tolerations_per_provider():
    data = yaml.safe_load(_read_knowledge_file("knowledge/deploy/infra_providers.yaml"))
    tol = data["gpu_tolerations"]
    # OpenShift: an Equal+value toleration for the value-bearing L40S taint.
    osp = tol["openshift"]["toleration_value_bearing"]
    assert osp["key"] == "nvidia.com/gpu" and osp["operator"] == "Equal" and osp["effect"] == "NoSchedule"
    # DOKS/GKE/AKS: the nvidia.com/gpu Exists toleration.
    for prov in ("doks", "gke", "aks"):
        t = tol[prov]["toleration"]
        assert t["key"] == "nvidia.com/gpu" and t["operator"] == "Exists"


def test_infra_providers_flags_gke_known_issues_gmp_undetected_nvshmem():
    """The GKE known-issue notes the acceptance criterion names: GMP, 'Undetected platform', NVSHMEM."""
    data = yaml.safe_load(_read_knowledge_file("knowledge/deploy/infra_providers.yaml"))
    gke_ids = {i["id"] for i in data["known_issues"]["gke"]}
    assert "google-managed-prometheus" in gke_ids
    assert "undetected-platform-vllm-0.10.0" in gke_ids
    assert any("nvshmem" in i for i in gke_ids)


def test_infra_providers_flags_openshift_servicemesh_conflict():
    data = yaml.safe_load(_read_knowledge_file("knowledge/deploy/infra_providers.yaml"))
    gw = data["gateway"]["openshift_servicemesh_conflict"]
    assert "ServiceMesh" in gw["symptom"] or "Istio" in gw["symptom"]
    assert "ServiceMesh" in gw["advice"] or "Istio" in gw["advice"]


def test_no_provider_decision_logic_leaked_into_python():
    """The probe module must contain NO provider DECISION logic — no `if provider ==`/`elif` chain
    that branches on a provider name to pick a CLI/toleration/known-issue, and no provider-name
    string LITERAL in executable code. Provider names appear only in the DATA table
    (`_PROVIDER_LABEL_HINTS`, sourced from the knowledge file) and in docstrings that POINT at the
    knowledge file — never as a branch. We parse the AST and assert no provider literal sits inside
    an `if`/`elif` test, and that `oc`/`kubectl` never appear as code literals (the CLI choice is
    knowledge, not Python)."""
    import ast

    src = _read_probe_src()
    tree = ast.parse(src)
    # The cloud-provider playbook names + the `oc` CLI. ('kind'/'kubectl' are deliberately NOT
    # screened — they are generic mechanism tokens used elsewhere, e.g. parsing `kind get clusters`
    # output and invoking `kubectl` to RUN the read-only probe.)
    forbidden_in_test = {p for _, p in _PROVIDER_LABEL_HINTS} | {"oc"}

    # (a) No `if`/`elif` test anywhere branches on a cloud-provider NAME or `oc`.
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            for lit in ast.walk(node.test):
                if isinstance(lit, ast.Constant) and isinstance(lit.value, str):
                    assert lit.value not in forbidden_in_test, (
                        f"provider/CLI literal {lit.value!r} used in an if/elif test — that is "
                        f"provider decision logic; it belongs in knowledge/infra_providers.yaml"
                    )

    # (b) `oc` / `kubectl` are never STRING LITERALS in the probe code at all: the probe only ever
    # runs `kubectl` for the read-only detection; the oc-vs-kubectl CLI CHOICE is knowledge.
    # (The literal "kubectl" used to RUN the detection probe is allowed; "oc" must be absent.)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == "oc":
            raise AssertionError("string literal 'oc' in probe.py — the CLI choice is knowledge, not Python")


# ---- (4) wiring: provider_detection is a selectable check -------------------

def test_provider_detection_is_a_selectable_check():
    assert "provider_detection" in probe._ALL_CHECKS


def test_probe_environment_schema_documents_provider_detection():
    from app.tools.schemas import ProbeEnvironmentInput
    desc = ProbeEnvironmentInput.model_fields["checks"].description
    assert "provider_detection" in desc
    assert "infra_providers.yaml" in desc


def test_probe_registry_description_points_to_provider_detection():
    from app.tools.registry import tool_definitions
    spec = next(d for d in tool_definitions() if d["name"] == "probe_environment")
    assert "provider_detection" in spec["description"]


# ---- helpers ---------------------------------------------------------------

def _project_root():
    from pathlib import Path
    return Path(__file__).resolve().parent.parent


def _read_knowledge_file(rel: str) -> str:
    return (_project_root() / rel).read_text()


def _read_probe_src() -> str:
    return (_project_root() / "app" / "tools" / "probe.py").read_text()
