"""T3 — the manage_orchestrated_runs tool: list / stop / reap the orchestrator's K8s Jobs ON
THE CLUSTER, end-to-end through dispatch + the allowlisted kubectl runner (CaptureRunner), no
cluster. This is the surface that makes a submitted Job actually stoppable — cancel_run only
stops the in-process watch. Mirrors tests/test_orchestrator_tools.py's hermetic setup.
"""
from __future__ import annotations

import json

from app.config import Settings
from app.security.allowlist import Allowlist
from app.tools.context import ToolContext
from app.tools.run.manage_runs import manage_orchestrated_runs
from app.tools.registry import dispatch
from tests.flows.catalog_snapshot import frozen_catalog
from tests.flows.harness import CaptureRunner

# A namespace with one RUNNING (active) Job and one TERMINAL (succeeded) Job, each carrying the
# agent's identifying labels — exactly what `kubectl get jobs -o json` returns.
JOBS = json.dumps({"items": [
    {
        "metadata": {"name": "llmd-bench-r1-a1", "labels": {
            "llmd-bench/run-id": "r1-a1", "llmd-bench/session": "s1",
            "llmd-bench/sweep": "sw-abc", "llmd-bench/treatment": "1",
        }},
        "status": {"active": 1},
    },
    {
        "metadata": {"name": "llmd-bench-r2-a1", "labels": {
            "llmd-bench/run-id": "r2-a1", "llmd-bench/session": "s1",
        }},
        "status": {"succeeded": 1, "conditions": [{"type": "Complete", "status": "True"}]},
    },
]})


def _ctx(tmp_path, *, canned=None):
    settings = Settings(_env_file=None, repos_dir=tmp_path / "repos", workspace_dir=tmp_path / "ws")

    async def approve(kind, payload):
        return True

    runner = CaptureRunner(settings.repo_paths, canned={"get jobs": JOBS, **(canned or {})})
    ctx = ToolContext(
        settings=settings, allowlist=Allowlist.from_file(settings.allowlist_path),
        runner=runner, workspace=settings.resolved_workspace_dir / "sessions" / "s1",
        request_approval=approve,
    )
    frozen = frozen_catalog()
    ctx._catalog = frozen
    ctx.catalog = lambda *, refresh=False: frozen
    return ctx, runner


def _deletes(runner):
    """Job names passed to `kubectl delete job`."""
    return [c["argv"][c["argv"].index("job") + 1]
            for c in runner.calls if c["argv"][:3] == ["kubectl", "delete", "job"]]


async def test_list_classifies_jobs_from_cluster(tmp_path):
    ctx, runner = _ctx(tmp_path)
    out = await manage_orchestrated_runs(ctx, namespace="bench", action="list")

    assert out["action"] == "list" and out["n"] == 2
    assert out["n_active"] == 1 and out["n_terminal"] == 1
    by_run = {r["run_id"]: r for r in out["runs"]}
    assert by_run["r1-a1"]["phase"] == "active" and not by_run["r1-a1"]["terminal"]
    assert by_run["r1-a1"]["sweep_id"] == "sw-abc" and by_run["r1-a1"]["treatment"] == "1"
    assert by_run["r2-a1"]["terminal"] is True
    # list is read-only — it never deletes anything.
    assert _deletes(runner) == []


async def test_list_scopes_by_session_via_label_selector(tmp_path):
    ctx, runner = _ctx(tmp_path)
    await manage_orchestrated_runs(ctx, namespace="bench", action="list",
                                   session_id="s1", sweep_id="sw-abc")
    get = next(" ".join(c["argv"]) for c in runner.calls
               if c["argv"][:3] == ["kubectl", "get", "jobs"])
    assert "llmd-bench/session=s1" in get and "llmd-bench/sweep=sw-abc" in get
    assert "app.kubernetes.io/managed-by=llmd-bench-agent" in get


async def test_stop_deletes_only_running_jobs(tmp_path):
    ctx, runner = _ctx(tmp_path)
    out = await manage_orchestrated_runs(ctx, namespace="bench", action="stop")

    # Only the active Job is stopped; the terminal one is left for cleanup.
    assert out["stopped"] == ["llmd-bench-r1-a1"] and out["n_stopped"] == 1
    assert _deletes(runner) == ["llmd-bench-r1-a1"]


async def test_cleanup_reaps_only_terminal_jobs(tmp_path):
    ctx, runner = _ctx(tmp_path)
    out = await manage_orchestrated_runs(ctx, namespace="bench", action="cleanup")

    # Only the terminal Job is reaped; the in-flight one is never killed.
    assert out["deleted"] == ["llmd-bench-r2-a1"] and out["n_deleted"] == 1
    assert _deletes(runner) == ["llmd-bench-r2-a1"]


async def test_empty_namespace_is_graceful(tmp_path):
    ctx, _ = _ctx(tmp_path, canned={"get jobs": json.dumps({"items": []})})
    out = await manage_orchestrated_runs(ctx, namespace="bench", action="stop")
    assert out["stopped"] == [] and out["n_stopped"] == 0


async def test_dispatch_validates_action_enum(tmp_path):
    ctx, _ = _ctx(tmp_path)
    # An action outside the Literal enum is rejected at the schema gate (returned, not raised).
    bad = await dispatch(ctx, "manage_orchestrated_runs", {"namespace": "bench", "action": "nuke"})
    assert "error" in bad
    ok = await dispatch(ctx, "manage_orchestrated_runs", {"namespace": "bench", "action": "list"})
    assert ok["n"] == 2
