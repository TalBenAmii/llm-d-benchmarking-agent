"""Phase 24 — endpoint health-check before submit (+ optional auto-standup).

Hermetic, no cluster: the endpoint-readiness gate is exercised end-to-end through dispatch +
the allowlisted kubectl runner (CaptureRunner returning canned `kubectl get endpoints` JSON).
Asserts:
  * the gate is a REAL endpoint-readiness check (ready backing addresses), not pod presence;
  * an UNREADY endpoint blocks submission with a structured not-ready result + standup
    suggestion AND mutates nothing (no `kubectl apply`);
  * a READY endpoint lets the orchestrated run proceed exactly as before;
  * auto-standup is only PROPOSED — the gate itself never runs a mutating standup.
"""
from __future__ import annotations

import json

import pytest

from app.config import Settings
from app.readiness.diagnostics import analyze_endpoints
from app.readiness.probes import check_endpoint_readiness
from app.security.allowlist import MUTATING, Allowlist
from app.tools.context import ToolContext, ToolError
from app.tools.registry import dispatch
from tests._helpers import kubectl_present
from tests.flows.catalog_snapshot import frozen_catalog
from tests.flows.harness import CaptureRunner

# A Service WITH a ready backing endpoint (the inference endpoint is serving). The default
# `kubernetes` Service is included to prove it is correctly ignored.
ENDPOINTS_READY = json.dumps({"items": [
    {"metadata": {"name": "kubernetes"}, "subsets": [{"addresses": [{"ip": "10.96.0.1"}]}]},
    {"metadata": {"name": "llm-d-inference"},
     "subsets": [{"addresses": [{"ip": "10.244.0.7"}], "notReadyAddresses": []}]},
]})

# A Service that EXISTS but has only notReadyAddresses — a pod is present (and may even be
# Running) but it is NOT serving. This is exactly the case pod-presence would wrongly pass.
ENDPOINTS_NOT_READY = json.dumps({"items": [
    {"metadata": {"name": "kubernetes"}, "subsets": [{"addresses": [{"ip": "10.96.0.1"}]}]},
    {"metadata": {"name": "llm-d-inference"},
     "subsets": [{"notReadyAddresses": [{"ip": "10.244.0.7"}]}]},
]})

# Only the cluster's own API Service — no inference endpoint at all.
ENDPOINTS_NONE = json.dumps({"items": [
    {"metadata": {"name": "kubernetes"}, "subsets": [{"addresses": [{"ip": "10.96.0.1"}]}]},
]})

SUCCEEDED_JOB = json.dumps({"items": [{
    "metadata": {"name": "llmd-bench-x", "labels": {}},
    "status": {"succeeded": 1, "conditions": [{"type": "Complete", "status": "True"}]},
}]})


def _ctx(tmp_path, *, canned=None, image="", which_kubectl=True, simulate=False):
    settings = Settings(_env_file=None, repos_dir=tmp_path / "repos",
                        workspace_dir=tmp_path / "ws", orchestrator_image=image,
                        simulate=simulate)

    async def approve(kind, payload):
        return True

    runner = CaptureRunner(settings.repo_paths, canned=canned or {})
    ctx = ToolContext(
        settings=settings, allowlist=Allowlist.from_file(settings.allowlist_path),
        runner=runner, workspace=settings.resolved_workspace_dir / "sessions" / "s1",
        request_approval=approve,
    )
    frozen = frozen_catalog()
    ctx._catalog = frozen
    ctx.catalog = lambda *, refresh=False: frozen
    return ctx, runner, settings


# Force `kubectl` to look present (the readiness tool guards on shutil.which) so the canned
# runner is reached on every host, with no real binary needed.
@pytest.fixture(autouse=True)
def _kubectl_present(monkeypatch):
    kubectl_present(monkeypatch, target="app.readiness.probes")


# ----------------------------------------------------------------------------
# Pure analyzer (mechanism): readiness == ready backing endpoints, not presence
# ----------------------------------------------------------------------------

def test_analyze_ready_endpoint():
    v = analyze_endpoints(ENDPOINTS_READY, namespace="bench")
    assert v.ready is True and v.reason == "endpoints_ready"
    assert [e["service"] for e in v.ready_endpoints] == ["llm-d-inference"]


def test_analyze_present_but_not_serving_is_not_ready():
    """The crux: a Service whose only addresses are notReady (pod present, not serving) is
    NOT ready — strictly beyond pod-presence."""
    v = analyze_endpoints(ENDPOINTS_NOT_READY, namespace="bench")
    assert v.ready is False and v.reason == "endpoints_not_ready"
    assert v.not_ready_endpoints and v.not_ready_endpoints[0]["not_ready_addresses"] == 1


def test_analyze_no_inference_endpoint():
    v = analyze_endpoints(ENDPOINTS_NONE, namespace="bench")
    assert v.ready is False and v.reason == "no_endpoints"


def test_analyze_ignores_kubernetes_service():
    """The always-present `kubernetes` Service must never be counted as an inference endpoint."""
    only_k8s = json.dumps({"items": [
        {"metadata": {"name": "kubernetes"}, "subsets": [{"addresses": [{"ip": "10.96.0.1"}]}]},
    ]})
    v = analyze_endpoints(only_k8s, namespace="bench")
    assert v.ready is False  # would be True if the API service leaked through


def test_analyze_garbage_input_is_not_ready_never_raises():
    for bad in ("", "not json", "null", "[]"):
        v = analyze_endpoints(bad, namespace="bench")
        assert v.ready is False


# ----------------------------------------------------------------------------
# The tool: read-only, structured, never mutates, proposes standup when unready
# ----------------------------------------------------------------------------

async def test_tool_ready_endpoint(tmp_path):
    ctx, runner, _ = _ctx(tmp_path, canned={"get endpoints": ENDPOINTS_READY})
    res = await dispatch(ctx, "check_endpoint_readiness", {"namespace": "bench"})
    assert res["ready"] is True and res["reason"] == "endpoints_ready"
    assert "standup_suggestion" not in res
    # Every command the gate ran was read-only (it auto-runs, no approval, no mutation).
    assert runner.calls
    for c in runner.calls:
        d = ctx.allowlist.validate(c["argv"], catalog=ctx.catalog_for_allowlist())
        assert d.mode != MUTATING, f"readiness gate ran a mutating command: {c['argv']}"
    assert not any(c["argv"][:2] == ["kubectl", "apply"] for c in runner.calls)


async def test_tool_unready_proposes_standup_but_does_not_run_it(tmp_path):
    ctx, runner, _ = _ctx(tmp_path, canned={"get endpoints": ENDPOINTS_NOT_READY})
    res = await dispatch(ctx, "check_endpoint_readiness",
                         {"namespace": "bench", "spec": "cicd/kind"})
    assert res["ready"] is False
    sug = res["standup_suggestion"]
    assert sug["recommended"] is True
    assert sug["subcommand"] == "standup" and sug["approval_required"] is True
    # Crucially: a standup was only PROPOSED — no standup/apply command actually ran.
    joined = [" ".join(c["argv"]) for c in runner.calls]
    assert not any("standup" in j for j in joined)
    assert not any(c["argv"][:2] == ["kubectl", "apply"] for c in runner.calls)


async def test_tool_uses_the_endpoints_readonly_path(tmp_path):
    ctx, runner, _ = _ctx(tmp_path, canned={"get endpoints": ENDPOINTS_NONE})
    await check_endpoint_readiness(ctx, namespace="bench")
    ep_calls = [c["argv"] for c in runner.calls
                if c["argv"][:3] == ["kubectl", "get", "endpoints"]]
    assert ep_calls and ep_calls[-1][-2:] == ["-o", "json"]


async def test_tool_cluster_unreachable_is_structured_not_ready(tmp_path):
    """A non-zero kubectl exit (unreachable cluster / missing ns) yields a structured
    not-ready verdict with a standup suggestion — never a crash."""
    class _FailRunner(CaptureRunner):
        async def execute(self, logical_argv, entry, *, on_line=None, timeout=None, cwd=None, extra_env=None):
            res = await super().execute(logical_argv, entry, on_line=on_line, timeout=timeout, cwd=cwd, extra_env=extra_env)
            if logical_argv[:3] == ["kubectl", "get", "endpoints"]:
                from app.security.runner import RunResult
                return RunResult(exit_code=1, duration_s=0.0, real_argv=list(logical_argv),
                                 cwd=None, output="Unable to connect to the server")
            return res

    settings = Settings(_env_file=None, repos_dir=tmp_path / "repos", workspace_dir=tmp_path / "ws")
    runner = _FailRunner(settings.repo_paths)
    ctx = ToolContext(settings=settings, allowlist=Allowlist.from_file(settings.allowlist_path),
                      runner=runner, workspace=settings.resolved_workspace_dir / "sessions" / "s1")
    frozen = frozen_catalog()
    ctx._catalog = frozen
    ctx.catalog = lambda *, refresh=False: frozen

    res = await check_endpoint_readiness(ctx, namespace="bench", probe_cli_endpoints=False)
    assert res["ready"] is False and res["reason"] == "cluster_unreachable"
    assert res["standup_suggestion"]["approval_required"] is True


async def test_tool_requires_kubectl(tmp_path, monkeypatch):
    monkeypatch.setattr("app.readiness.probes.shutil.which", lambda name, *a, **k: None)
    ctx, _runner, _ = _ctx(tmp_path)
    with pytest.raises(ToolError):
        await check_endpoint_readiness(ctx, namespace="bench")


async def test_tool_corroborates_with_cli_list_endpoints(tmp_path):
    """When the benchmark venv is installed, the gate ALSO runs the CLI's read-only
    `run --list-endpoints` and reports how many endpoints it saw (corroboration only — the
    Kubernetes endpoint readiness remains the gate)."""
    cli_out = "Discovered endpoints:\n  http://llm-d-inference.bench.svc:8000/v1\n  http://llm-d-inference.bench.svc:8001/v1\n"
    ctx, runner, settings = _ctx(
        tmp_path,
        canned={"get endpoints": ENDPOINTS_READY, "--list-endpoints": cli_out},
        image="ghcr.io/llm-d/bench:0",
    )
    # Materialize a fake benchmark venv python so the corroborating probe runs.
    venv_bin = settings.bench_repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    (venv_bin / "python").write_text("")

    res = await check_endpoint_readiness(ctx, namespace="bench", spec="cicd/kind")
    assert res["ready"] is True
    assert res["cli_endpoints_seen"] == 2  # two distinct URLs, deduped
    # It really used the read-only `run --list-endpoints` path (never deploys).
    le_calls = [c["argv"] for c in runner.calls if "--list-endpoints" in c["argv"]]
    assert le_calls and le_calls[-1][:3] == ["llmdbenchmark", "--spec", "cicd/kind"]
    # And nothing mutated.
    for c in runner.calls:
        d = ctx.allowlist.validate(c["argv"], catalog=ctx.catalog_for_allowlist())
        assert d.mode != MUTATING


# ----------------------------------------------------------------------------
# The orchestrate gate: unready blocks submission (no mutation); ready proceeds
# ----------------------------------------------------------------------------

async def test_orchestrate_blocks_on_unready_endpoint_no_mutation(tmp_path):
    ctx, runner, _ = _ctx(tmp_path, canned={"get endpoints": ENDPOINTS_NOT_READY},
                          image="ghcr.io/llm-d/bench:0")
    res = await dispatch(ctx, "orchestrate_benchmark_run", {
        "namespace": "bench", "spec": "cicd/kind", "harness": "inference-perf",
        "workload": "sanity_random.yaml", "poll_interval": 0, "watch": True,
    })
    assert res["submitted"] is False and res["ready"] is False
    assert res["standup_suggestion"]["subcommand"] == "standup"
    # NOTHING was mutated: no Job manifest applied, and no manifest was even written.
    assert not any(c["argv"][:2] == ["kubectl", "apply"] for c in runner.calls)
    assert not (ctx.workspace / "jobs").exists()


async def test_orchestrate_no_endpoint_blocks(tmp_path):
    ctx, runner, _ = _ctx(tmp_path, canned={"get endpoints": ENDPOINTS_NONE},
                          image="ghcr.io/llm-d/bench:0")
    res = await dispatch(ctx, "orchestrate_benchmark_run", {
        "namespace": "bench", "spec": "cicd/kind", "watch": True, "poll_interval": 0,
    })
    assert res["submitted"] is False and res["readiness"]["reason"] == "no_endpoints"
    assert not any(c["argv"][:2] == ["kubectl", "apply"] for c in runner.calls)


async def test_orchestrate_proceeds_when_endpoint_ready(tmp_path):
    """A READY endpoint lets the orchestrated run proceed exactly as before — it submits the
    Job (kubectl apply) and watches it to success."""
    ctx, runner, _ = _ctx(tmp_path,
                          canned={"get endpoints": ENDPOINTS_READY, "get jobs": SUCCEEDED_JOB},
                          image="ghcr.io/llm-d/bench:0")
    res = await dispatch(ctx, "orchestrate_benchmark_run", {
        "namespace": "bench", "spec": "cicd/kind", "harness": "inference-perf",
        "workload": "sanity_random.yaml", "poll_interval": 0, "watch": True,
    })
    assert res.get("succeeded") is True and "submitted" not in res
    applies = [c["argv"] for c in runner.calls if c["argv"][:2] == ["kubectl", "apply"]]
    assert applies and applies[-1][-2:] == ["-n", "bench"]


async def test_orchestrate_can_opt_out_of_readiness_gate(tmp_path):
    """require_ready_endpoint=false skips the gate entirely (external -U endpoint case): the
    run proceeds without ever reading endpoints."""
    ctx, runner, _ = _ctx(tmp_path, canned={"get jobs": SUCCEEDED_JOB},
                          image="ghcr.io/llm-d/bench:0")
    res = await dispatch(ctx, "orchestrate_benchmark_run", {
        "namespace": "bench", "spec": "cicd/kind", "poll_interval": 0, "watch": True,
        "require_ready_endpoint": False,
    })
    assert res.get("succeeded") is True
    assert not any(c["argv"][:3] == ["kubectl", "get", "endpoints"] for c in runner.calls)


async def test_orchestrate_simulate_mode_skips_gate(tmp_path):
    """Simulate mode deploys nothing, so the readiness gate is skipped (the synthetic walk must
    not block on a real cluster check)."""
    ctx, runner, _ = _ctx(tmp_path, canned={"get jobs": SUCCEEDED_JOB},
                          image="ghcr.io/llm-d/bench:0", simulate=True)
    res = await dispatch(ctx, "orchestrate_benchmark_run", {
        "namespace": "bench", "spec": "cicd/kind", "poll_interval": 0, "watch": True,
    })
    assert res.get("succeeded") is True and res.get("submitted") is not False
    assert not any(c["argv"][:3] == ["kubectl", "get", "endpoints"] for c in runner.calls)
