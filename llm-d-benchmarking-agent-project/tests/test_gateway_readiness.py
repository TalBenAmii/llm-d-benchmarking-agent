"""Phase 65 — Gateway-mode readiness gate (Gateway PROGRAMMED + InferencePool Accepted/ResolvedRefs).

Hermetic, no cluster: feeds canned `kubectl get gateway,gatewayclass,inferencepool,httproute -o json`
permutations into the pure analyzer and through the allowlisted kubectl runner (CaptureRunner). It
proves the gate distinguishes "the model pods are Ready" from "traffic can actually reach them":

  * the verdict carries the PROGRAMMED + Accepted/ResolvedRefs condition FACTS + GatewayClass-exists;
  * pods-Ready-but-PROGRAMMED:False yields a not-ready verdict with a gateway-specific reason token;
  * a fully-programmed Gateway with ResolvedRefs:True yields control_plane_ready;
  * all wait-vs-standup-vs-error decisions come from knowledge/gateway_readiness.md, not Python
    (the tool only emits a `read_knowledge('gateway_readiness')` pointer);
  * security/allowlist.yaml permits exactly the four new read-only kubectl_resource values under get.
"""
from __future__ import annotations

import json

import pytest
import yaml

from app.config import Settings
from app.readiness.diagnostics import analyze_gateway
from app.readiness.probes import check_endpoint_readiness
from app.security.allowlist import MUTATING, Allowlist
from app.tools.context import ToolContext
from app.tools.registry import dispatch
from tests._helpers import kubectl_present
from tests.flows.catalog_snapshot import frozen_catalog
from tests.flows.harness import CaptureRunner

# ---------------------------------------------------------------------------
# Canned Gateway-API JSON building blocks (status conditions are the whole point)
# ---------------------------------------------------------------------------


def _gateway(programmed: bool | None, *, name: str = "llm-d-inference-gateway") -> str:
    conds = []
    if programmed is not None:
        conds.append({"type": "Programmed", "status": "True" if programmed else "False"})
    return json.dumps({"items": [{
        "metadata": {"name": name},
        "status": {"conditions": conds},
    }]})


GATEWAYCLASS_PRESENT = json.dumps({"items": [
    {"metadata": {"name": "gke-l7-regional-external-managed"},
     "status": {"conditions": [{"type": "Accepted", "status": "True"}]}},
]})
GATEWAYCLASS_ABSENT = json.dumps({"items": []})


def _inferencepool(accepted: bool | None, resolved: bool | None, *, name: str = "llm-d-pool") -> str:
    conds = []
    if accepted is not None:
        conds.append({"type": "Accepted", "status": "True" if accepted else "False"})
    if resolved is not None:
        conds.append({"type": "ResolvedRefs", "status": "True" if resolved else "False"})
    return json.dumps({"items": [{
        "metadata": {"name": name},
        "status": {"parents": [{"conditions": conds}]},
    }]})


def _httproute(accepted: bool | None, reconciled: bool | None, *, name: str = "llm-d-route") -> str:
    conds = []
    if accepted is not None:
        conds.append({"type": "Accepted", "status": "True" if accepted else "False"})
    if reconciled is not None:
        conds.append({"type": "Reconciled", "status": "True" if reconciled else "False"})
    return json.dumps({"items": [{
        "metadata": {"name": name},
        "status": {"parents": [{"conditions": conds}]},
    }]})


# A Service WITH a ready backing endpoint — i.e. the model pods ARE Ready. The crux of Phase 65 is
# that this can be true while the Gateway is still not programmed.
ENDPOINTS_READY = json.dumps({"items": [
    {"metadata": {"name": "kubernetes"}, "subsets": [{"addresses": [{"ip": "10.96.0.1"}]}]},
    {"metadata": {"name": "llm-d-inference"},
     "subsets": [{"addresses": [{"ip": "10.244.0.7"}], "notReadyAddresses": []}]},
]})


# ---------------------------------------------------------------------------
# Pure analyzer (mechanism): conditions -> facts, never a wait/standup decision
# ---------------------------------------------------------------------------


def test_analyze_fully_programmed_gateway_is_control_plane_ready():
    g = analyze_gateway(
        namespace="bench",
        gateway_json=_gateway(True),
        gatewayclass_json=GATEWAYCLASS_PRESENT,
        inferencepool_json=_inferencepool(True, True),
        httproute_json=_httproute(True, True),
    )
    assert g.programmed is True
    assert g.gatewayclass_exists is True
    assert g.inferencepools[0]["accepted"] is True
    assert g.inferencepools[0]["resolved_refs"] is True
    assert g.inferencepools_resolved is True
    assert g.control_plane_ready is True
    assert g.not_ready_reason is None
    assert g.httproutes[0]["reconciled"] is True


def test_analyze_programmed_false_is_not_ready_with_token():
    """The headline case: a Gateway exists but PROGRAMMED:False — traffic can't reach the pods."""
    g = analyze_gateway(
        namespace="bench",
        gateway_json=_gateway(False),
        gatewayclass_json=GATEWAYCLASS_PRESENT,
        inferencepool_json=_inferencepool(True, True),
        httproute_json=_httproute(True, True),
    )
    assert g.programmed is False
    assert g.control_plane_ready is False
    assert g.not_ready_reason == "gateway_not_programmed"


def test_analyze_resolvedrefs_false_is_inferencepool_unresolved():
    g = analyze_gateway(
        namespace="bench",
        gateway_json=_gateway(True),
        gatewayclass_json=GATEWAYCLASS_PRESENT,
        inferencepool_json=_inferencepool(True, False),
        httproute_json=_httproute(True, True),
    )
    assert g.programmed is True
    assert g.inferencepools[0]["resolved_refs"] is False
    assert g.inferencepools_resolved is False
    assert g.control_plane_ready is False
    assert g.not_ready_reason == "inferencepool_unresolved"


def test_analyze_missing_gatewayclass_is_reported_and_blocks():
    g = analyze_gateway(
        namespace="bench",
        gateway_json=_gateway(False),
        gatewayclass_json=GATEWAYCLASS_ABSENT,
        inferencepool_json=_inferencepool(True, True),
        httproute_json=_httproute(True, True),
    )
    assert g.gatewayclass_exists is False
    assert g.control_plane_ready is False
    # GatewayClass missing is reported BEFORE the not-programmed gap (it's the root cause).
    assert g.not_ready_reason == "gatewayclass_missing"


def test_analyze_no_gateway_at_all_degrades_gracefully():
    g = analyze_gateway(
        namespace="bench",
        gateway_json="",
        gatewayclass_json="",
        inferencepool_json="",
        httproute_json="",
    )
    assert g.programmed is None
    assert g.gatewayclass_exists is False
    assert g.inferencepools == [] and g.httproutes == []
    assert g.inferencepools_resolved is False
    assert g.control_plane_ready is False


def test_analyze_garbage_input_never_raises():
    for bad in ("not json", "null", "[]", "{}"):
        g = analyze_gateway(namespace="bench", gateway_json=bad, gatewayclass_json=bad,
                            inferencepool_json=bad, httproute_json=bad)
        assert g.control_plane_ready is False


# The acceptance permutation grid: (programmed, resolved_refs, gatewayclass_present) -> expectations.
@pytest.mark.parametrize(
    ("programmed", "resolved", "gc_present", "expect_ready", "expect_token"),
    [
        (True, True, True, True, None),
        (False, True, True, False, "gateway_not_programmed"),
        (True, False, True, False, "inferencepool_unresolved"),
        (False, False, True, False, "gateway_not_programmed"),
        (True, True, False, False, "gatewayclass_missing"),
        (False, True, False, False, "gatewayclass_missing"),
    ],
)
def test_acceptance_permutations(programmed, resolved, gc_present, expect_ready, expect_token):
    g = analyze_gateway(
        namespace="bench",
        gateway_json=_gateway(programmed),
        gatewayclass_json=GATEWAYCLASS_PRESENT if gc_present else GATEWAYCLASS_ABSENT,
        inferencepool_json=_inferencepool(True, resolved),
        httproute_json=_httproute(True, True),
    )
    assert g.control_plane_ready is expect_ready
    assert g.not_ready_reason == expect_token
    d = g.as_dict()
    assert d["control_plane_ready"] is expect_ready and d["not_ready_reason"] == expect_token


# ---------------------------------------------------------------------------
# Tool wiring (mechanism): read-only, attaches facts, points at knowledge, never decides
# ---------------------------------------------------------------------------


def _ctx(tmp_path, *, canned):
    settings = Settings(_env_file=None, repos_dir=tmp_path / "repos",
                        workspace_dir=tmp_path / "ws", orchestrator_image="")

    async def approve(kind, payload):
        return True

    runner = CaptureRunner(settings.repo_paths, canned=canned)
    ctx = ToolContext(
        settings=settings, allowlist=Allowlist.from_file(settings.allowlist_path),
        runner=runner, workspace=settings.resolved_workspace_dir / "sessions" / "s1",
        request_approval=approve,
    )
    frozen = frozen_catalog()
    ctx._catalog = frozen
    ctx.catalog = lambda *, refresh=False: frozen
    return ctx, runner


@pytest.fixture(autouse=True)
def _kubectl_present(monkeypatch):
    kubectl_present(monkeypatch, target="app.readiness.probes")


# Distinct canned keys (substring match): "get gateway -n" must NOT also catch "get gatewayclass".
def _gateway_canned(programmed, resolved, *, gc_present=True, endpoints=ENDPOINTS_READY):
    return {
        "get endpoints": endpoints,
        "get gateway -n": _gateway(programmed),
        "get gatewayclass": GATEWAYCLASS_PRESENT if gc_present else GATEWAYCLASS_ABSENT,
        "get inferencepool": _inferencepool(True, resolved),
        "get httproute": _httproute(True, True),
    }


async def test_tool_pods_ready_but_gateway_not_programmed(tmp_path):
    """The headline behavior: endpoints are READY (pods up) yet the Gateway is PROGRAMMED:False, so
    the tool surfaces the gateway-specific reason + a read_knowledge('gateway_readiness') pointer —
    and NEVER decides wait-vs-standup (that's the knowledge file's job)."""
    ctx, runner = _ctx(tmp_path, canned=_gateway_canned(False, True))
    res = await check_endpoint_readiness(ctx, namespace="bench", probe_cli_endpoints=False)

    # Endpoint readiness still says the pods are serving...
    assert res["ready"] is True and res["reason"] == "endpoints_ready"
    # ...but the gateway facts say traffic can't reach them yet.
    gw = res["gateway"]
    assert gw["programmed"] is False
    assert gw["control_plane_ready"] is False
    assert gw["not_ready_reason"] == "gateway_not_programmed"
    # The action decision is deferred to knowledge — the tool only POINTS there.
    guidance = res["gateway_readiness_guidance"]
    assert guidance["read_knowledge"] == "gateway_readiness"
    assert guidance["reason"] == "gateway_not_programmed"

    # Every gateway probe was read-only and nothing mutated.
    assert runner.calls
    for c in runner.calls:
        d = ctx.allowlist.validate(c["argv"], catalog=ctx.catalog_for_allowlist())
        assert d.mode != MUTATING, f"gateway gate ran a mutating command: {c['argv']}"


async def test_tool_fully_programmed_gateway_has_no_guidance(tmp_path):
    ctx, _runner = _ctx(tmp_path, canned=_gateway_canned(True, True))
    res = await check_endpoint_readiness(ctx, namespace="bench", probe_cli_endpoints=False)
    assert res["gateway"]["control_plane_ready"] is True
    assert res["gateway"]["not_ready_reason"] is None
    assert "gateway_readiness_guidance" not in res


async def test_tool_reads_all_four_gateway_resources_readonly(tmp_path):
    """It runs exactly the four read-only `get <res> -o json` reads; gatewayclass is cluster-scoped
    (no -n), the other three are namespaced."""
    ctx, runner = _ctx(tmp_path, canned=_gateway_canned(True, True))
    await check_endpoint_readiness(ctx, namespace="bench", probe_cli_endpoints=False)

    def _calls_for(resource):
        return [c["argv"] for c in runner.calls if c["argv"][:3] == ["kubectl", "get", resource]]

    for resource in ("gateway", "inferencepool", "httproute"):
        calls = _calls_for(resource)
        assert calls, f"no read for {resource}"
        assert calls[-1] == ["kubectl", "get", resource, "-n", "bench", "-o", "json"]
    gc_calls = _calls_for("gatewayclass")
    assert gc_calls and gc_calls[-1] == ["kubectl", "get", "gatewayclass", "-o", "json"]  # no -n


async def test_tool_check_gateway_false_skips_the_four_reads(tmp_path):
    ctx, runner = _ctx(tmp_path, canned=_gateway_canned(True, True))
    res = await check_endpoint_readiness(
        ctx, namespace="bench", probe_cli_endpoints=False, check_gateway=False
    )
    assert res["gateway"] is None
    for resource in ("gateway", "gatewayclass", "inferencepool", "httproute"):
        assert not any(c["argv"][:3] == ["kubectl", "get", resource] for c in runner.calls)


async def test_dispatch_default_enables_gateway_facts(tmp_path):
    """Through the real tool dispatch + schema, check_gateway defaults on and the gateway facts ride
    on the result."""
    ctx, _runner = _ctx(tmp_path, canned=_gateway_canned(False, True))
    res = await dispatch(ctx, "check_endpoint_readiness",
                         {"namespace": "bench", "probe_cli_endpoints": False})
    assert res["gateway"]["not_ready_reason"] == "gateway_not_programmed"
    assert res["gateway_readiness_guidance"]["read_knowledge"] == "gateway_readiness"


# ---------------------------------------------------------------------------
# Allowlist (DATA): exactly the four new read-only resources under `get`
# ---------------------------------------------------------------------------

_NEW_RESOURCES = ["gateway", "gatewayclass", "inferencepool", "httproute"]


def _allowlist():
    settings = Settings(_env_file=None)
    return Allowlist.from_file(settings.allowlist_path), settings.allowlist_path


def test_allowlist_yaml_adds_exactly_the_four_resources():
    _, path = _allowlist()
    data = yaml.safe_load(path.read_text())
    enum = data["value_constraints"]["kubectl_resource"]["enum"]
    for r in _NEW_RESOURCES:
        assert r in enum, f"{r} missing from kubectl_resource enum"
    # No accidental extra Gateway-API resources slipped in (Phase 65 = exactly these four).
    gateway_api = {"gateway", "gateways", "gatewayclass", "gatewayclasses",
                   "inferencepool", "inferencepools", "httproute", "httproutes",
                   "referencegrant", "tcproute", "grpcroute"}
    present = gateway_api & set(enum)
    assert present == set(_NEW_RESOURCES), f"unexpected Gateway-API resources: {present}"


def test_allowlist_permits_each_new_resource_as_readonly_get(tmp_path):
    allowlist, _ = _allowlist()
    catalog = frozen_catalog()
    # Namespaced read (gateway/inferencepool/httproute) and cluster-scoped read (gatewayclass).
    cases = [
        ["kubectl", "get", "gateway", "-n", "bench", "-o", "json"],
        ["kubectl", "get", "inferencepool", "-n", "bench", "-o", "json"],
        ["kubectl", "get", "httproute", "-n", "bench", "-o", "json"],
        ["kubectl", "get", "gatewayclass", "-o", "json"],
    ]
    for argv in cases:
        decision = allowlist.validate(argv, catalog=catalog)
        assert decision.mode != MUTATING, f"{argv} should be read-only"


def test_allowlist_does_not_permit_mutating_gateway_verbs():
    """The four resources are DATA-only: `kubectl delete gateway` is NOT newly permitted — delete is
    still pinned to job/jobs by positional enum, so a gateway delete is denied (allowed=False)."""
    allowlist, _ = _allowlist()
    catalog = frozen_catalog()
    decision = allowlist.validate(["kubectl", "delete", "gateway", "g", "-n", "bench"], catalog=catalog)
    assert decision.allowed is False
