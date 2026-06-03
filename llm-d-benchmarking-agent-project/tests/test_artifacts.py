"""Per-run chart artifacts: discovery (locate_and_parse_report's `charts`) + the read-only
serving route (``GET /api/sessions/{sid}/artifact``).

Hermetic: a fake session tree under a tmp workspace, no cluster/network. The serving route
reads ``get_settings()`` DIRECTLY (not via Depends), so we point it at the tmp workspace by
monkeypatching ``app.main.get_settings`` — the same pattern the WS auth tests use.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main_mod
from app.config import get_settings
from app.tools.probe import _discover_charts

# A 1x1 PNG (smallest valid image) so FileResponse serves real bytes.
_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000154a24f600000000049454e44ae426082"
)


def _make_run(sessions_root: Path, sid: str) -> Path:
    """Build <sessions>/<sid>/<run>/{results,analysis} with a report + two chart PNGs.
    Returns the report path."""
    run = sessions_root / sid / "tal-run-1"
    results = run / "results" / "infperf_1"
    analysis = run / "analysis" / "infperf_1"
    results.mkdir(parents=True)
    analysis.mkdir(parents=True)
    report = results / "benchmark_report_v0.2,_stage_0.json.yaml"
    report.write_text("schema: v0.2\n")
    (analysis / "latency_vs_qps.png").write_bytes(_PNG_BYTES)
    (analysis / "throughput_vs_latency.png").write_bytes(_PNG_BYTES)
    (analysis / "notes.txt").write_text("not an image")  # must be ignored
    return report


# ---------------------------------------------------------------------------
# _discover_charts: pure mechanism (no HTTP).
# ---------------------------------------------------------------------------


def test_discover_charts_finds_images_relative_to_session(tmp_path):
    sessions_root = tmp_path / "sessions"
    report = _make_run(sessions_root, "sess123")

    charts = _discover_charts(report, sessions_root)

    paths = sorted(c["path"] for c in charts)
    assert paths == [
        "tal-run-1/analysis/infperf_1/latency_vs_qps.png",
        "tal-run-1/analysis/infperf_1/throughput_vs_latency.png",
    ]
    # Each chart carries its session id + a human title; the .txt is excluded. The title is
    # prefixed with the family subdir under analysis/ (here "infperf_1") so nested families don't
    # collide on bare filenames (Phase 40), and `family` records that subdir explicitly.
    assert all(c["session_id"] == "sess123" for c in charts)
    assert {c["title"] for c in charts} == {
        "Infperf 1: Latency vs qps",
        "Infperf 1: Throughput vs latency",
    }
    assert all(c["family"] == "infperf_1" for c in charts)


def test_discover_charts_empty_when_no_analysis_dir(tmp_path):
    sessions_root = tmp_path / "sessions"
    run = sessions_root / "sess1" / "run"
    run.mkdir(parents=True)
    report = run / "benchmark_report_v0.2.yaml"
    report.write_text("x: 1\n")
    assert _discover_charts(report, sessions_root) == []


def test_discover_charts_empty_for_report_outside_session_workspace(tmp_path):
    """A report located via an explicit results_dir outside the per-session workspace yields
    no charts (can't be addressed by the session-keyed artifact route) rather than erroring."""
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    outside = tmp_path / "elsewhere" / "benchmark_report_v0.2.yaml"
    outside.parent.mkdir(parents=True)
    outside.write_text("x: 1\n")
    assert _discover_charts(outside, sessions_root) == []


# ---------------------------------------------------------------------------
# The serving route over the real app, pointed at a tmp workspace.
# ---------------------------------------------------------------------------


@pytest.fixture
def client_with_workspace(tmp_path, monkeypatch):
    """A TestClient whose app resolves its workspace to tmp_path (so the artifact route serves
    from our fake session tree). Yields (client, sessions_root)."""
    sessions_root = tmp_path / "ws" / "sessions"
    sessions_root.mkdir(parents=True)
    settings = get_settings().model_copy(update={"workspace_dir": tmp_path / "ws"})
    monkeypatch.setattr(main_mod, "get_settings", lambda: settings)
    with TestClient(main_mod.app) as client:
        yield client, sessions_root


def test_artifact_route_serves_png(client_with_workspace):
    client, sessions_root = client_with_workspace
    _make_run(sessions_root, "sessABC")
    r = client.get(
        "/api/sessions/sessABC/artifact",
        params={"path": "tal-run-1/analysis/infperf_1/latency_vs_qps.png"},
    )
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content == _PNG_BYTES


def test_artifact_route_rejects_non_image(client_with_workspace):
    client, sessions_root = client_with_workspace
    _make_run(sessions_root, "sessABC")
    r = client.get(
        "/api/sessions/sessABC/artifact",
        params={"path": "tal-run-1/analysis/infperf_1/notes.txt"},
    )
    assert r.status_code == 404


def test_artifact_route_blocks_path_traversal(client_with_workspace, tmp_path):
    client, sessions_root = client_with_workspace
    _make_run(sessions_root, "sessABC")
    # Plant a secret OUTSIDE the session dir; a ../ path must not reach it.
    secret = tmp_path / "ws" / "secret.png"
    secret.write_bytes(_PNG_BYTES)
    r = client.get(
        "/api/sessions/sessABC/artifact",
        params={"path": "../../secret.png"},
    )
    assert r.status_code == 404


def test_artifact_route_404_for_missing_file(client_with_workspace):
    client, sessions_root = client_with_workspace
    _make_run(sessions_root, "sessABC")
    r = client.get(
        "/api/sessions/sessABC/artifact",
        params={"path": "tal-run-1/analysis/infperf_1/nope.png"},
    )
    assert r.status_code == 404


def test_artifact_route_404_for_unknown_session(client_with_workspace):
    client, _ = client_with_workspace
    r = client.get(
        "/api/sessions/does-not-exist/artifact",
        params={"path": "a/b.png"},
    )
    assert r.status_code == 404
