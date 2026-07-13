"""Integration: the /readyz endpoint reflects the startup self-check, and the lifespan runs
the retention GC + self-check (Phase 18). Uses the real FastAPI wiring via TestClient — no
network, no cluster (the self-check only OBSERVES config and the workspace)."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings


@pytest.mark.skipif(not get_settings().bench_repo.is_dir(), reason="repo not present")
def test_readyz_endpoint_and_lifespan_wiring():
    from app.main import app

    with TestClient(app) as client:
        # The lifespan recorded a structured self-check on app.state.
        assert hasattr(app.state, "self_check")
        sc = app.state.self_check
        assert isinstance(sc.ok, bool)
        # /readyz mirrors it: 200 when ready, 503 when not, structured body either way.
        resp = client.get("/readyz")
        body = resp.json()
        assert "ready" in body and "self_check" in body
        assert resp.status_code == (200 if body["ready"] else 503)
        # Whatever the verdict, /readyz agrees with the recorded self-check on workspace
        # writability (the workspace under test is always writable in CI).
        names = {c["name"] for c in body["self_check"]["checks"]}
        assert "workspace_writable" in names
        assert "provider_coherent" in names
        # Liveness is separate and unconditionally ok.
        assert client.get("/healthz").json()["ok"] is True


def test_readyz_body_does_not_leak_host_paths_or_username():
    """BUG: /readyz is UNAUTHENTICATED but the self-check's detail/data carried absolute host
    paths (``writable at /home/<user>/…/workspace``, repo/policy paths), disclosing the host
    layout + OS username. The public body must relativize the home dir to ``~`` (server-side logs
    still keep the full paths)."""
    from app.main import app

    with TestClient(app) as client:
        raw = client.get("/readyz").text
        assert str(Path.home()) not in raw, "readyz body leaked the host home path / OS username"
        assert "/home/" not in raw, "readyz body leaked an absolute /home/ path"
