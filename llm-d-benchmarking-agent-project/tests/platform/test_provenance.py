"""Reproducibility — the provenance capture mechanism (app/storage/provenance.py).

Hermetic: real BR v0.2 report fixtures, a real tmp git repo for SHA/dirty capture (created with
the local git binary), and a fake read-only runner so capture_repo_state runs no policy gate
here. No cluster, no GPU, no network.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from app.storage.provenance import (
    BundleStore,
    InvalidReportError,
    ProvenanceBundle,
    _safe_id,
    build_bundle,
    capture_repo_state,
    knowledge_hash,
    provenance_view,
    regenerate_command,
)
from app.validation.report import load_report, summarize_report, validate_report

# When the suite runs INSIDE a git hook (e.g. the main-branch pre-commit gate), git exports
# GIT_DIR / GIT_INDEX_FILE / … into the environment. The tests below shell out to the real git
# binary against a throwaway tmp repo; an inherited GIT_INDEX_FILE would make their `git add` stage
# into the REAL index — corrupting the very commit that triggered the hook. Scrub every GIT_* var so
# these subprocesses stay hermetic regardless of how the suite was launched.
_HERMETIC_GIT_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


# ---- helpers ---------------------------------------------------------------


class _FakeResult:
    def __init__(self, ok: bool, stdout: str = "", exit_code: int = 0):
        self.ok = ok
        self.stdout = stdout
        self.exit_code = exit_code


def _fake_run_readonly(responses: dict[tuple[str, ...], _FakeResult]):
    """A fake ctx.run_readonly: matches an argv prefix to a canned result; cwd is accepted +
    ignored (the mapping already encodes the repo). Unknown argv → ok=False."""
    async def run(argv, *, cwd=None, timeout=None, quiet=False):
        for prefix, res in responses.items():
            if tuple(argv[: len(prefix)]) == prefix:
                return res
        return _FakeResult(False)
    return run


def _valid_summary(br_example: Path) -> dict:
    report = load_report(br_example)
    from tests.conftest import BR_SCHEMA

    v = validate_report(report, BR_SCHEMA)
    assert v.valid
    return summarize_report(report)


# ---- knowledge_hash --------------------------------------------------------


def test_knowledge_hash_is_deterministic_and_content_sensitive(tmp_path):
    kdir = tmp_path / "knowledge"
    kdir.mkdir()
    (kdir / "a.md").write_text("# A\nhello")
    (kdir / "b.yaml").write_text("k: v")
    h1 = knowledge_hash(kdir)
    h2 = knowledge_hash(kdir)
    assert h1 == h2 and len(h1) == 64
    # Editing a file changes the hash.
    (kdir / "a.md").write_text("# A\nhello world")
    assert knowledge_hash(kdir) != h1
    # Meta docs (CLAUDE.md / README.md) are excluded — adding one does NOT change the hash.
    h3 = knowledge_hash(kdir)
    (kdir / "CLAUDE.md").write_text("meta")
    (kdir / "README.md").write_text("meta")
    assert knowledge_hash(kdir) == h3


def test_knowledge_hash_missing_dir_is_stable(tmp_path):
    # A missing dir hashes the empty set rather than crashing.
    assert knowledge_hash(tmp_path / "nope") == knowledge_hash(tmp_path / "also-nope")


# ---- capture_repo_state ----------------------------------------------------


async def test_capture_repo_state_clean_then_dirty(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=_HERMETIC_GIT_ENV)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True, env=_HERMETIC_GIT_ENV)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True, env=_HERMETIC_GIT_ENV)
    (repo / "f.txt").write_text("one")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=_HERMETIC_GIT_ENV)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, env=_HERMETIC_GIT_ENV)

    real_sha = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=repo, capture_output=True, text=True,
        check=True, env=_HERMETIC_GIT_ENV,
    ).stdout.strip()

    # A real read-only runner that shells out with the given cwd (mirrors the tool's path).
    async def run(argv, *, cwd=None, timeout=None, quiet=False):
        r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, env=_HERMETIC_GIT_ENV)
        return _FakeResult(r.returncode == 0, r.stdout, r.returncode)

    clean = await capture_repo_state(repo, run)
    assert clean["sha"] == real_sha and clean["dirty"] is False
    assert "unavailable" not in clean

    # Touch a tracked file → dirty.
    (repo / "f.txt").write_text("two")
    dirty = await capture_repo_state(repo, run)
    assert dirty["sha"] == real_sha and dirty["dirty"] is True


async def test_capture_repo_state_missing_repo_is_unavailable_never_raises(tmp_path):
    # An absent / non-git dir degrades to unavailable, and never invokes the runner.
    async def run(argv, *, cwd=None, timeout=None, quiet=False):
        raise AssertionError("runner must not be called for a missing repo")

    out = await capture_repo_state(tmp_path / "ghost", run)
    assert out == {"sha": None, "dirty": None, "unavailable": True}
    # An empty (no .git) dir is also unavailable.
    empty = tmp_path / "empty"
    empty.mkdir()
    assert (await capture_repo_state(empty, run))["unavailable"] is True


async def test_capture_repo_state_git_error_degrades(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)  # looks like a repo but git will fail

    async def run(argv, *, cwd=None, timeout=None, quiet=False):
        return _FakeResult(False)  # rev-parse fails

    out = await capture_repo_state(repo, run)
    assert out == {"sha": None, "dirty": None, "unavailable": True}


# ---- regenerate_command ----------------------------------------------------


def test_regenerate_command_shape():
    cmd = regenerate_command("/ws/run-config.yaml", "ns1")
    assert cmd == "llmdbenchmark run -c /ws/run-config.yaml -p ns1"
    # Honest placeholder when no config was captured.
    assert "generate-config" in regenerate_command(None, "ns1")


# ---- build_bundle ----------------------------------------------------------


def test_build_bundle_has_every_field_and_stable_digest(br_example):
    summary = _valid_summary(br_example)
    report_bytes = br_example.read_bytes()
    repos = {
        "llm-d": {"sha": "aaa111", "dirty": False, "ref": "main"},
        "llm-d-benchmark": {"sha": "bbb222", "dirty": True, "ref": "main"},
    }
    cfg = {"found": True, "path": "/ws/run-config.yaml", "body": "spec: cicd/kind\n"}
    b = build_bundle(
        report_bytes=report_bytes, report_summary=summary, report_valid=True,
        report_path=str(br_example), repos=repos, resolved_config=cfg,
        agent_version="0.1.0", knowledge_version="kh", spec="cicd/kind",
        harness="inference-perf", workload="sanity_random.yaml", namespace="ns1",
        model="m", slo={"ttft_ms": 200}, env_snapshot={"x": 1}, label="baseline",
    )
    assert isinstance(b, ProvenanceBundle)
    # Every §2 field present and populated.
    assert b.bundle_id and len(b.bundle_id) == 16
    assert b.created_at > 0 and b.agent_version == "0.1.0" and b.knowledge_version == "kh"
    assert b.repos == repos and b.resolved_config == cfg
    assert b.spec == "cicd/kind" and b.namespace == "ns1" and b.slo == {"ttft_ms": 200}
    assert b.env_snapshot == {"x": 1} and b.report_summary == summary
    assert b.report_digest and b.regenerate_command.startswith("llmdbenchmark run -c")
    # dirty rolls up from either repo.
    assert b.dirty is True
    # report_digest is stable for the same bytes+summary.
    b2 = build_bundle(
        report_bytes=report_bytes, report_summary=summary, report_valid=True,
        report_path=str(br_example), repos=repos, resolved_config=cfg,
        agent_version="0.1.0", knowledge_version="kh",
    )
    assert b2.report_digest == b.report_digest
    # to_json round-trips through JSON.
    assert json.loads(json.dumps(b.to_json()))["bundle_id"] == b.bundle_id


def test_build_bundle_refuses_invalid_report(br_example):
    summary = _valid_summary(br_example)
    with pytest.raises(InvalidReportError):
        build_bundle(
            report_bytes=b"x", report_summary=summary, report_valid=False,
            report_path=None, repos={}, resolved_config={},
            agent_version="0", knowledge_version="0",
        )


def test_build_bundle_id_is_content_addressed(br_example):
    summary = _valid_summary(br_example)
    rb = br_example.read_bytes()
    base = dict(report_bytes=rb, report_summary=summary, report_valid=True,
                resolved_config={}, agent_version="0", knowledge_version="0")
    repos_a = {"llm-d": {"sha": "a"}}
    repos_b = {"llm-d": {"sha": "b"}}
    id1 = build_bundle(report_path="/r", repos=repos_a, **base).bundle_id
    id1b = build_bundle(report_path="/r", repos=repos_a, **base).bundle_id
    id2 = build_bundle(report_path="/r", repos=repos_b, **base).bundle_id  # different SHA
    id3 = build_bundle(report_path="/other", repos=repos_a, **base).bundle_id  # different path
    assert id1 == id1b          # deterministic
    assert id1 != id2 and id1 != id3  # SHA- and path-sensitive


def test_build_bundle_id_no_collision_for_different_runs_at_same_path(tmp_path):
    """Two GENUINELY DIFFERENT validated runs (different report bytes + summary, hence different
    report_digest) with NO run_uid, written to the SAME report_path and same repo SHAs, must get
    DISTINCT bundle ids — otherwise the BundleStore silently OVERWRITES the first bundle with the
    second, collapsing two different runs onto one provenance node.

    Reproduces the collision: run_uid is optional (report.run.uid may be absent), and the old id
    basis was only {run_uid, report_path, repo_shas} — so with run_uid=None and a reused path the
    two ids were identical despite differing report content.
    """
    repos = {
        "llm-d": {"sha": "abc1234", "dirty": False},
        "llm-d-benchmark": {"sha": "def5678", "dirty": False},
    }
    common = dict(
        report_valid=True, report_path="/runs/latest/benchmark_report_v0.2.yaml",
        repos=repos, resolved_config={}, agent_version="1", knowledge_version="k",
    )
    # No "run_uid" key in either summary -> run_uid is None for both.
    b1 = build_bundle(
        report_bytes=b"RUN-ONE-bytes",
        report_summary={"harness": "x", "throughput": {"output_token_rate": {"mean": 100}}},
        **common,
    )
    b2 = build_bundle(
        report_bytes=b"RUN-TWO-bytes",
        report_summary={"harness": "x", "throughput": {"output_token_rate": {"mean": 999}}},
        **common,
    )
    # Different content -> different digest -> must be different ids.
    assert b1.report_digest != b2.report_digest
    assert b1.bundle_id != b2.bundle_id, "different runs collided onto one provenance bundle id"

    # And the store must keep BOTH bundles, not overwrite the first.
    store = BundleStore(tmp_path / "ws")
    store.write(b1)
    store.write(b2)
    ids = {b["bundle_id"] for b in store.list()}
    assert ids == {b1.bundle_id, b2.bundle_id}


# ---- BundleStore -----------------------------------------------------------


def test_bundle_store_round_trip(tmp_path, br_example):
    summary = _valid_summary(br_example)
    b = build_bundle(
        report_bytes=br_example.read_bytes(), report_summary=summary, report_valid=True,
        report_path=str(br_example), repos={"llm-d": {"sha": "x"}},
        resolved_config={}, agent_version="0.1.0", knowledge_version="kh",
    )
    store = BundleStore(tmp_path / "ws")
    path = store.write(b)
    assert path.exists() and path.parent.name == "bundles"
    got = store.read(b.bundle_id)
    assert got is not None and got["bundle_id"] == b.bundle_id
    assert got["report_digest"] == b.report_digest
    # list() surfaces it.
    assert [x["bundle_id"] for x in store.list()] == [b.bundle_id]


def test_bundle_store_rejects_traversal(tmp_path):
    store = BundleStore(tmp_path / "ws")
    assert store.read("../../etc/passwd") is None
    assert store.read("a/b") is None
    with pytest.raises(ValueError):
        store.write({"bundle_id": "../evil", "created_at": 1.0})


def test_bundle_store_skips_corrupt_file(tmp_path):
    store = BundleStore(tmp_path / "ws")
    store.dir.mkdir(parents=True, exist_ok=True)
    (store.dir / "garbage.json").write_text("{not json")
    (store.dir / "wrongshape.json").write_text('{"no": "bundle_id"}')
    assert store.read("garbage") is None
    assert store.read("wrongshape") is None
    assert store.list() == []


def test_safe_id_guard():
    assert _safe_id("abc123")
    assert not _safe_id("../x") and not _safe_id("a/b") and not _safe_id("") and not _safe_id(None)


def test_provenance_view_is_compact(br_example):
    summary = _valid_summary(br_example)
    b = build_bundle(
        report_bytes=br_example.read_bytes(), report_summary=summary, report_valid=True,
        report_path=str(br_example), repos={"llm-d": {"sha": "x", "dirty": False}},
        resolved_config={}, agent_version="0.1.0", knowledge_version="kh",
    ).to_json()
    pv = provenance_view(b)
    assert pv["bundle_id"] == b["bundle_id"]
    assert pv["regenerate_command"] == b["regenerate_command"]
    assert "report_summary" not in pv  # the heavy body is NOT carried onto the record


# ---- a fully-faked end-to-end capture_repo_state for both repos -------------


async def test_capture_repo_state_with_canned_runner():
    run = _fake_run_readonly({
        ("git", "rev-parse", "--short", "HEAD"): _FakeResult(True, "deadbee\n"),
        ("git", "status", "--porcelain"): _FakeResult(True, " M file.py\n"),
    })
    # A dir that looks like a git repo so we get past the cheap .git existence check.
    import tempfile

    d = Path(tempfile.mkdtemp())
    (d / ".git").mkdir()
    out = await capture_repo_state(d, run)
    assert out == {"sha": "deadbee", "dirty": True}


def test_list_survives_non_numeric_created_at(tmp_path):
    """BUG-021: a bundle whose on-disk ``created_at`` is a truthy non-number (a forged/corrupt
    string) must not crash ``list()`` for EVERY bundle. The old ``b.get('created_at') or 0.0`` key
    only neutralized falsy values, so a string still raised ``TypeError: '<' not supported between
    str and float`` and broke the whole bundle list. The corrupt bundle stays listed, sorted as
    oldest (coerced to 0.0)."""
    bdir = tmp_path / "bundles"
    bdir.mkdir()
    (bdir / "aaaaaaaa.json").write_text(json.dumps({"bundle_id": "ignored", "created_at": 5.0}))
    (bdir / "bbbbbbbb.json").write_text(json.dumps({"bundle_id": "ignored", "created_at": "NOPE"}))
    bundles = BundleStore(tmp_path).list()  # must not raise
    ids = [b["bundle_id"] for b in bundles]  # read() overrides id with the path stem
    assert set(ids) == {"aaaaaaaa", "bbbbbbbb"}
    assert ids[0] == "aaaaaaaa"  # numeric 5.0 first; corrupt (-> 0.0) sorts last
