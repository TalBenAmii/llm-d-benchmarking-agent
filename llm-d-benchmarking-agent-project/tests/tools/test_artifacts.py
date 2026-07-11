"""Per-run chart artifacts: discovery (locate_and_parse_report's `charts`) + the read-only
serving route (``GET /api/sessions/{sid}/artifact``).

Hermetic: a fake session tree under a tmp workspace, no cluster/network. The serving route
reads ``get_settings()`` DIRECTLY (not via Depends), so we point it at the tmp workspace by
monkeypatching ``app.main.get_settings`` — the same pattern the WS auth tests use.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main_mod
from app.config import get_settings
from app.tools.analyze.report_locate import _discover_charts

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


def test_artifact_route_404_for_overlong_ids(client_with_workspace):
    """Regression: an over-long `sid`/`path` component (> NAME_MAX → OSError ENAMETOOLONG) or an
    embedded NUL byte (`%00` → ValueError "embedded null byte") must 404, NOT 500."""
    client, sessions_root = client_with_workspace
    _make_run(sessions_root, "sessABC")
    # Over-long sid → is_dir() on sessions_root/<sid> raises ENAMETOOLONG.
    assert client.get(
        "/api/sessions/" + "a" * 2000 + "/artifact", params={"path": "x.png"}
    ).status_code == 404
    # Real session + over-long path → is_file() on the resolved candidate raises ENAMETOOLONG.
    assert client.get(
        "/api/sessions/sessABC/artifact", params={"path": "a" * 2000 + ".png"}
    ).status_code == 404
    # Embedded NUL byte in the path → ValueError on resolve(); must also 404, not 500.
    assert client.get(
        "/api/sessions/sessABC/artifact", params={"path": "a\x00b.png"}
    ).status_code == 404


# ---------------------------------------------------------------------------
# Reproducibility — the provenance-bundle JSON + report-card.html routes.
# Same tmp-workspace TestClient pattern; the bundle JSON is planted under
# <sessions>/<sid>/bundles/<bundle_id>.json (where BundleStore writes it).
# ---------------------------------------------------------------------------


def _plant_bundle(sessions_root: Path, sid: str, bundle_id: str = "bundle0123abcd") -> dict:
    bundles = sessions_root / sid / "bundles"
    bundles.mkdir(parents=True, exist_ok=True)
    bundle = {
        "bundle_id": bundle_id,
        "created_at": 1_700_000_000.0,
        "model": "meta-llama/Llama-3.1-8B",
        "agent_version": "0.1.0",
        "harness": "inference-perf",
        "spec": "cicd/kind",
        "repos": {
            "llm-d": {"sha": "abcd123", "dirty": False, "ref": "main"},
            "llm-d-benchmark": {"sha": "ef99887", "dirty": False, "ref": "main"},
        },
        "resolved_config": {"found": True, "path": "/ws/run-config.yaml", "body": "spec: cicd/kind\n"},
        "report_summary": {"model": "meta-llama/Llama-3.1-8B", "harness": "inference-perf"},
        "report_digest": "deadbeef", "knowledge_version": "cafef00d",
        "regenerate_command": "llmdbenchmark run -c /ws/run-config.yaml -p ns1",
        "dirty": False,
    }
    (bundles / f"{bundle_id}.json").write_text(json.dumps(bundle))
    return bundle


def test_bundle_json_route_returns_metadata(client_with_workspace):
    client, sessions_root = client_with_workspace
    _plant_bundle(sessions_root, "sessZ")
    r = client.get("/api/sessions/sessZ/bundle/bundle0123abcd")
    assert r.status_code == 200
    body = r.json()
    assert body["bundle_id"] == "bundle0123abcd"
    assert body["regenerate_command"].startswith("llmdbenchmark run -c")


def test_bundle_report_card_is_html_attachment(client_with_workspace):
    client, sessions_root = client_with_workspace
    _plant_bundle(sessions_root, "sessZ")
    r = client.get("/api/sessions/sessZ/bundle/bundle0123abcd/report-card.html")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "attachment" in r.headers.get("content-disposition", "")
    assert "abcd123" in r.text and "meta-llama/Llama-3.1-8B" in r.text
    # Self-contained — no external asset links.
    assert "http://" not in r.text and "https://" not in r.text


def test_bundle_route_404_for_unknown_bundle(client_with_workspace):
    client, sessions_root = client_with_workspace
    _plant_bundle(sessions_root, "sessZ")
    assert client.get("/api/sessions/sessZ/bundle/doesnotexist").status_code == 404
    assert client.get("/api/sessions/sessZ/bundle/doesnotexist/report-card.html").status_code == 404


def test_bundle_route_404_for_unknown_session(client_with_workspace):
    client, _ = client_with_workspace
    assert client.get("/api/sessions/ghost/bundle/bundle0123abcd").status_code == 404


def test_bundle_route_blocks_traversal_in_sid_and_bundle_id(client_with_workspace, tmp_path):
    client, sessions_root = client_with_workspace
    _plant_bundle(sessions_root, "sessZ")
    # Plant a secret bundle OUTSIDE the session dir; traversal must not reach it.
    secret_dir = tmp_path / "ws" / "bundles"
    secret_dir.mkdir(parents=True, exist_ok=True)
    (secret_dir / "secret.json").write_text('{"bundle_id": "secret"}')
    # Traversal in the bundle_id (rejected by _safe_id) and the sid (rejected by base.parent check).
    assert client.get("/api/sessions/sessZ/bundle/..%2f..%2fsecret").status_code in (404, 400)
    assert client.get("/api/sessions/..%2f..%2fsecret/bundle/bundle0123abcd").status_code in (404, 400)


def test_bundle_route_404_for_overlong_sid(client_with_workspace):
    """Regression: an over-long `sid` makes is_dir() raise OSError(ENAMETOOLONG); both the JSON and
    the report-card.html bundle routes must 404, NOT 500."""
    client, _ = client_with_workspace
    long_sid = "a" * 2000
    assert client.get(f"/api/sessions/{long_sid}/bundle/x").status_code == 404
    assert client.get(f"/api/sessions/{long_sid}/bundle/x/report-card.html").status_code == 404
