"""Phase 3a — orchestrator foundation: the allowlist additions for managing K8s Jobs via
kubectl, and the RealKubeClient that shells out to those allowlisted commands.

The KubeClient is exercised against the hermetic CaptureRunner (records argv, replays canned
output) so we assert exact commands + JSON parsing + workspace confinement with no cluster.
"""
from __future__ import annotations

import json
import os

import pytest

from app.config import Settings
from app.orchestrator.kube import KubeError, RealKubeClient, parse_items
from app.security.allowlist import MUTATING, READ_ONLY, Allowlist
from app.tools.context import ToolContext
from tests.flows.catalog_snapshot import frozen_catalog
from tests.flows.harness import CaptureRunner

# ---- allowlist: the new kubectl surface -----------------------------------

ALLOW = [
    (["kubectl", "apply", "-f", "/ws/job.yaml", "-n", "bench"], MUTATING),
    (["kubectl", "apply", "--filename", "/ws/run-1.yml", "--namespace", "bench"], MUTATING),
    (["kubectl", "get", "jobs", "-n", "bench", "-l", "run-id=abc", "-o", "json"], READ_ONLY),
    (["kubectl", "get", "jobs", "-l", "run-id=abc", "--watch", "-o", "json"], READ_ONLY),
    (["kubectl", "get", "pods", "-n", "bench", "-l", "job-name=x", "-o", "json"], READ_ONLY),
    (["kubectl", "logs", "-l", "job-name=x", "-n", "bench", "--tail", "200", "-f"], READ_ONLY),
    (["kubectl", "delete", "job", "my-job", "-n", "bench"], MUTATING),
    (["kubectl", "delete", "jobs", "my-job", "--ignore-not-found"], MUTATING),
]

DENY = [
    ["kubectl", "delete", "pod", "my-pod"],            # delete restricted to jobs
    ["kubectl", "delete", "namespace", "bench"],       # cannot remove arbitrary objects
    ["kubectl", "apply", "-f", "/etc/passwd"],         # -f must be a .yaml
    ["kubectl", "apply", "-f", "/ws/job.yaml;rm"],     # shell metachar screen
    ["kubectl", "logs", "somepod"],                    # logs by selector only, no positional
]


@pytest.mark.parametrize("argv,mode", ALLOW, ids=[" ".join(a) for a, _ in ALLOW])
def test_orchestrator_kubectl_allowed(allowlist, catalog, argv, mode):
    d = allowlist.validate(argv, catalog=catalog)
    assert d.allowed and d.mode == mode


@pytest.mark.parametrize("argv", DENY, ids=[" ".join(a) for a in DENY])
def test_orchestrator_kubectl_denied(allowlist, catalog, argv):
    assert not allowlist.validate(argv, catalog=catalog).allowed


# ---- parse_items ----------------------------------------------------------

def test_parse_items_list_object():
    out = json.dumps({"kind": "List", "items": [{"metadata": {"name": "a"}}, {"metadata": {"name": "b"}}]})
    items = parse_items(out)
    assert [i["metadata"]["name"] for i in items] == ["a", "b"]


def test_parse_items_single_object_is_wrapped():
    out = json.dumps({"kind": "Job", "metadata": {"name": "solo"}})
    assert parse_items(out) == [{"kind": "Job", "metadata": {"name": "solo"}}]


def test_parse_items_empty_and_garbage():
    assert parse_items("") == []
    assert parse_items("not json") == []
    assert parse_items(json.dumps({"kind": "List", "items": None})) == []


def test_parse_items_drops_non_dict_elements():
    # Defense-in-depth at the SOURCE: a forged/corrupt `kubectl get ... -o json` whose `items`
    # carries non-dict elements (a bare string / number / list / null) must be filtered out, so
    # NO consumer (controller.classify_job_status, classify_failure, parse_checkpoint, the chaos
    # decorator's _run_id_of) can AttributeError on a `.get` of a non-dict. Mirrors the sibling
    # parsers (readiness.diagnostics._parse_items, tools.probe._items_from_json).
    out = json.dumps({
        "kind": "List",
        "items": [{"metadata": {"name": "good"}}, "bad-string", 42, None, ["nested"]],
    })
    items = parse_items(out)
    assert items == [{"metadata": {"name": "good"}}]
    assert all(isinstance(i, dict) for i in items)


# ---- RealKubeClient against the CaptureRunner -----------------------------

def _ctx(tmp_path, *, canned=None):
    settings = Settings(_env_file=None, repos_dir=tmp_path / "repos", workspace_dir=tmp_path / "ws")

    async def approve(kind, payload):
        return True

    runner = CaptureRunner(settings.repo_paths, canned=canned or {})
    ctx = ToolContext(
        settings=settings,
        allowlist=Allowlist.from_file(settings.allowlist_path),
        runner=runner,
        workspace=settings.resolved_workspace_dir / "sessions" / "s1",
        request_approval=approve,
    )
    frozen = frozen_catalog()
    ctx._catalog = frozen
    ctx.catalog = lambda *, refresh=False: frozen
    return ctx, runner


async def test_apply_builds_argv_and_confines_to_workspace(tmp_path):
    ctx, runner = _ctx(tmp_path)
    kube = RealKubeClient(ctx)
    manifest = ctx.workspace / "jobs" / "run-1.yaml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("apiVersion: batch/v1\nkind: Job\n")

    res = await kube.apply(manifest, namespace="bench")
    assert res.exit_code == 0
    assert runner.calls[-1]["argv"] == ["kubectl", "apply", "-f", str(manifest.resolve()), "-n", "bench"]


async def test_apply_refuses_manifest_outside_workspace(tmp_path):
    ctx, runner = _ctx(tmp_path)
    kube = RealKubeClient(ctx)
    with pytest.raises(KubeError):
        await kube.apply("/etc/evil.yaml", namespace="bench")
    assert runner.calls == []  # nothing ran


async def test_apply_refuses_symlink_escape(tmp_path):
    # A symlink INSIDE the workspace pointing OUTSIDE must be refused (resolve() follows it).
    ctx, runner = _ctx(tmp_path)
    kube = RealKubeClient(ctx)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "evil.yaml").write_text("kind: Job\n")
    ctx.workspace.mkdir(parents=True, exist_ok=True)
    link = ctx.workspace / "link.yaml"
    os.symlink(outside / "evil.yaml", link)
    with pytest.raises(KubeError):
        await kube.apply(link, namespace="bench")
    assert runner.calls == []


async def test_apply_refuses_dotdot_escape(tmp_path):
    ctx, runner = _ctx(tmp_path)
    kube = RealKubeClient(ctx)
    escape = ctx.workspace / ".." / ".." / ".." / ".." / "evil.yaml"  # resolves above the workspace
    with pytest.raises(KubeError):
        await kube.apply(escape, namespace="bench")
    assert runner.calls == []


async def test_apply_allows_symlink_within_workspace(tmp_path):
    # Positive control: a symlink resolving to a real manifest INSIDE the workspace is fine.
    ctx, runner = _ctx(tmp_path)
    kube = RealKubeClient(ctx)
    real = ctx.workspace / "real" / "m.yaml"
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_text("kind: Job\n")
    link = ctx.workspace / "good.yaml"
    os.symlink(real, link)
    res = await kube.apply(link, namespace="bench")
    assert res.exit_code == 0 and runner.calls  # it ran


async def test_list_jobs_parses_and_passes_selector(tmp_path):
    jobs_json = json.dumps({"kind": "List", "items": [{"metadata": {"name": "j1"}}]})
    ctx, runner = _ctx(tmp_path, canned={"get jobs": jobs_json})
    kube = RealKubeClient(ctx)

    jobs = await kube.list_jobs(namespace="bench", selector="run-id=abc")
    assert [j["metadata"]["name"] for j in jobs] == ["j1"]
    argv = runner.calls[-1]["argv"]
    assert argv == ["kubectl", "get", "jobs", "-n", "bench", "-o", "json", "-l", "run-id=abc"]


async def test_list_jobs_filters_forged_non_dict_items(tmp_path):
    # End-to-end at the boundary: a forged `kubectl get jobs -o json` with a non-dict element
    # must NOT reach a consumer's `.get`. The source filter in parse_items drops it, so the only
    # remaining UNGUARDED consumer — the chaos decorator's _run_id_of (a bare `job.get(...)`) —
    # can iterate the result without AttributeError. (Before the fix, ChaosKubeClient.list_jobs
    # raised AttributeError on the bare string element.)
    from app.orchestrator.chaos import ChaosKubeClient, ChaosPlan

    jobs_json = json.dumps({"kind": "List", "items": [{"metadata": {"name": "j1"}}, "forged"]})
    ctx, runner = _ctx(tmp_path, canned={"get jobs": jobs_json})
    kube = RealKubeClient(ctx)
    assert [j["metadata"]["name"] for j in await kube.list_jobs(namespace="bench")] == ["j1"]

    chaos = ChaosKubeClient(kube, ChaosPlan(injections=[]))
    jobs = await chaos.list_jobs(namespace="bench")  # must not raise on the forged element
    assert [j["metadata"]["name"] for j in jobs] == ["j1"]


async def test_list_pods_argv(tmp_path):
    ctx, runner = _ctx(tmp_path, canned={"get pods": json.dumps({"items": []})})
    kube = RealKubeClient(ctx)
    await kube.list_pods(namespace="bench", selector="job-name=j1")
    assert runner.calls[-1]["argv"] == ["kubectl", "get", "pods", "-n", "bench", "-o", "json", "-l", "job-name=j1"]


async def test_logs_streams_and_returns_output(tmp_path):
    ctx, runner = _ctx(tmp_path, canned={"logs": "line-1\nline-2"})
    kube = RealKubeClient(ctx)
    out = await kube.logs(namespace="bench", selector="job-name=j1", tail=200, follow=True)
    assert "line-1" in out and "line-2" in out
    assert runner.calls[-1]["argv"] == ["kubectl", "logs", "-l", "job-name=j1", "-n", "bench", "--tail", "200", "-f"]


async def test_delete_job_argv(tmp_path):
    ctx, runner = _ctx(tmp_path)
    kube = RealKubeClient(ctx)
    await kube.delete_job("run-1", namespace="bench")
    assert runner.calls[-1]["argv"] == ["kubectl", "delete", "job", "run-1", "-n", "bench", "--ignore-not-found"]
