"""Phase 59 — model-load readiness gate: /v1/models vs /health + stuck-pod diagnostics.

Hermetic, no cluster / no GPU / no network. Three layers are exercised:

  * the PURE analyzer (`classify_serving_readiness`) — it extracts FACTS only (pod phase /
    Ready conditions / restartCount / age / role-by-port + the verbatim /health and /v1/models
    probe outcomes) and contains NO loading-vs-broken if/elif (the judgment is in
    knowledge/readiness_probes.md);
  * the curl status parsing (`_probe_status`) — reads the HTTP code from a `curl -i` status line
    and reports connection-refused as unreachable;
  * the COMMAND POLICY — the constrained `curl` GET probe is permitted ONLY for GET on ports
    8000/8200 at /v1/models or /health against an in-namespace svc URL, and is REJECTED for any
    other verb/port/path/host.

The tool-level wiring is exercised end-to-end through dispatch with a CaptureRunner returning
canned `kubectl get pods` JSON + canned `curl -i` bodies, asserting:
  * /health 200 + /v1/models 503 + young pod + low restarts => the "still loading" FACTS surface;
  * /health refused or high restartCount => the "wedged/broken" FACTS surface;
  * both 200 (served via a ready endpoint) => the endpoint gate is serving-ready;
  * the verdict is reported (the serving-readiness block + the read_knowledge pointer) BEFORE any
    benchmark could be submitted, and nothing mutates.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.config import Settings
from app.readiness.diagnostics import (
    EndpointReadiness,
    ServingReadiness,
    analyze_endpoints,
    classify_serving_readiness,
)
from app.readiness.probes import _probe_status, check_endpoint_readiness
from app.security.policy import MUTATING, READ_ONLY, CommandPolicy
from app.security.runner import RunResult
from app.tools.context import ToolContext
from tests._helpers import kubectl_present
from tests.flows.catalog_snapshot import frozen_catalog
from tests.flows.harness import CaptureRunner

_NOW = datetime(2024, 1, 2, 4, 0, 0, tzinfo=UTC)


def _ts(*, minutes_ago: int) -> str:
    return (_NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


# A Running-but-NotReady prefill pod that is YOUNG with 0 restarts (the legitimate-load shape).
PODS_LOADING = json.dumps({"items": [{
    "metadata": {"name": "vllm-prefill-0", "creationTimestamp": _ts(minutes_ago=4)},
    "spec": {"containers": [{"name": "vllm", "ports": [{"containerPort": 8000}]}]},
    "status": {
        "phase": "Running",
        "conditions": [{"type": "Ready", "status": "False"},
                       {"type": "ContainersReady", "status": "False"}],
        "containerStatuses": [{"restartCount": 0}],
    },
}]})

# A Running-but-NotReady decode pod that is OLD and crash-looping (the wedged shape).
PODS_CRASHING = json.dumps({"items": [{
    "metadata": {"name": "vllm-decode-0", "creationTimestamp": _ts(minutes_ago=45)},
    "spec": {"containers": [{"name": "vllm", "ports": [{"containerPort": 8200}]}]},
    "status": {
        "phase": "Running",
        "conditions": [{"type": "Ready", "status": "False"}],
        "containerStatuses": [{"restartCount": 9}],
    },
}]})


# ----------------------------------------------------------------------------
# Pure analyzer: FACTS only — no loading-vs-broken decision in Python
# ----------------------------------------------------------------------------

def test_classify_still_loading_facts():
    """/health 200 + /v1/models 503, young pod, 0 restarts: the facts that mean 'still loading'."""
    sr = classify_serving_readiness(
        PODS_LOADING, namespace="bench",
        health_status=200, models_status=503, now=_NOW,
    )
    assert sr.health_status_code == 200 and sr.health_reachable is True
    assert sr.models_status_code == 503 and sr.models_reachable is True
    assert sr.max_restart_count == 0
    assert sr.youngest_age_seconds == 4 * 60
    assert sr.roles == ["prefill"]
    pod = sr.pods[0]
    assert pod["phase"] == "Running" and pod["ready_condition"] == "False"
    assert pod["role"] == "prefill"


def test_classify_wedged_facts():
    """/health refused + high restartCount + old pod: the facts that mean 'wedged/broken'."""
    sr = classify_serving_readiness(
        PODS_CRASHING, namespace="bench",
        health_status=None, health_reachable=False,
        models_status=None, models_reachable=False, now=_NOW,
    )
    assert sr.health_reachable is False and sr.health_status_code is None
    assert sr.models_reachable is False
    assert sr.max_restart_count == 9               # crash-looping
    assert sr.youngest_age_seconds == 45 * 60      # past a ~30-min startup budget
    assert sr.roles == ["decode"]


def test_classify_serving_ready_facts():
    """Both probes 200: the facts that mean serving-ready."""
    sr = classify_serving_readiness(
        PODS_LOADING, namespace="bench",
        health_status=200, models_status=200, now=_NOW,
    )
    assert sr.health_status_code == 200 and sr.models_status_code == 200


def test_classify_no_python_decision_branch():
    """Mechanism guarantee: the classifier reports signals and never emits a verdict token —
    the loading-vs-broken JUDGMENT lives in knowledge/readiness_probes.md, not here."""
    sr = classify_serving_readiness(PODS_LOADING, namespace="bench",
                                    health_status=200, models_status=503, now=_NOW)
    blob = json.dumps(sr.as_dict()).lower()
    for verdict_word in ("loading", "wedged", "broken", "stuck", "keep waiting", "stop"):
        assert verdict_word not in blob, f"analyzer leaked a verdict token: {verdict_word!r}"


def test_classify_garbage_pods_never_raises():
    for bad in ("", "not json", "null", "{}"):
        sr = classify_serving_readiness(bad, namespace="bench")
        assert isinstance(sr, ServingReadiness)
        assert sr.pods == [] and sr.max_restart_count == 0 and sr.youngest_age_seconds is None


def test_classify_survives_non_numeric_restart_count():
    """A forged/corrupt pod with a non-numeric ``restartCount`` must NOT crash the classifier —
    the docstring promises garbage pods_json degrades (never raises), and the sibling guards on
    the same line (``isinstance(cs, dict)``) / two lines below (``isinstance(cp, int)``) already
    coerce malformed input. A real ``kubectl get pods -o json`` always emits an integer count, but
    a corrupt/forged status object propagated a raw ``ValueError`` out of the readiness gate
    (same class as the orchestrator's BUG-023/029/037 ``int()`` hardening)."""
    pods = json.dumps({"items": [{
        "metadata": {"name": "vllm-prefill-0", "creationTimestamp": _ts(minutes_ago=4)},
        "spec": {"containers": [{"name": "vllm", "ports": [{"containerPort": 8000}]}]},
        "status": {
            "phase": "Running",
            "conditions": [{"type": "Ready", "status": "False"}],
            "containerStatuses": [{"restartCount": "lots"}],   # forged non-numeric count
        },
    }]})
    sr = classify_serving_readiness(pods, namespace="bench", now=_NOW)
    assert isinstance(sr, ServingReadiness)
    # The bad count coerces to 0 rather than crashing; the rest of the pod facts still classify.
    assert sr.max_restart_count == 0
    assert sr.pods and sr.pods[0]["role"] == "prefill"
    assert sr.pods[0]["restart_count"] == 0


def test_classify_real_count_still_read_among_malformed_siblings():
    """A genuine integer restartCount in one container is still read even if a SIBLING container's
    count is forged-non-numeric (the max ignores the bad one rather than aborting the whole pod)."""
    pods = json.dumps({"items": [{
        "metadata": {"name": "vllm-decode-0", "creationTimestamp": _ts(minutes_ago=4)},
        "spec": {"containers": [{"name": "vllm", "ports": [{"containerPort": 8200}]}]},
        "status": {
            "phase": "Running",
            "conditions": [{"type": "Ready", "status": "False"}],
            "containerStatuses": [{"restartCount": "x"}, {"restartCount": 7}],
        },
    }]})
    sr = classify_serving_readiness(pods, namespace="bench", now=_NOW)
    assert sr.max_restart_count == 7


def test_classify_unparseable_age_is_none():
    pods = json.dumps({"items": [{
        "metadata": {"name": "p", "creationTimestamp": "not-a-timestamp"},
        "spec": {"containers": [{"ports": [{"containerPort": 8000}]}]},
        "status": {"phase": "Running", "conditions": []},
    }]})
    sr = classify_serving_readiness(pods, namespace="bench", now=_NOW)
    assert sr.pods[0]["age_seconds"] is None and sr.youngest_age_seconds is None


def test_endpoint_readiness_carries_serving_readiness_field():
    """EndpointReadiness exposes the new serving_readiness field in as_dict() (None when unset)."""
    v = EndpointReadiness(namespace="bench", ready=False, reason="endpoints_not_ready", detail="x")
    assert v.as_dict()["serving_readiness"] is None
    v.serving_readiness = classify_serving_readiness(PODS_LOADING, namespace="bench",
                                                     health_status=200, models_status=503, now=_NOW)
    d = v.as_dict()["serving_readiness"]
    assert d is not None and d["models_status_code"] == 503


# ----------------------------------------------------------------------------
# curl -i status-line parsing (pure mechanism)
# ----------------------------------------------------------------------------

@pytest.mark.parametrize("output,expected", [
    ("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{\"object\":\"list\"}", (200, True)),
    ("HTTP/2 200\r\n\r\n{}", (200, True)),
    ("HTTP/1.1 503 Service Unavailable\r\n\r\n", (503, True)),
    ("curl: (7) Failed to connect to host port 8000: Connection refused", (None, False)),
    ("", (None, False)),
])
def test_probe_status_parsing(output, expected):
    assert _probe_status(RunResult(0, 0.0, [], None, output=output)) == expected


# ----------------------------------------------------------------------------
# CommandPolicy: the constrained curl GET probe — permitted vs rejected
# ----------------------------------------------------------------------------

@pytest.fixture
def policy() -> CommandPolicy:
    return CommandPolicy.from_file(Settings(_env_file=None).command_policy_path)


@pytest.mark.parametrize("argv", [
    ["curl", "-s", "-S", "-i", "-m", "5", "-X", "GET",
     "http://llm-d-inference.bench.svc:8000/v1/models"],
    ["curl", "-s", "-i", "-X", "GET", "http://gaie.bench.svc:8200/health"],
    ["curl", "-i", "http://svc.bench.svc:8000/health"],                       # GET is the default
    ["curl", "-i", "http://svc.bench.svc.cluster.local:8200/v1/models"],      # full cluster DNS
])
def test_policy_permits_constrained_get_probe(policy, argv):
    d = policy.validate(argv)
    assert d.allowed is True
    assert d.mode == READ_ONLY, "the probe must auto-run (read-only), never need approval"


@pytest.mark.parametrize("argv,why", [
    (["curl", "-X", "POST", "http://svc.bench.svc:8000/v1/models"], "POST verb"),
    (["curl", "--request", "DELETE", "http://svc.bench.svc:8000/health"], "DELETE verb"),
    (["curl", "-i", "-X", "GET", "http://svc.bench.svc:8000/v1/completions"], "off-enum path"),
    (["curl", "-i", "-X", "GET", "http://svc.bench.svc:8000/"], "off-enum path"),
    (["curl", "-i", "-X", "GET", "http://svc.bench.svc:9090/v1/models"], "off-enum port"),
    (["curl", "-i", "-X", "GET", "http://evil.example.com:8000/v1/models"], "non-svc host"),
    (["curl", "-i", "-X", "GET", "http://169.254.169.254:8000/v1/models"], "non-svc host (IP)"),
    (["curl", "-i", "-X", "GET", "https://svc.bench.svc:8000/v1/models"], "https scheme"),
])
def test_policy_rejects_off_policy_probe(policy, argv, why):
    d = policy.validate(argv)
    assert d.allowed is False, f"should have rejected {why}: {argv}"


def test_policy_curl_metachar_screen_blocks_injection(policy):
    """The blanket metacharacter screen still applies to the URL positional (defense in depth)."""
    d = policy.validate(["curl", "-i", "http://svc.bench.svc:8000/v1/models;rm -rf /"])
    assert d.allowed is False


# ----------------------------------------------------------------------------
# Tool wiring: dispatch the gate with canned kubectl pods + canned curl bodies
# ----------------------------------------------------------------------------

ENDPOINTS_NOT_READY = json.dumps({"items": [
    {"metadata": {"name": "kubernetes"}, "subsets": [{"addresses": [{"ip": "10.96.0.1"}]}]},
    {"metadata": {"name": "llm-d-inference"},
     "subsets": [{"notReadyAddresses": [{"ip": "10.244.0.7"}]}]},
]})

ENDPOINTS_READY = json.dumps({"items": [
    {"metadata": {"name": "kubernetes"}, "subsets": [{"addresses": [{"ip": "10.96.0.1"}]}]},
    {"metadata": {"name": "llm-d-inference"},
     "subsets": [{"addresses": [{"ip": "10.244.0.7"}], "notReadyAddresses": []}]},
]})

_HTTP_200 = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{\"object\":\"list\",\"data\":[{\"id\":\"m\"}]}"
_HTTP_503 = "HTTP/1.1 503 Service Unavailable\r\n\r\n"
_REFUSED = "curl: (7) Failed to connect to host port 8000: Connection refused"


def _ctx(tmp_path, *, canned):
    settings = Settings(_env_file=None, repos_dir=tmp_path / "repos", workspace_dir=tmp_path / "ws")
    runner = CaptureRunner(settings.repo_paths, canned=canned)
    ctx = ToolContext(
        settings=settings, policy=CommandPolicy.from_file(settings.command_policy_path),
        runner=runner, workspace=settings.resolved_workspace_dir / "sessions" / "s1",
    )
    frozen = frozen_catalog()
    ctx._catalog = frozen
    ctx.catalog = lambda *, refresh=False: frozen
    return ctx, runner


@pytest.fixture(autouse=True)
def _kubectl_present(monkeypatch):
    kubectl_present(monkeypatch, target="app.readiness.probes")


class _ProbeRunner(CaptureRunner):
    """A CaptureRunner that returns DIFFERENT canned curl bodies for /v1/models vs /health, so
    the two probe paths can be distinguished. Everything else falls back to `canned`."""

    def __init__(self, repo_paths, *, canned, models_body, health_body):
        super().__init__(repo_paths, canned=canned)
        self._models = models_body
        self._health = health_body

    async def execute(self, logical_argv, entry, *, on_line=None, timeout=None, cwd=None):
        argv = list(logical_argv)
        if argv and argv[0] == "curl":
            self.calls.append({"argv": argv, "entry": entry, "cwd": None})
            url = argv[-1]
            body = self._models if url.endswith("/v1/models") else self._health
            # A connection-refused probe is a non-zero curl exit with no HTTP status line.
            refused = body.startswith("curl:")
            return RunResult(exit_code=7 if refused else 0, duration_s=0.0,
                             real_argv=argv, cwd=None, output=body)
        return await super().execute(argv, entry, on_line=on_line, timeout=timeout, cwd=cwd)


async def test_gate_reports_still_loading_facts_before_any_benchmark(tmp_path):
    """Running-but-NotReady with /health 200 + /v1/models 503, young pod, 0 restarts: the gate
    surfaces the still-loading FACTS + the knowledge pointer, and mutates nothing."""
    settings = Settings(_env_file=None, repos_dir=tmp_path / "repos", workspace_dir=tmp_path / "ws")
    runner = _ProbeRunner(
        settings.repo_paths,
        canned={"get endpoints": ENDPOINTS_NOT_READY, "get pods": PODS_LOADING},
        models_body=_HTTP_503, health_body=_HTTP_200,
    )
    ctx = ToolContext(settings=settings, policy=CommandPolicy.from_file(settings.command_policy_path),
                      runner=runner, workspace=settings.resolved_workspace_dir / "sessions" / "s1")
    frozen = frozen_catalog()
    ctx._catalog = frozen
    ctx.catalog = lambda *, refresh=False: frozen

    res = await check_endpoint_readiness(ctx, namespace="bench", probe_cli_endpoints=False)

    assert res["ready"] is False and res["reason"] == "endpoints_not_ready"
    sr = res["serving_readiness"]
    assert sr is not None
    assert sr["health_status_code"] == 200 and sr["models_status_code"] == 503
    assert sr["max_restart_count"] == 0
    assert sr["roles"] == ["prefill"]
    # The agent is pointed at the JUDGMENT knowledge (the verdict is not made in Python).
    assert res["serving_readiness_guidance"]["read_knowledge"] == "readiness_probes"
    # It really ran the constrained GET probes (read-only) and nothing mutated.
    probe_calls = [c["argv"] for c in runner.calls if c["argv"][0] == "curl"]
    assert probe_calls, "expected the gate to run the curl /v1/models + /health probes"
    for c in runner.calls:
        d = ctx.policy.validate(c["argv"], catalog=ctx.catalog_for_policy())
        assert d.mode != MUTATING, f"the readiness gate ran a mutating command: {c['argv']}"
    assert not any(c["argv"][:2] == ["kubectl", "apply"] for c in runner.calls)
    # Both probe paths were exercised against an in-namespace svc URL on a model-server port.
    probed = {c["argv"][-1] for c in runner.calls if c["argv"][0] == "curl"}
    assert any(u.endswith("/v1/models") for u in probed)
    assert any(u.endswith("/health") for u in probed)
    assert all(".svc:" in u and (":8000" in u or ":8200" in u) for u in probed)


async def test_gate_reports_wedged_facts(tmp_path):
    """/health refused + high restartCount + old pod: the gate surfaces the wedged FACTS."""
    settings = Settings(_env_file=None, repos_dir=tmp_path / "repos", workspace_dir=tmp_path / "ws")
    runner = _ProbeRunner(
        settings.repo_paths,
        canned={"get endpoints": ENDPOINTS_NOT_READY, "get pods": PODS_CRASHING},
        models_body=_REFUSED, health_body=_REFUSED,
    )
    ctx = ToolContext(settings=settings, policy=CommandPolicy.from_file(settings.command_policy_path),
                      runner=runner, workspace=settings.resolved_workspace_dir / "sessions" / "s1")
    frozen = frozen_catalog()
    ctx._catalog = frozen
    ctx.catalog = lambda *, refresh=False: frozen

    res = await check_endpoint_readiness(ctx, namespace="bench", probe_cli_endpoints=False)
    sr = res["serving_readiness"]
    assert sr is not None
    assert sr["health_reachable"] is False and sr["models_reachable"] is False
    assert sr["max_restart_count"] == 9
    # The tool path uses real wall-clock now; the crashing pod's fixed (2024) creationTimestamp
    # is well past any failureThreshold*periodSeconds startup budget — i.e. a large positive age.
    assert sr["youngest_age_seconds"] is not None and sr["youngest_age_seconds"] > 30 * 60
    assert sr["roles"] == ["decode"]


async def test_gate_serving_ready_skips_serving_readiness(tmp_path):
    """A READY endpoint (both /v1/models + /health would be 200) is serving — the gate returns
    ready and does NOT populate serving_readiness (only the NotReady case classifies)."""
    ctx, runner = _ctx(tmp_path, canned={"get endpoints": ENDPOINTS_READY})
    res = await check_endpoint_readiness(ctx, namespace="bench", probe_cli_endpoints=False)
    assert res["ready"] is True and res["reason"] == "endpoints_ready"
    assert res["serving_readiness"] is None
    # No model-load probing happens once the endpoint is already serving.
    assert not any(c["argv"][0] == "curl" for c in runner.calls)


async def test_gate_degrades_when_pods_unreadable(tmp_path):
    """A kubectl-pods failure must not break the gate: serving_readiness still returns (with no
    pod facts), and the probes are best-effort."""
    settings = Settings(_env_file=None, repos_dir=tmp_path / "repos", workspace_dir=tmp_path / "ws")

    class _PodsFail(_ProbeRunner):
        async def execute(self, logical_argv, entry, *, on_line=None, timeout=None, cwd=None):
            if logical_argv[:3] == ["kubectl", "get", "pods"]:
                self.calls.append({"argv": list(logical_argv), "entry": entry, "cwd": None})
                return RunResult(exit_code=1, duration_s=0.0, real_argv=list(logical_argv),
                                 cwd=None, output="Error from server (Forbidden)")
            return await super().execute(logical_argv, entry, on_line=on_line, timeout=timeout, cwd=cwd)

    runner = _PodsFail(settings.repo_paths,
                       canned={"get endpoints": ENDPOINTS_NOT_READY},
                       models_body=_HTTP_503, health_body=_HTTP_200)
    ctx = ToolContext(settings=settings, policy=CommandPolicy.from_file(settings.command_policy_path),
                      runner=runner, workspace=settings.resolved_workspace_dir / "sessions" / "s1")
    frozen = frozen_catalog()
    ctx._catalog = frozen
    ctx.catalog = lambda *, refresh=False: frozen

    res = await check_endpoint_readiness(ctx, namespace="bench", probe_cli_endpoints=False)
    sr = res["serving_readiness"]
    assert sr is not None and sr["pods"] == []          # graceful: no pod facts, still classified
    assert sr["health_status_code"] == 200 and sr["models_status_code"] == 503


def test_analyze_endpoints_unchanged_for_ready_case():
    """Regression: the Phase 24 endpoint verdict is untouched — serving_readiness defaults None."""
    v = analyze_endpoints(ENDPOINTS_READY, namespace="bench")
    assert v.ready is True and v.serving_readiness is None


def test_readiness_probes_knowledge_is_discoverable(tmp_path):
    """The judgment guide must be auto-discovered by read_knowledge (no CORE_KNOWLEDGE edit)."""
    from app.tools.access.knowledge_access import read_knowledge
    ctx, _ = _ctx(tmp_path, canned={})
    # Point the knowledge dir at the real one (the tmp settings still resolve to the project).
    out = read_knowledge(ctx, name="readiness_probes")
    assert out.get("topic") == "readiness_probes"
    body = out["content"].lower()
    # The guide carries the loading-vs-broken judgment + the startup-budget rule.
    assert "still loading" in body and "wedged" in body
    assert "failurethreshold" in body and "/v1/models" in body and "/health" in body
