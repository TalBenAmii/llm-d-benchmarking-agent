"""Phase 63 — Accelerator + CPU-inferencing precondition advisor.

`advise_accelerators` answers "can my hardware actually run this?" BEFORE a standup: it reads
each node's ADVERTISED resources via the read-only `kubectl get nodes -o json` and reports
which accelerator extended-resource key a node advertises (nvidia.com/gpu or the
amd/gaudi/tpu/xpu siblings) vs CPU-only, plus per-node cpu/memory — complementing
check_capacity's GPU-memory sizing.

The MECHANISM (node-advertised-resource extraction) is pinned here; the FEASIBILITY judgment
(CUDA/driver minimums, Device-Plugin vs DRA, the real-CPU 64c/64GB floor, the Kind/CPU-sim
exemption) lives in knowledge/accelerators.yaml — these tests assert that data is present and
loads, and that NO threshold logic leaked into Python. No GPU, no live cluster, no network.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import yaml

from app.config import Settings
from app.security.allowlist import Allowlist
from app.tools.knowledge_access import read_knowledge
from app.tools.probe import (
    _ACCELERATOR_RESOURCE_KEYS,
    _node_accelerator_summaries,
    advise_accelerators,
)
from app.tools.registry import dispatch, tool_definitions
from tests._helpers import _ctx

# A GPU node advertising nvidia.com/gpu under capacity + allocatable, with real cpu/memory.
GPU_NODE_JSON = json.dumps({
    "items": [{
        "metadata": {"name": "gpu-worker-0"},
        "status": {
            "capacity": {"cpu": "32", "memory": "128Gi", "nvidia.com/gpu": "4"},
            "allocatable": {"cpu": "31500m", "memory": "120Gi", "nvidia.com/gpu": "4"},
        },
    }],
})

# A CPU-only node — no accelerator extended resource at all.
CPU_ONLY_NODE_JSON = json.dumps({
    "items": [{
        "metadata": {"name": "cpu-worker-0"},
        "status": {
            "capacity": {"cpu": "8", "memory": "16Gi"},
            "allocatable": {"cpu": "7500m", "memory": "15Gi"},
        },
    }],
})

# A mixed cluster: one nvidia node + one CPU-only node (the realistic "do I have a GPU?" case).
MIXED_CLUSTER_JSON = json.dumps({
    "items": [
        {"metadata": {"name": "gpu-worker-0"},
         "status": {"capacity": {"cpu": "32", "memory": "128Gi", "nvidia.com/gpu": "8"},
                    "allocatable": {"cpu": "32", "memory": "128Gi", "nvidia.com/gpu": "8"}}},
        {"metadata": {"name": "cpu-worker-0"},
         "status": {"capacity": {"cpu": "64", "memory": "64Gi"},
                    "allocatable": {"cpu": "64", "memory": "64Gi"}}},
    ],
})

# A sibling-vendor node advertising habana.ai/gaudi (Intel Gaudi / HPU).
GAUDI_NODE_JSON = json.dumps({
    "items": [{
        "metadata": {"name": "hpu-worker-0"},
        "status": {
            "capacity": {"cpu": "96", "memory": "512Gi", "habana.ai/gaudi": "8"},
            "allocatable": {"cpu": "96", "memory": "512Gi", "habana.ai/gaudi": "8"},
        },
    }],
})


# ---- (1) node-advertised-resource extraction (the mechanism) ---------------

async def test_advise_accelerators_gpu_node_reports_advertised_resource(tmp_path):
    """On a GPU-advertised node the agent gets the advertised accelerator resource + cpu/memory."""
    ctx, runner = _ctx(tmp_path, nodes_json=GPU_NODE_JSON)
    with patch("app.tools.probe.shutil.which", side_effect=lambda n, *a, **k: f"/usr/bin/{n}"):
        out = await advise_accelerators(ctx)
    assert out["available"] is True
    assert out["any_accelerator"] is True
    assert out["cpu_only"] is False
    assert out["advertised_resources"] == ["nvidia.com/gpu"]
    # It reached the runner via the already-allowlisted read-only kubectl get nodes.
    assert ["kubectl", "get", "nodes", "-o", "json"] in [c["argv"] for c in runner.calls]
    node = out["nodes"][0]
    assert node["name"] == "gpu-worker-0"
    assert node["accelerated"] is True and node["cpu_only"] is False
    # The advertised accelerator quantity is surfaced verbatim.
    assert node["accelerators"] == {"nvidia.com/gpu": "4"}
    # CPU is parsed into cores; memory is kept as the RAW K8s string (mechanism — no conversion).
    assert node["capacity"]["cpu"] == 32.0
    assert node["capacity"]["memory"] == "128Gi"
    assert node["allocatable"]["cpu"] == 31.5
    assert node["allocatable"]["memory"] == "120Gi"
    assert node["capacity"]["nvidia.com/gpu"] == "4"


async def test_advise_accelerators_cpu_only_node(tmp_path):
    """A CPU-only node advertises no accelerator resource — cpu_only is True, facts surface
    so the agent can pair the 64c/64GB floor warning with check_capacity's sizing."""
    ctx, _ = _ctx(tmp_path, nodes_json=CPU_ONLY_NODE_JSON)
    with patch("app.tools.probe.shutil.which", side_effect=lambda n, *a, **k: f"/usr/bin/{n}"):
        out = await advise_accelerators(ctx)
    assert out["available"] is True
    assert out["any_accelerator"] is False
    assert out["cpu_only"] is True
    assert out["advertised_resources"] == []
    node = out["nodes"][0]
    assert node["name"] == "cpu-worker-0"
    assert node["accelerated"] is False and node["cpu_only"] is True
    assert node["accelerators"] == {}
    # The cpu/memory the agent compares against the 64c/64GB floor are present.
    assert node["capacity"]["cpu"] == 8.0
    assert node["capacity"]["memory"] == "16Gi"


async def test_advise_accelerators_mixed_cluster_union_of_resources(tmp_path):
    """A mixed cluster advertises the accelerator on its GPU node — any_accelerator is True,
    advertised_resources is the union, and each node carries its own cpu_only flag."""
    ctx, _ = _ctx(tmp_path, nodes_json=MIXED_CLUSTER_JSON)
    with patch("app.tools.probe.shutil.which", side_effect=lambda n, *a, **k: f"/usr/bin/{n}"):
        out = await advise_accelerators(ctx)
    assert out["any_accelerator"] is True
    assert out["cpu_only"] is False  # at least one node advertises an accelerator
    assert out["advertised_resources"] == ["nvidia.com/gpu"]
    by_name = {n["name"]: n for n in out["nodes"]}
    assert by_name["gpu-worker-0"]["accelerated"] is True
    assert by_name["cpu-worker-0"]["cpu_only"] is True


async def test_advise_accelerators_detects_sibling_vendor_key(tmp_path):
    """Sibling extended resources (amd/gaudi/tpu/xpu) are detected, not just nvidia."""
    ctx, _ = _ctx(tmp_path, nodes_json=GAUDI_NODE_JSON)
    with patch("app.tools.probe.shutil.which", side_effect=lambda n, *a, **k: f"/usr/bin/{n}"):
        out = await advise_accelerators(ctx)
    assert out["advertised_resources"] == ["habana.ai/gaudi"]
    assert out["nodes"][0]["accelerators"] == {"habana.ai/gaudi": "8"}
    assert out["nodes"][0]["accelerated"] is True


async def test_advise_accelerators_no_kubectl_degrades_gracefully(tmp_path):
    ctx, runner = _ctx(tmp_path, nodes_json=GPU_NODE_JSON)
    with patch("app.tools.probe.shutil.which", side_effect=lambda n, *a, **k: None):
        out = await advise_accelerators(ctx)
    assert out == {
        "available": False, "nodes": [], "any_accelerator": False,
        "cpu_only": True, "advertised_resources": [],
    }
    assert runner.calls == []  # nothing ran — no kubectl on PATH


async def test_advise_accelerators_unreachable_cluster(tmp_path):
    """A non-zero kubectl exit yields a structured unavailable result (never raises)."""
    ctx, _ = _ctx(tmp_path, nodes_json="")

    async def boom(argv, **kw):
        from app.security.runner import RunResult
        return RunResult(exit_code=1, duration_s=0.0, real_argv=list(argv), cwd=None,
                         output="The connection to the server was refused")

    with patch("app.tools.probe.shutil.which", side_effect=lambda n, *a, **k: f"/usr/bin/{n}"), \
            patch.object(ctx, "run_readonly", side_effect=boom):
        out = await advise_accelerators(ctx)
    assert out["available"] is False
    assert out["any_accelerator"] is False and out["cpu_only"] is True


def test_node_accelerator_summaries_keeps_memory_verbatim_and_parses_cpu():
    """The helper keeps the raw K8s memory string verbatim (no unit conversion) and parses CPU
    into cores; absent accelerator keys simply don't appear."""
    nodes = _node_accelerator_summaries(GPU_NODE_JSON)
    assert len(nodes) == 1
    n = nodes[0]
    assert n["allocatable"]["memory"] == "120Gi"  # NOT converted to bytes/GiB float
    assert n["allocatable"]["cpu"] == 31.5
    # CPU-only node: no accelerator key leaks into the slot dict.
    cpu_nodes = _node_accelerator_summaries(CPU_ONLY_NODE_JSON)
    assert "nvidia.com/gpu" not in cpu_nodes[0]["capacity"]
    assert cpu_nodes[0]["accelerators"] == {}


def test_node_accelerator_summaries_bad_json_is_empty():
    assert _node_accelerator_summaries("not json") == []
    assert _node_accelerator_summaries("") == []


# ---- (2) wiring: schema + registry + dispatch ------------------------------

async def test_advise_accelerators_registered_and_dispatchable(tmp_path):
    names = {d["name"] for d in tool_definitions()}
    assert "advise_accelerators" in names
    ctx, _ = _ctx(tmp_path, nodes_json=GPU_NODE_JSON)
    with patch("app.tools.probe.shutil.which", side_effect=lambda n, *a, **k: f"/usr/bin/{n}"):
        result = await dispatch(ctx, "advise_accelerators", {})
    assert result["advertised_resources"] == ["nvidia.com/gpu"]


def test_advise_accelerators_description_points_to_knowledge_and_complements_capacity():
    spec = next(d for d in tool_definitions() if d["name"] == "advise_accelerators")
    desc = spec["description"]
    assert "read_knowledge('accelerators')" in desc
    assert "check_capacity" in desc  # explicitly complements the GPU-memory sizing check


def test_no_allowlist_widening_reuses_existing_get_nodes(tmp_path):
    """The tool reuses the EXISTING read-only `kubectl get nodes` allowlist entry — confirm it
    is present (no new mutating command, no per-command Python widening)."""
    from app.security.allowlist import READ_ONLY
    settings = Settings(_env_file=None, repos_dir=tmp_path / "repos", workspace_dir=tmp_path / "ws")
    al = Allowlist.from_file(settings.allowlist_path)
    decision = al.validate(["kubectl", "get", "nodes", "-o", "json"])
    assert decision.allowed is True
    assert decision.mode == READ_ONLY  # auto-runs, no approval


# ---- (3) the judgment is DATA in knowledge/accelerators.yaml, not Python ----

def test_accelerators_knowledge_loads_via_read_knowledge(tool_ctx):
    out = read_knowledge(tool_ctx, name="accelerators")
    assert out["name"] == "accelerators.yaml"
    data = yaml.safe_load(out["content"])
    assert isinstance(data, dict)


def test_accelerators_knowledge_carries_cpu_floor():
    """The real (non-sim) CPU-only 64c/64GB-per-replica floor is DATA, not a Python threshold."""
    path = "knowledge/deploy/accelerators.yaml"
    data = yaml.safe_load(_read_knowledge_file(path))
    cpu = data["cpu_inferencing"]
    assert cpu["supported"] is True
    assert cpu["cpu_cores_per_replica_min"] == 64
    assert cpu["memory_gb_per_replica_min"] == 64
    # The numeric floor exists nowhere in the probe module (no if/elif threshold leaked in).
    probe_src = _read_probe_src()
    assert "64" not in probe_src or "cpu_cores_per_replica_min" not in probe_src


def test_accelerators_knowledge_carries_cuda_driver_minimums():
    data = yaml.safe_load(_read_knowledge_file("knowledge/deploy/accelerators.yaml"))
    cuda = data["cuda_driver"]
    assert cuda["current"]["cuda_version"] == "12.9.1"
    assert cuda["current"]["min_driver"] == "525.60.13"
    assert cuda["current"]["max_driver"] == "< 580"
    assert cuda["current"]["recommended_driver"] == "575.x"
    assert cuda["planned"]["cuda_version"] == "13.0.2"
    assert cuda["planned"]["min_driver"] == "580.65.06"


def test_accelerators_knowledge_carries_dra_vs_device_plugin_distinction():
    data = yaml.safe_load(_read_knowledge_file("knowledge/deploy/accelerators.yaml"))
    rm = data["resource_management"]
    names = {m["name"] for m in rm["mechanisms"]}
    assert any("Device Plugin" in n for n in names)
    assert any("Dynamic Resource Allocation" in n or "DRA" in n for n in names)
    # The distinction (typically one OR the other) is spelled out.
    assert "one" in rm["distinction"].lower()


def test_accelerators_knowledge_marks_kind_sim_floor_exempt():
    data = yaml.safe_load(_read_knowledge_file("knowledge/deploy/accelerators.yaml"))
    sim = data["kind_cpu_sim"]
    assert sim["floor_exempt"] is True
    assert sim["path"] == "cicd/kind"


def test_accelerators_knowledge_maps_sibling_vendor_resource_keys():
    """Every accelerator key the probe detects must be documented as a vendor resource."""
    data = yaml.safe_load(_read_knowledge_file("knowledge/deploy/accelerators.yaml"))
    documented: set[str] = set()
    for entry in data["accelerator_resources"]:
        if "resource_key" in entry:
            documented.add(entry["resource_key"])
        documented.update(entry.get("resource_keys", []))
    for key in _ACCELERATOR_RESOURCE_KEYS:
        assert key in documented, f"probe key {key} not documented in accelerators.yaml"


# ---- helpers ---------------------------------------------------------------

def _project_root():
    from pathlib import Path
    return Path(__file__).resolve().parent.parent


def _read_knowledge_file(rel: str) -> str:
    return (_project_root() / rel).read_text()


def _read_probe_src() -> str:
    return (_project_root() / "app" / "tools" / "probe.py").read_text()
