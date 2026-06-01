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


def _ctx(tmp_path, *, canned=None, image=""):
    settings = Settings(_env_file=None, repos_dir=tmp_path / "repos",
                        workspace_dir=tmp_path / "ws", orchestrator_image=image)

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
        """Returns a FAILED job for the first `get jobs`, SUCCEEDED for the next."""
        def __init__(self, repo_paths):
            super().__init__(repo_paths)
            self._gj = [failed_job, SUCCEEDED_JOB]
            self._i = 0

        async def execute(self, logical_argv, entry, *, on_line=None, timeout=None, cwd=None):
            if "get jobs" in " ".join(logical_argv):
                out = self._gj[min(self._i, len(self._gj) - 1)]
                self._i += 1
                self.calls.append({"argv": list(logical_argv), "entry": entry, "cwd": None})
                return RunResult(exit_code=0, duration_s=0.0, real_argv=list(logical_argv), cwd=None, output=out)
            return await super().execute(logical_argv, entry, on_line=on_line, timeout=timeout, cwd=cwd)

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
