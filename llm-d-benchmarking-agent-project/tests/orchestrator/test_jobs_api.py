"""G1 — the read-only GET /api/jobs REST mirror of the orchestrator's run state. A non-chat
client can poll run state without driving the LLM. Exercised through TestClient with a hermetic
CaptureRunner standing in for kubectl (no cluster); read-only — the route never mutates.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.main import app
from tests.flows.harness import CaptureRunner

JOBS = json.dumps({"items": [
    {"metadata": {"name": "llmd-bench-r1-a1",
                  "labels": {"llmd-bench/run-id": "r1-a1", "llmd-bench/session": "s1"}},
     "status": {"active": 1}},
    {"metadata": {"name": "llmd-bench-r2-a1", "labels": {"llmd-bench/run-id": "r2-a1"}},
     "status": {"succeeded": 1, "conditions": [{"type": "Complete", "status": "True"}]}},
]})


def test_api_jobs_lists_cluster_runs():
    with TestClient(app) as client:
        client.app.state.runner = CaptureRunner(
            client.app.state.settings.repo_paths, canned={"get jobs": JOBS})
        body = client.get("/api/jobs", params={"namespace": "bench"}).json()
    assert body["available"] is True and body["n"] == 2
    assert body["n_active"] == 1 and body["n_terminal"] == 1
    assert {r["run_id"] for r in body["runs"]} == {"r1-a1", "r2-a1"}


def test_api_jobs_scopes_by_session_and_is_read_only():
    with TestClient(app) as client:
        runner = CaptureRunner(client.app.state.settings.repo_paths, canned={"get jobs": JOBS})
        client.app.state.runner = runner
        client.get("/api/jobs", params={"namespace": "bench", "session_id": "s1"})
    get = next(" ".join(c["argv"]) for c in runner.calls if c["argv"][:3] == ["kubectl", "get", "jobs"])
    assert "llmd-bench/session=s1" in get
    # A read mirror must NEVER mutate: no apply/delete ever leaves the route.
    assert not any(c["argv"][:2] in (["kubectl", "delete"], ["kubectl", "apply"]) for c in runner.calls)


def test_api_jobs_soft_fails_without_cluster():
    with TestClient(app) as client:
        runner = CaptureRunner(client.app.state.settings.repo_paths)

        async def boom(*a, **k):
            raise RuntimeError("no cluster reachable")

        runner.execute = boom  # type: ignore[method-assign]
        client.app.state.runner = runner
        r = client.get("/api/jobs", params={"namespace": "bench"})
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False and body["runs"] == [] and body["n"] == 0
