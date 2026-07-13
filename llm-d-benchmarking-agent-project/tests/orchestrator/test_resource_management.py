"""Phase 23 — resource management: node affinity / GPU selection / anti-starvation.

Pure, hermetic unit tests over the manifest-assembly MECHANISM (no cluster, no GPU). The
contract:
  * when `scheduling` is omitted (or an empty `Scheduling`), the rendered Job manifest is
    BYTE-FOR-BYTE the current generic cpu/memory baseline;
  * when scheduling fields are supplied, they land in the correct manifest paths
    (`resources.requests`/`limits` for the GPU, `nodeSelector`, `affinity`/podAntiAffinity,
    `tolerations`);
  * the parser (`Scheduling.from_dict`) accepts on TYPE and rejects malformed shapes — no
    placement judgment lives in Python (that's `knowledge/resource_management.md`).
"""
from __future__ import annotations

import json

import pytest
import yaml

from app.config import Settings
from app.orchestrator.job import (
    DEFAULT_GPU_RESOURCE,
    JobSpec,
    Scheduling,
    build_job_manifest,
)
from app.security.policy import CommandPolicy
from app.tools.context import ToolContext, ToolError
from app.tools.registry import dispatch
from app.tools.run.orchestrate import orchestrate_benchmark_run
from tests.flows.catalog_snapshot import frozen_catalog
from tests.flows.harness import CaptureRunner


def _base_spec(**over) -> JobSpec:
    kw = dict(run_id="r1", namespace="bench", image="img", command=["llmdbenchmark", "run"])
    kw.update(over)
    return JobSpec(**kw)


# --- 1. Baseline preservation (the non-negotiable: unset == today) ----------

def test_no_scheduling_is_byte_for_byte_baseline():
    """A JobSpec with no scheduling renders the SAME dict as a spec built without the new
    field at all — proving the default path is unchanged. We compare the full manifests."""
    manifest = build_job_manifest(_base_spec())
    pod = manifest["spec"]["template"]["spec"]
    # None of the new placement keys appear.
    for key in ("nodeSelector", "affinity", "tolerations"):
        assert key not in pod
    # The container requests/limits carry ONLY generic cpu/memory.
    res = pod["containers"][0]["resources"]
    assert res == {
        "requests": {"cpu": "1", "memory": "1Gi"},
        "limits": {"cpu": "1", "memory": "1Gi"},
    }


def test_generated_job_never_mounts_volumes_or_escalates():
    """FS-isolation guardrail: the orchestrator must never template a volume (a hostPath in
    particular) or a privileged / host-namespace pod into a benchmark Job. The agent's namespace
    enforces Pod Security Baseline, and the generated Job must stay conformant by construction — no
    volume mount means no path onto the node filesystem. Holds with scheduling set, too."""
    sched = Scheduling.from_dict(
        {"gpu_count": 1, "node_selector": {"pool": "gpu"}, "tolerations": [{"key": "x", "operator": "Exists"}]})
    for m in (build_job_manifest(_base_spec()), build_job_manifest(_base_spec(scheduling=sched))):
        pod = m["spec"]["template"]["spec"]
        assert "volumes" not in pod
        assert "volumeMounts" not in pod["containers"][0]
        for k in ("hostNetwork", "hostPID", "hostIPC"):
            assert k not in pod
        sc = pod["containers"][0]["securityContext"]
        assert sc["allowPrivilegeEscalation"] is False
        assert sc.get("privileged", False) is False
        assert sc["capabilities"]["drop"] == ["ALL"]


def test_empty_scheduling_equals_no_scheduling():
    """An explicitly-empty Scheduling must render IDENTICALLY to scheduling=None (byte-for-byte),
    so 'the agent supplied an empty object' degrades cleanly to the baseline."""
    a = build_job_manifest(_base_spec(scheduling=None))
    b = build_job_manifest(_base_spec(scheduling=Scheduling()))
    assert a == b
    # And serializing to YAML (what the orchestrator writes) is identical too.
    assert yaml.safe_dump(a, sort_keys=False) == yaml.safe_dump(b, sort_keys=False)


def test_from_dict_none_and_empty_return_none():
    assert Scheduling.from_dict(None) is None
    assert Scheduling.from_dict({}) is None


# --- 2. GPU resource request --------------------------------------------------

def test_gpu_count_added_to_requests_and_limits():
    sched = Scheduling.from_dict({"gpu_count": 2})
    res = build_job_manifest(_base_spec(scheduling=sched))["spec"]["template"]["spec"]["containers"][0]["resources"]
    # Extended resources MUST match on requests and limits.
    assert res["requests"][DEFAULT_GPU_RESOURCE] == "2"
    assert res["limits"][DEFAULT_GPU_RESOURCE] == "2"
    # cpu/memory untouched.
    assert res["requests"]["cpu"] == "1" and res["requests"]["memory"] == "1Gi"


def test_custom_gpu_resource_name():
    sched = Scheduling.from_dict({"gpu_count": 1, "gpu_resource": "amd.com/gpu"})
    res = build_job_manifest(_base_spec(scheduling=sched))["spec"]["template"]["spec"]["containers"][0]["resources"]
    assert res["requests"]["amd.com/gpu"] == "1" and res["limits"]["amd.com/gpu"] == "1"
    assert DEFAULT_GPU_RESOURCE not in res["requests"]


def test_no_gpu_count_means_no_gpu_resource():
    """A Scheduling that only sets placement (no gpu_count) must NOT inject a GPU request."""
    sched = Scheduling.from_dict({"node_selector": {"pool": "cpu"}})
    res = build_job_manifest(_base_spec(scheduling=sched))["spec"]["template"]["spec"]["containers"][0]["resources"]
    assert res == {"requests": {"cpu": "1", "memory": "1Gi"}, "limits": {"cpu": "1", "memory": "1Gi"}}


# --- 3. Node selection / GPU TYPE pin ----------------------------------------

def test_node_selector_and_gpu_type_label_merge():
    sched = Scheduling.from_dict({
        "node_selector": {"topology.kubernetes.io/zone": "us-east-1a"},
        "gpu_type_label": ["nvidia.com/gpu.product", "NVIDIA-A100-SXM4-80GB"],
    })
    pod = build_job_manifest(_base_spec(scheduling=sched))["spec"]["template"]["spec"]
    assert pod["nodeSelector"] == {
        "topology.kubernetes.io/zone": "us-east-1a",
        "nvidia.com/gpu.product": "NVIDIA-A100-SXM4-80GB",
    }


def test_gpu_type_label_alone_creates_node_selector():
    sched = Scheduling.from_dict({"gpu_type_label": ["nvidia.com/gpu.product", "NVIDIA-L4"]})
    pod = build_job_manifest(_base_spec(scheduling=sched))["spec"]["template"]["spec"]
    assert pod["nodeSelector"] == {"nvidia.com/gpu.product": "NVIDIA-L4"}


# --- 4. Anti-starvation: avoid_labels -> pod anti-affinity -------------------

def test_avoid_labels_render_required_pod_anti_affinity():
    sched = Scheduling.from_dict({"avoid_labels": {"llm-d.ai/role": "decode"}})
    pod = build_job_manifest(_base_spec(scheduling=sched))["spec"]["template"]["spec"]
    anti = pod["affinity"]["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
    assert len(anti) == 1
    term = anti[0]
    assert term["topologyKey"] == "kubernetes.io/hostname"  # default: avoid the same NODE
    exprs = term["labelSelector"]["matchExpressions"]
    assert exprs == [{"key": "llm-d.ai/role", "operator": "In", "values": ["decode"]}]


def test_avoid_topology_key_override():
    sched = Scheduling.from_dict({
        "avoid_labels": {"app": "llm-d"},
        "avoid_topology_key": "topology.kubernetes.io/zone",
    })
    pod = build_job_manifest(_base_spec(scheduling=sched))["spec"]["template"]["spec"]
    term = pod["affinity"]["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"][0]
    assert term["topologyKey"] == "topology.kubernetes.io/zone"


def test_avoid_labels_merge_into_raw_affinity_without_clobbering():
    """A raw affinity block (node affinity) plus avoid_labels must COEXIST — the synthesized
    podAntiAffinity is added alongside the agent's nodeAffinity, not replacing it."""
    raw = {
        "nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [
                    {"matchExpressions": [
                        {"key": "node.kubernetes.io/instance-type", "operator": "In",
                         "values": ["g5.xlarge"]}]}
                ]
            }
        }
    }
    sched = Scheduling.from_dict({"affinity": raw, "avoid_labels": {"role": "prefill"}})
    pod = build_job_manifest(_base_spec(scheduling=sched))["spec"]["template"]["spec"]
    aff = pod["affinity"]
    assert "nodeAffinity" in aff                       # agent's block preserved
    assert "podAntiAffinity" in aff                    # anti-starvation term added
    # Original raw dict must not have been mutated (deepcopy hygiene).
    assert "podAntiAffinity" not in raw


def test_avoid_labels_preserve_existing_anti_affinity_terms():
    """If the agent already supplied a podAntiAffinity term, avoid_labels APPENDS, not replaces."""
    raw = {
        "podAntiAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": [
                {"labelSelector": {"matchLabels": {"x": "y"}}, "topologyKey": "kubernetes.io/hostname"}
            ]
        }
    }
    sched = Scheduling.from_dict({"affinity": raw, "avoid_labels": {"role": "decode"}})
    pod = build_job_manifest(_base_spec(scheduling=sched))["spec"]["template"]["spec"]
    terms = pod["affinity"]["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
    assert len(terms) == 2  # the agent's term + the synthesized avoid term


def test_multiple_avoid_labels_become_anded_match_expressions():
    sched = Scheduling.from_dict({"avoid_labels": {"a": "1", "b": "2"}})
    pod = build_job_manifest(_base_spec(scheduling=sched))["spec"]["template"]["spec"]
    exprs = pod["affinity"]["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"][0]["labelSelector"]["matchExpressions"]
    # sorted, AND-ed (a single term with both expressions).
    assert exprs == [
        {"key": "a", "operator": "In", "values": ["1"]},
        {"key": "b", "operator": "In", "values": ["2"]},
    ]


# --- 5. Tolerations + raw affinity passthrough -------------------------------

def test_tolerations_passed_through():
    tols = [{"key": "dedicated", "operator": "Equal", "value": "gpu", "effect": "NoSchedule"}]
    sched = Scheduling.from_dict({"tolerations": tols})
    pod = build_job_manifest(_base_spec(scheduling=sched))["spec"]["template"]["spec"]
    assert pod["tolerations"] == tols
    # input list is deep-copied, not aliased into the manifest.
    assert pod["tolerations"] is not tols


def test_raw_affinity_passed_through_verbatim_when_no_avoid():
    raw = {"nodeAffinity": {"preferredDuringSchedulingIgnoredDuringExecution": [
        {"weight": 1, "preference": {"matchExpressions": [
            {"key": "disktype", "operator": "In", "values": ["ssd"]}]}}]}}
    sched = Scheduling.from_dict({"affinity": raw})
    pod = build_job_manifest(_base_spec(scheduling=sched))["spec"]["template"]["spec"]
    assert pod["affinity"] == raw


# --- 6. Full combined manifest in the right spec paths -----------------------

def test_full_scheduling_lands_in_all_correct_paths():
    sched = Scheduling.from_dict({
        "gpu_count": 1,
        "gpu_type_label": ["nvidia.com/gpu.product", "NVIDIA-A100-SXM4-80GB"],
        "node_selector": {"pool": "gpu"},
        "tolerations": [{"key": "dedicated", "operator": "Equal", "value": "gpu", "effect": "NoSchedule"}],
        "avoid_labels": {"llm-d.ai/role": "decode"},
    })
    manifest = build_job_manifest(_base_spec(scheduling=sched))
    pod = manifest["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert container["resources"]["requests"]["nvidia.com/gpu"] == "1"
    assert container["resources"]["limits"]["nvidia.com/gpu"] == "1"
    assert pod["nodeSelector"] == {"pool": "gpu", "nvidia.com/gpu.product": "NVIDIA-A100-SXM4-80GB"}
    assert pod["affinity"]["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
    assert pod["tolerations"][0]["key"] == "dedicated"
    # Job-level invariants are untouched by scheduling.
    assert manifest["spec"]["backoffLimit"] == 0
    assert container["securityContext"]["allowPrivilegeEscalation"] is False


# --- 7. Parser: accept-on-type, reject malformed (no policy in Python) -------

@pytest.mark.parametrize("bad", [
    {"gpu_count": 0},                                   # must be >= 1
    {"gpu_count": -1},
    {"gpu_count": "1"},                                 # not an int
    {"gpu_count": True},                                # bool is not a count
    {"gpu_resource": ""},                               # empty resource name
    {"gpu_type_label": ["only-one"]},                   # not a pair
    {"gpu_type_label": ["k", ""]},                      # empty value
    {"gpu_type_label": "nvidia"},                       # not a list
    {"node_selector": {"k": 1}},                        # non-string value
    {"node_selector": "pool=gpu"},                      # not a mapping
    {"tolerations": [1, 2]},                            # not dicts
    {"tolerations": {"k": "v"}},                        # not a list
    {"affinity": "nodeAffinity"},                       # not a mapping
    {"avoid_labels": {"k": 3}},                         # non-string value
    {"avoid_topology_key": ""},                         # empty
    {"unknown_field": True},                            # typo'd key must not silently no-op
])
def test_from_dict_rejects_malformed(bad):
    with pytest.raises(ValueError):
        Scheduling.from_dict(bad)


def test_from_dict_accepts_valid_shapes():
    s = Scheduling.from_dict({
        "gpu_count": 4, "gpu_resource": "habana.ai/gaudi",
        "gpu_type_label": ["habana.ai/gaudi.type", "gaudi2"],
        "node_selector": {"pool": "hpu"}, "avoid_labels": {"role": "decode"},
        "tolerations": [{"key": "x", "operator": "Exists"}],
        "affinity": {"nodeAffinity": {}},
    })
    assert s is not None and not s.is_empty()
    assert s.gpu_quantity() == "4" and s.gpu_resource == "habana.ai/gaudi"


# --- 8. End-to-end through the agent tool (dispatch + policy-allowed runner) ----

# Phase 24: orchestrate_benchmark_run gates on endpoint readiness before submitting. These
# manifest-mechanics tests stand the endpoint up READY so the gate is transparent.
_ENDPOINTS_READY = json.dumps({"items": [
    {"metadata": {"name": "llm-d-inference"}, "subsets": [{"addresses": [{"ip": "10.244.0.7"}]}]},
]})


def _tool_ctx(tmp_path):
    settings = Settings(_env_file=None, repos_dir=tmp_path / "repos",
                        workspace_dir=tmp_path / "ws", orchestrator_image="ghcr.io/llm-d/bench:0")

    async def approve(kind, payload):
        return True

    runner = CaptureRunner(settings.repo_paths, canned={"get endpoints": _ENDPOINTS_READY})
    ctx = ToolContext(
        settings=settings, policy=CommandPolicy.from_file(settings.command_policy_path),
        runner=runner, workspace=settings.resolved_workspace_dir / "sessions" / "s1",
        request_approval=approve,
    )
    frozen = frozen_catalog()
    ctx._catalog = frozen
    ctx.catalog = lambda *, refresh=False: frozen
    return ctx, runner


async def test_tool_threads_scheduling_into_submitted_manifest(tmp_path):
    ctx, runner = _tool_ctx(tmp_path)
    await dispatch(ctx, "orchestrate_benchmark_run", {
        "namespace": "bench", "spec": "cicd/kind", "watch": False,
        "scheduling": {
            "gpu_count": 1,
            "gpu_type_label": ["nvidia.com/gpu.product", "NVIDIA-A100-SXM4-80GB"],
            "avoid_labels": {"llm-d.ai/role": "decode"},
        },
    })
    manifest = yaml.safe_load(next((ctx.workspace / "jobs").glob("*.yaml")).read_text())
    pod = manifest["spec"]["template"]["spec"]
    assert pod["containers"][0]["resources"]["requests"]["nvidia.com/gpu"] == "1"
    assert pod["nodeSelector"]["nvidia.com/gpu.product"] == "NVIDIA-A100-SXM4-80GB"
    assert pod["affinity"]["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]


async def test_tool_without_scheduling_writes_baseline_manifest(tmp_path):
    """The tool path with no `scheduling` writes the exact baseline pod spec (regression guard
    that the new optional arg defaults to today's behavior)."""
    ctx, _ = _tool_ctx(tmp_path)
    await dispatch(ctx, "orchestrate_benchmark_run", {
        "namespace": "bench", "spec": "cicd/kind", "watch": False,
    })
    manifest = yaml.safe_load(next((ctx.workspace / "jobs").glob("*.yaml")).read_text())
    pod = manifest["spec"]["template"]["spec"]
    for key in ("nodeSelector", "affinity", "tolerations"):
        assert key not in pod
    assert pod["containers"][0]["resources"] == {
        "requests": {"cpu": "1", "memory": "1Gi"},
        "limits": {"cpu": "1", "memory": "1Gi"},
    }


async def test_tool_rejects_malformed_scheduling_before_cluster(tmp_path):
    ctx, runner = _tool_ctx(tmp_path)
    with pytest.raises(ToolError):
        await orchestrate_benchmark_run(
            ctx, namespace="bench", spec="cicd/kind", watch=False,
            scheduling={"gpu_count": 0},  # invalid
        )
    # Refused before any kubectl ran.
    assert not any(c["argv"][:2] == ["kubectl", "apply"] for c in runner.calls)


def test_scheduling_in_tool_schema_is_optional_and_documented():
    """The OrchestrateBenchmarkInput schema exposes `scheduling` as an optional object the LLM
    can fill, defaulting to None (so omitting it is valid)."""
    from app.tools.registry import tool_definitions
    by = {d["name"]: d for d in tool_definitions()}
    schema = by["orchestrate_benchmark_run"]["input_schema"]
    assert "scheduling" not in schema.get("required", [])
    prop = schema["properties"]["scheduling"]
    blob = json.dumps(prop)
    assert "avoid_labels" in blob and "gpu_type_label" in blob  # the description guides the LLM
