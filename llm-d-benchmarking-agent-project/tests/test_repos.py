"""Tests for the repos clone/setup tool (app/tools/setup/repos.py).

Focus: the catalog refresh that ``ensure_repos`` triggers MUST run even when a later repo's
clone is rejected/denied mid-loop — otherwise a successfully-cloned earlier repo is left
invisible behind a STALE (empty) per-context catalog cache, and every downstream tool that
reads ``ctx.catalog()`` without ``refresh`` (plan validation + the allowlist ref_catalog
checks) rejects valid names for the rest of the turn.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import BENCH_REPO_NAME, GUIDE_REPO_NAME, Settings
from app.security.allowlist import Allowlist
from app.security.runner import CommandRunner
from app.tools.context import ApprovalRejected, ToolContext
from app.tools.setup import repos


class _Res:
    def __init__(self, exit_code: int = 0, output: str = "") -> None:
        self.exit_code = exit_code
        self.output = output
        self.timed_out = False


def _ctx(tmp_path: Path) -> ToolContext:
    s = Settings(repos_dir=str(tmp_path), workspace_dir=str(tmp_path / "ws"))
    return ToolContext(
        settings=s,
        allowlist=Allowlist.from_file(s.allowlist_path),
        runner=CommandRunner(s.repo_paths),
        workspace=tmp_path / "ws" / "sessions" / "s1",
    )


def _clone_succeeds(cwd, url: str) -> None:
    """Mimic ``git clone`` creating its target dir (URL basename) with a ``.git``."""
    name = url.rstrip("/").rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    (Path(cwd) / name / ".git").mkdir(parents=True, exist_ok=True)


async def test_catalog_refreshed_even_when_a_later_clone_is_rejected(tmp_path, monkeypatch):
    """REGRESSION: the bench repo clone (first) is approved + succeeds; the guide repo clone
    (second) is REJECTED at the approval gate, so ``ctx.run_command`` raises mid-loop. The
    catalog refresh used to be the function's last (fall-through-only) line, so the exception
    skipped it and left the cache stale (present=False) even though the bench repo is on disk.
    With the fix (refresh in a ``finally``) the cached catalog reflects the new clone.
    """
    ctx = _ctx(tmp_path)
    # Prime the per-context cache to the pre-clone state (repos absent → present=False).
    assert ctx.catalog()["present"] is False

    calls = {"n": 0}

    async def fake_run(argv, *, cwd=None, timeout=None, **kw):
        calls["n"] += 1
        if calls["n"] == 1:  # first repo (bench): approved + succeeds
            _clone_succeeds(cwd, argv[2])
            return _Res(0)
        raise ApprovalRejected(list(argv))  # second repo (guide): user rejects

    monkeypatch.setattr(ctx, "run_command", fake_run)

    # Both repos requested, bench first (= _KNOWN_REPOS order).
    with pytest.raises(ApprovalRejected):
        await repos.ensure_repos(ctx, repos=[BENCH_REPO_NAME, GUIDE_REPO_NAME])

    # The bench repo really was cloned to disk...
    assert (ctx.settings.bench_repo / ".git").exists()
    # ...and the per-context catalog cache (read WITHOUT refresh, like plan/allowlist do) now
    # reflects it, instead of the stale empty pre-clone snapshot.
    assert ctx.catalog()["present"] is True


async def test_successful_clone_refreshes_catalog(tmp_path, monkeypatch):
    """Sanity: the normal (no-exception) path still refreshes the catalog cache."""
    ctx = _ctx(tmp_path)
    assert ctx.catalog()["present"] is False

    async def fake_run(argv, *, cwd=None, timeout=None, **kw):
        _clone_succeeds(cwd, argv[2])
        return _Res(0)

    monkeypatch.setattr(ctx, "run_command", fake_run)
    out = await repos.ensure_repos(ctx, repos=[BENCH_REPO_NAME])
    assert [r["action"] for r in out["results"]] == ["cloned"]
    assert ctx.catalog()["present"] is True
