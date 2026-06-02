"""Hermetic tests for Phase 18 — workspace retention/GC + startup self-check.

Pure filesystem + tmp dirs + a fake clock (via controlled mtimes). No network, no cluster, no
GPU. We seed fake session/run/jobs/history items with controlled mtimes and sizes, then assert
the GC prunes EXACTLY per policy and preserves a marked-active session; and that the self-check
returns the expected STRUCTURED status for a good and a broken config.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.config import Settings
from app.storage.retention import (
    MANAGED_AREAS,
    RetentionCaps,
    readiness,
    run_gc,
    self_check,
)

NOW = 1_700_000_000.0  # fixed reference "now" (fake clock)
DAY = 86400.0


# ---------------------------------------------------------------------------
# helpers — seed scratch with controlled mtime + size
# ---------------------------------------------------------------------------
def _settings(tmp_path: Path, **overrides) -> Settings:
    """A Settings pinned to a tmp workspace + tmp repos, ignoring the real .env."""
    return Settings(
        _env_file=None,
        repos_dir=tmp_path / "repos",
        workspace_dir=tmp_path / "ws",
        **overrides,
    )


def _set_mtime(path: Path, mtime: float) -> None:
    os.utime(path, (mtime, mtime))


def _seed_session(ws: Path, sid: str, *, age_days: float, size: int = 100) -> Path:
    d = ws / "sessions" / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text("x" * size)
    _set_mtime(d, NOW - age_days * DAY)
    return d


def _seed_run(ws: Path, rid: str, *, age_days: float, size: int = 100) -> Path:
    d = ws / "runs" / rid
    d.mkdir(parents=True, exist_ok=True)
    (d / "out.log").write_text("x" * size)
    _set_mtime(d, NOW - age_days * DAY)
    return d


def _seed_history(ws: Path, hid: str, *, age_days: float, size: int = 100) -> Path:
    d = ws / "history"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{hid}.json"
    f.write_text(json.dumps({"summary": {}, "pad": "x" * size}))
    _set_mtime(f, NOW - age_days * DAY)
    return f


def _seed_job(ws: Path, rid: str, *, age_days: float, size: int = 100) -> Path:
    """Mirror what app/orchestrator/controller.py writes: workspace/jobs/<run_id>.yaml (a FILE)."""
    d = ws / "jobs"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{rid}.yaml"
    f.write_text("kind: Job\n# " + "x" * size + "\n")
    _set_mtime(f, NOW - age_days * DAY)
    return f


def _names(ws: Path, area: str, kind: str = "dir") -> set[str]:
    base = ws / area
    if not base.is_dir():
        return set()
    if kind == "dir":
        return {p.name for p in base.iterdir() if p.is_dir()}
    return {p.stem for p in base.iterdir() if p.is_file()}


# ---------------------------------------------------------------------------
# GC: max_items
# ---------------------------------------------------------------------------
def test_gc_max_items_removes_oldest_only(tmp_path):
    ws = tmp_path / "ws"
    # 5 sessions, ages 1..5 days; keep newest 2 -> the 3 oldest go.
    for i in range(5):
        _seed_session(ws, f"s{i}", age_days=float(i + 1))
    s = _settings(tmp_path, retention_max_items=2)

    res = run_gc(s, active_session_ids=set(), now=NOW)

    survivors = _names(ws, "sessions")
    assert survivors == {"s0", "s1"}  # newest two (1 and 2 days old)
    area = next(a for a in res.areas if a.area == "sessions")
    assert set(area.removed) == {"s2", "s3", "s4"}
    assert area.kept == 2


def test_gc_max_items_unlimited_when_zero(tmp_path):
    ws = tmp_path / "ws"
    for i in range(4):
        _seed_session(ws, f"s{i}", age_days=float(i + 1))
    s = _settings(tmp_path, retention_max_items=0)  # 0 == unlimited

    res = run_gc(s, active_session_ids=set(), now=NOW)

    assert _names(ws, "sessions") == {"s0", "s1", "s2", "s3"}
    assert res.total_removed == 0


# ---------------------------------------------------------------------------
# GC: max_age_days
# ---------------------------------------------------------------------------
def test_gc_max_age_prunes_old_keeps_recent(tmp_path):
    ws = tmp_path / "ws"
    _seed_session(ws, "fresh", age_days=1.0)
    _seed_session(ws, "edge", age_days=10.0)     # exactly the cap -> NOT older-than -> kept
    _seed_session(ws, "stale", age_days=10.1)    # strictly older -> pruned
    _seed_session(ws, "ancient", age_days=99.0)  # pruned
    s = _settings(tmp_path, retention_max_age_days=10.0, retention_max_items=0)

    run_gc(s, active_session_ids=set(), now=NOW)

    assert _names(ws, "sessions") == {"fresh", "edge"}


# ---------------------------------------------------------------------------
# GC: max_bytes
# ---------------------------------------------------------------------------
def test_gc_max_bytes_removes_oldest_until_under_cap(tmp_path):
    ws = tmp_path / "ws"
    # Each session is ~1000 bytes of payload; cap survivors under 2500 bytes.
    for i in range(4):
        _seed_session(ws, f"s{i}", age_days=float(i + 1), size=1000)
    s = _settings(tmp_path, retention_max_bytes=2500, retention_max_items=0)

    run_gc(s, active_session_ids=set(), now=NOW)

    survivors = _names(ws, "sessions")
    # Newest survive; oldest removed until under cap. 4*~1000 -> must drop the 2 oldest to fit.
    assert "s0" in survivors and "s1" in survivors
    assert "s3" not in survivors  # oldest gone
    assert len(survivors) <= 2


# ---------------------------------------------------------------------------
# GC: active-session safety — NEVER prune a running/live session
# ---------------------------------------------------------------------------
def test_gc_never_prunes_active_session(tmp_path):
    ws = tmp_path / "ws"
    # The OLDEST and LARGEST session is the active one — every cap would target it first.
    _seed_session(ws, "active", age_days=999.0, size=10_000)
    for i in range(5):
        _seed_session(ws, f"s{i}", age_days=float(i + 1), size=100)
    s = _settings(
        tmp_path,
        retention_max_items=1,
        retention_max_age_days=1.0,
        retention_max_bytes=200,
    )

    res = run_gc(s, active_session_ids={"active"}, now=NOW)

    survivors = _names(ws, "sessions")
    assert "active" in survivors, "an active session must NEVER be pruned"
    assert "active" not in res.areas[0].removed
    area = next(a for a in res.areas if a.area == "sessions")
    assert area.protected_active == 1


# ---------------------------------------------------------------------------
# GC: all areas covered (runs/ + history/ too), independent per area
# ---------------------------------------------------------------------------
def test_gc_covers_runs_and_history_independently(tmp_path):
    ws = tmp_path / "ws"
    for i in range(3):
        _seed_run(ws, f"r{i}", age_days=float(i + 1))
    for i in range(3):
        _seed_history(ws, f"h{i}", age_days=float(i + 1))
    s = _settings(tmp_path, retention_max_items=1)

    res = run_gc(s, active_session_ids=set(), now=NOW)

    assert _names(ws, "runs") == {"r0"}                 # newest run kept
    assert _names(ws, "history", kind="file") == {"h0"}  # newest history record kept
    by_area = {a.area: a for a in res.areas}
    assert len(by_area["runs"].removed) == 2
    assert len(by_area["history"].removed) == 2
    # active-session ids must not affect non-session areas.
    assert by_area["runs"].protected_active == 0


def test_gc_prunes_orchestrator_job_manifests(tmp_path):
    """The orchestrator's per-run scratch is workspace/jobs/<run_id>.yaml — FILES, not dirs.
    GC must enumerate and prune them per policy (regression: a dir/file kind mismatch once made
    the jobs area scan 0 items and prune nothing, leaving per-run scratch to grow unbounded)."""
    ws = tmp_path / "ws"
    # 5 job manifests, ages 1..5 days. Keep newest 1 -> the 4 oldest must go.
    for i in range(5):
        _seed_job(ws, f"run-{i}", age_days=float(i + 1))
    s = _settings(tmp_path, retention_max_items=1)

    res = run_gc(s, active_session_ids=set(), now=NOW)

    survivors = _names(ws, "jobs", kind="file")
    assert survivors == {"run-0"}  # newest manifest (1 day old) kept
    jobs_area = next(a for a in res.areas if a.area == "jobs")
    assert jobs_area.scanned == 5, "the jobs area must actually enumerate the .yaml FILES"
    assert set(jobs_area.removed) == {"run-1", "run-2", "run-3", "run-4"}
    assert jobs_area.kept == 1
    # active-session ids must not protect anything in the jobs area (no live owner).
    assert jobs_area.protected_active == 0


def test_gc_job_manifests_age_and_bytes(tmp_path):
    """Age + bytes caps also apply to the jobs file area, independently and oldest-first."""
    ws = tmp_path / "ws"
    _seed_job(ws, "fresh", age_days=1.0)
    _seed_job(ws, "edge", age_days=7.0)    # exactly the cap -> kept (strict older-than)
    _seed_job(ws, "stale", age_days=7.5)   # strictly older -> pruned
    s = _settings(tmp_path, retention_max_age_days=7.0, retention_max_items=0)

    run_gc(s, active_session_ids=set(), now=NOW)
    assert _names(ws, "jobs", kind="file") == {"fresh", "edge"}

    # Fresh workspace for the bytes cap: ~1000-byte manifests, survivors must fit under 2500.
    ws2 = tmp_path / "ws2"
    s2 = Settings(_env_file=None, repos_dir=tmp_path / "repos", workspace_dir=ws2,
                  retention_max_bytes=2500, retention_max_items=0)
    for i in range(4):
        _seed_job(ws2, f"j{i}", age_days=float(i + 1), size=1000)
    run_gc(s2, active_session_ids=set(), now=NOW)
    survivors = _names(ws2, "jobs", kind="file")
    assert "j0" in survivors and "j3" not in survivors  # newest fit, oldest dropped
    assert len(survivors) <= 2


def test_gc_missing_areas_are_noops(tmp_path):
    # No workspace seeded at all -> GC runs cleanly, removes nothing.
    s = _settings(tmp_path, retention_max_items=1)
    res = run_gc(s, active_session_ids=set(), now=NOW)
    assert res.ran is True
    assert res.total_removed == 0
    assert {a.area for a in res.areas} == {a.name for a in MANAGED_AREAS}


def test_gc_dry_run_reports_without_deleting(tmp_path):
    ws = tmp_path / "ws"
    for i in range(3):
        _seed_session(ws, f"s{i}", age_days=float(i + 1))
    s = _settings(tmp_path, retention_max_items=1)

    res = run_gc(s, active_session_ids=set(), now=NOW, dry_run=True)

    # Reported as removable, but nothing actually deleted.
    area = next(a for a in res.areas if a.area == "sessions")
    assert set(area.removed) == {"s1", "s2"}
    assert _names(ws, "sessions") == {"s0", "s1", "s2"}


def test_caps_from_settings_normalizes_zero_to_unlimited(tmp_path):
    s = _settings(tmp_path, retention_max_age_days=0, retention_max_items=0, retention_max_bytes=0)
    caps = RetentionCaps.from_settings(s)
    assert caps.max_age_seconds is None
    assert caps.max_items is None
    assert caps.max_bytes is None

    s2 = _settings(tmp_path, retention_max_age_days=2, retention_max_items=3, retention_max_bytes=4096)
    caps2 = RetentionCaps.from_settings(s2)
    assert caps2.max_age_seconds == pytest.approx(2 * DAY)
    assert caps2.max_items == 3
    assert caps2.max_bytes == 4096


# ===========================================================================
# Startup self-check
# ===========================================================================
def _make_repos(tmp_path: Path) -> Path:
    """Create the two read-only sibling repo dirs so repos_resolvable passes."""
    repos = tmp_path / "repos"
    (repos / "llm-d").mkdir(parents=True, exist_ok=True)
    (repos / "llm-d-benchmark").mkdir(parents=True, exist_ok=True)
    return repos


def test_self_check_good_config_passes(tmp_path):
    _make_repos(tmp_path)
    s = _settings(tmp_path, llm_provider="anthropic", anthropic_api_key="sk-test")

    res = self_check(s)

    assert res.ok is True
    assert res.failures == []
    names = {c.name for c in res.checks}
    # Phase 16 added the runner_ok component (the allowlist policy loads) alongside the
    # Phase-18 checks; the shipped allowlist loads, so a good config still passes overall.
    assert names == {"workspace_writable", "provider_coherent", "repos_resolvable",
                     "runner_ok", "auth_coherent"}
    # The structured payload carries per-check booleans + reasons.
    js = res.to_json()
    assert js["ok"] is True
    assert js["reasons"] == []
    assert all(c["ok"] for c in js["checks"])


def test_self_check_missing_provider_key_fails(tmp_path):
    _make_repos(tmp_path)
    s = _settings(tmp_path, llm_provider="anthropic", anthropic_api_key=None)

    res = self_check(s)

    assert res.ok is False
    failing = {c.name for c in res.failures}
    assert "provider_coherent" in failing
    # Structured reason names the missing key.
    reason = next(c.detail for c in res.checks if c.name == "provider_coherent")
    assert "ANTHROPIC_API_KEY" in reason


def test_self_check_unknown_provider_fails(tmp_path):
    _make_repos(tmp_path)
    s = _settings(tmp_path, llm_provider="bogus", anthropic_api_key="sk-test")
    res = self_check(s)
    assert res.ok is False
    assert "provider_coherent" in {c.name for c in res.failures}


def test_self_check_missing_repos_fails(tmp_path):
    # repos_dir points at a non-existent tree -> repos_resolvable fails.
    s = _settings(tmp_path, llm_provider="anthropic", anthropic_api_key="sk-test")
    res = self_check(s)
    assert res.ok is False
    failing = {c.name for c in res.failures}
    assert "repos_resolvable" in failing
    repos_check = next(c for c in res.checks if c.name == "repos_resolvable")
    assert "llm-d-benchmark" in repos_check.detail or "llm-d" in repos_check.detail


def test_self_check_auth_enabled_without_token_fails(tmp_path):
    _make_repos(tmp_path)
    s = _settings(
        tmp_path,
        llm_provider="anthropic",
        anthropic_api_key="sk-test",
        auth_enabled=True,
        auth_token="",
    )
    res = self_check(s)
    assert res.ok is False
    assert "auth_coherent" in {c.name for c in res.failures}


def test_self_check_workspace_unwritable_fails(tmp_path):
    _make_repos(tmp_path)
    # Point the workspace at a path under a file (cannot be created as a dir).
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")
    s = Settings(
        _env_file=None,
        repos_dir=tmp_path / "repos",
        workspace_dir=blocker / "ws",
        llm_provider="anthropic",
        anthropic_api_key="sk-test",
    )
    res = self_check(s)
    assert res.ok is False
    assert "workspace_writable" in {c.name for c in res.failures}


# ---------------------------------------------------------------------------
# readiness() contribution (the /readyz seam)
# ---------------------------------------------------------------------------
def test_readiness_reflects_self_check(tmp_path):
    _make_repos(tmp_path)
    good = _settings(tmp_path, llm_provider="anthropic", anthropic_api_key="sk-test")
    contrib = readiness(good)
    assert contrib["ready"] is True
    assert contrib["self_check"]["ok"] is True

    bad = _settings(tmp_path, llm_provider="anthropic", anthropic_api_key=None)
    contrib_bad = readiness(bad)
    assert contrib_bad["ready"] is False
    assert "provider_coherent" in contrib_bad["self_check"]["failures"]


def test_readiness_skipped_when_self_check_disabled(tmp_path):
    # Even with a broken config, a disabled self-check reports ready (operator opted out).
    s = _settings(
        tmp_path,
        llm_provider="anthropic",
        anthropic_api_key=None,
        startup_self_check=False,
    )
    contrib = readiness(s)
    assert contrib["ready"] is True
    assert contrib["self_check"]["skipped"] is True
