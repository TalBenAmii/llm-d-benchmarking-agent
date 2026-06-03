"""Phase 3e — the orchestrate_benchmark_run agent tool: wires the orchestrator to the agent,
end-to-end through dispatch + the allowlisted kubectl runner (CaptureRunner), no cluster."""
from __future__ import annotations

import json

import pytest

from app.config import Settings
from app.security.allowlist import Allowlist
from app.tools.context import ToolContext, ToolError
from app.tools.orchestrate import orchestrate_benchmark_run
from app.tools.registry import dispatch
from tests.flows.catalog_snapshot import frozen_catalog
from tests.flows.harness import CaptureRunner

SUCCEEDED_JOB = json.dumps({"items": [{
    "metadata": {"name": "llmd-bench-x", "labels": {}},
    "status": {"succeeded": 1, "conditions": [{"type": "Complete", "status": "True"}]},
}]})

# Phase 24: orchestrate_benchmark_run now gates submission on a real endpoint-readiness check.
# These tests exercise the submit/watch/retry/manifest mechanics, so they stand the endpoint
# up READY by default — the gate is transparent when the inference endpoint is serving (a
# Service with a ready backing address). Tests asserting the gate BLOCKS live in
# tests/test_endpoint_readiness.py.
ENDPOINTS_READY = json.dumps({"items": [
    {"metadata": {"name": "kubernetes"}, "subsets": [{"addresses": [{"ip": "10.96.0.1"}]}]},
    {"metadata": {"name": "llm-d-inference"}, "subsets": [{"addresses": [{"ip": "10.244.0.7"}]}]},
]})


def _ctx(tmp_path, *, canned=None, image=""):
    settings = Settings(_env_file=None, repos_dir=tmp_path / "repos",
                        workspace_dir=tmp_path / "ws", orchestrator_image=image)

    async def approve(kind, payload):
        return True

    # Default the endpoint to READY so the readiness gate passes; a test can override.
    canned = {"get endpoints": ENDPOINTS_READY, **(canned or {})}
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


async def test_tool_requires_an_image(tmp_path):
    ctx, runner = _ctx(tmp_path, image="")  # none configured, none passed
    with pytest.raises(ToolError):
        await orchestrate_benchmark_run(ctx, namespace="bench", spec="cicd/kind",
                                        harness="inference-perf", workload="sanity_random.yaml")
    assert runner.calls == []  # refused before touching the cluster


async def test_tool_submits_watches_and_succeeds(tmp_path):
    ctx, runner = _ctx(tmp_path, canned={"get jobs": SUCCEEDED_JOB})
    res = await dispatch(ctx, "orchestrate_benchmark_run", {
        "namespace": "bench", "spec": "cicd/kind", "harness": "inference-perf",
        "workload": "sanity_random.yaml", "image": "ghcr.io/llm-d/bench:0",
        "poll_interval": 0, "watch": True,
    })
    assert res["succeeded"] is True and res["dead_lettered"] is False
    applies = [c["argv"] for c in runner.calls if c["argv"][:2] == ["kubectl", "apply"]]
    assert applies and applies[-1][-2:] == ["-n", "bench"]      # a Job was applied to the ns


async def test_tool_streams_pod_logs_as_output_events(tmp_path):
    """Phase 21 end-to-end: through dispatch + the REAL RealKubeClient + the allowlisted
    `kubectl logs -f` runner path, the benchmark pod's log lines surface as `output` events
    (the SAME event the UI renders) DURING the run — not just at the end."""
    pod_logs = "starting benchmark\nload point 1/2\nload point 2/2\nbenchmark complete: 30/30 ok"
    ctx, runner = _ctx(tmp_path, canned={"get jobs": SUCCEEDED_JOB, "logs": pod_logs})

    events: list[tuple[str, dict]] = []

    async def emit(t, p):
        events.append((t, p))

    ctx.emit = emit

    res = await dispatch(ctx, "orchestrate_benchmark_run", {
        "namespace": "bench", "spec": "cicd/kind", "harness": "inference-perf",
        "workload": "sanity_random.yaml", "image": "ghcr.io/llm-d/bench:0",
        "poll_interval": 0, "watch": True,
    })
    assert res["succeeded"] is True

    # The pod log lines were emitted as `output` events, in order, via the standard transport.
    output_lines = [p["line"] for (t, p) in events if t == "output"]
    for expected in pod_logs.splitlines():
        assert expected in output_lines
    assert output_lines.index("starting benchmark") < output_lines.index("benchmark complete: 30/30 ok")

    # And it really used the allowlisted `kubectl logs -f` path (read-only, argv-only).
    log_calls = [c["argv"] for c in runner.calls if c["argv"][:2] == ["kubectl", "logs"]]
    assert log_calls and "-f" in log_calls[-1]


async def test_tool_submit_only_does_not_watch(tmp_path):
    ctx, runner = _ctx(tmp_path, image="ghcr.io/llm-d/bench:0")
    res = await dispatch(ctx, "orchestrate_benchmark_run", {
        "namespace": "bench", "spec": "cicd/kind", "watch": False,
    })
    assert res["submitted"] is True and res["job"].startswith("llmd-bench-")
    assert not any(c["argv"][:3] == ["kubectl", "get", "jobs"] for c in runner.calls)  # never watched


async def test_tool_retries_transient_then_succeeds(tmp_path):
    """End-to-end through dispatch: max_attempts>1 flows into the orchestrator, a transient
    failure retries as a distinct Job (-a1, -a2), and the run finally succeeds."""
    from app.security.runner import RunResult

    failed_job = json.dumps({"items": [{"metadata": {"name": "j"}, "status": {
        "failed": 1, "conditions": [{"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded"}]}}]})

    class _SeqRunner(CaptureRunner):
        """Returns a FAILED job for the first `get jobs`, SUCCEEDED for the next. The endpoint
        is READY (canned) so the Phase 24 readiness gate passes and the run reaches submission."""
        def __init__(self, repo_paths):
            super().__init__(repo_paths, canned={"get endpoints": ENDPOINTS_READY})
            self._gj = [failed_job, SUCCEEDED_JOB]
            self._i = 0

        async def execute(self, logical_argv, entry, *, on_line=None, timeout=None, cwd=None, extra_env=None):
            if "get jobs" in " ".join(logical_argv):
                out = self._gj[min(self._i, len(self._gj) - 1)]
                self._i += 1
                self.calls.append({"argv": list(logical_argv), "entry": entry, "cwd": None})
                return RunResult(exit_code=0, duration_s=0.0, real_argv=list(logical_argv), cwd=None, output=out)
            return await super().execute(logical_argv, entry, on_line=on_line, timeout=timeout, cwd=cwd, extra_env=extra_env)

    settings = Settings(_env_file=None, repos_dir=tmp_path / "repos",
                        workspace_dir=tmp_path / "ws", orchestrator_image="img")

    async def approve(kind, payload):
        return True

    runner = _SeqRunner(settings.repo_paths)
    ctx = ToolContext(settings=settings, allowlist=Allowlist.from_file(settings.allowlist_path),
                      runner=runner, workspace=settings.resolved_workspace_dir / "sessions" / "s1",
                      request_approval=approve)
    frozen = frozen_catalog()
    ctx._catalog = frozen
    ctx.catalog = lambda *, refresh=False: frozen

    res = await dispatch(ctx, "orchestrate_benchmark_run", {
        "namespace": "bench", "spec": "cicd/kind", "max_attempts": 2, "poll_interval": 0, "watch": True,
    })
    assert res["succeeded"] is True and res["dead_lettered"] is False
    runids = [a["run_id"] for a in res["attempts"]]
    assert len(runids) == 2 and runids[0].endswith("-a1") and runids[1].endswith("-a2")
    applies = [c for c in runner.calls if c["argv"][:2] == ["kubectl", "apply"]]
    assert len(applies) == 2   # two distinct Job submissions


async def test_tool_default_command_embeds_run_invocation(tmp_path):
    ctx, runner = _ctx(tmp_path, image="ghcr.io/llm-d/bench:0")
    await dispatch(ctx, "orchestrate_benchmark_run", {
        "namespace": "bench", "spec": "cicd/kind", "harness": "inference-perf",
        "workload": "sanity_random.yaml", "watch": False,
    })
    import yaml
    manifest = yaml.safe_load(next((ctx.workspace / "jobs").glob("*.yaml")).read_text())
    cmd = manifest["spec"]["template"]["spec"]["containers"][0]["command"]
    assert cmd == ["llmdbenchmark", "--spec", "cicd/kind", "run", "-p", "bench",
                   "-l", "inference-perf", "-w", "sanity_random.yaml"]
    assert manifest["spec"]["backoffLimit"] == 0
    # No SA configured/passed → the pod uses the namespace default (no serviceAccountName key).
    assert "serviceAccountName" not in manifest["spec"]["template"]["spec"]


async def test_tool_runs_job_under_configured_service_account(tmp_path):
    """Phase 8: the least-privilege SA the deploy creates flows into the submitted Job, so an
    in-cluster orchestrated run authenticates as that SA (resolving the Phase-3 RBAC gap)."""
    import yaml

    settings = Settings(_env_file=None, repos_dir=tmp_path / "repos", workspace_dir=tmp_path / "ws",
                        orchestrator_image="ghcr.io/llm-d/bench:0",
                        orchestrator_service_account="llm-d-benchmarking-agent")

    async def approve(kind, payload):
        return True

    runner = CaptureRunner(settings.repo_paths, canned={"get endpoints": ENDPOINTS_READY})
    ctx = ToolContext(settings=settings, allowlist=Allowlist.from_file(settings.allowlist_path),
                      runner=runner, workspace=settings.resolved_workspace_dir / "sessions" / "s1",
                      request_approval=approve)
    frozen = frozen_catalog()
    ctx._catalog = frozen
    ctx.catalog = lambda *, refresh=False: frozen

    await dispatch(ctx, "orchestrate_benchmark_run", {
        "namespace": "bench", "spec": "cicd/kind", "watch": False,
    })
    manifest = yaml.safe_load(next((ctx.workspace / "jobs").glob("*.yaml")).read_text())
    assert manifest["spec"]["template"]["spec"]["serviceAccountName"] == "llm-d-benchmarking-agent"
