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
