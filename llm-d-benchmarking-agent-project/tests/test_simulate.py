"""Hermetic tests for Simulate Mode (SIMULATE=1).

No sibling repos, no network, no API key. Like the flow harness, we shadow the live
catalog with the frozen snapshot so the allowlist's ref-catalog checks behave as in prod
even though the bench repo is absent here.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.agent.prompt import SIMULATE_NOTE, build_system_prompt
from app.config import Settings
from app.security.allowlist import Allowlist
from app.security.runner import CommandRunner, SimRunner
from app.tools.context import ApprovalRejected, ToolContext
from app.tools.probe import locate_and_parse_report
from tests.flows.catalog_snapshot import frozen_catalog


def _settings(tmp_path: Path, *, simulate: bool) -> Settings:
    """Hermetic settings — ignore the developer's .env, point dirs at a temp sandbox."""
    return Settings(
        _env_file=None,
        simulate=simulate,
        repos_dir=tmp_path / "repos",
        workspace_dir=tmp_path / "ws",
    )


def _ctx(tmp_path: Path, *, simulate: bool, runner, request_approval=None) -> ToolContext:
    settings = _settings(tmp_path, simulate=simulate)
    allowlist = Allowlist.from_file(settings.allowlist_path)
    ctx = ToolContext(
        settings=settings,
        allowlist=allowlist,
        runner=runner,
        workspace=tmp_path / "ws" / "sessions" / "sim",
        request_approval=request_approval,
    )
    # Shadow the catalog so allowlist ref-checks work with no repos on disk (see harness).
    frozen = frozen_catalog()
    ctx._catalog = frozen
    ctx.catalog = lambda *, refresh=False: frozen
    return ctx


# A representative mutating command (would prompt for approval outside simulate mode).
STANDUP = ["llmdbenchmark", "--spec", "cicd/kind", "standup", "-p", "ns"]


async def test_simrunner_execute_is_noop_success(tmp_path):
    runner = SimRunner({})
    res = await runner.execute(["llmdbenchmark", "standup"], None)
    assert res.exit_code == 0
    assert res.duration_s == 0.0
    assert res.real_argv == ["llmdbenchmark", "standup"]
    # Captured output is EMPTY — mirrors the canonical test fake (CaptureRunner) and a real
    # command that produced no stdout, so output-parsing consumers don't mistake a synthetic
    # banner for real DATA. The "this was simulated" signal rides the `command` event's
    # `simulated` flag + the SIMULATE_NOTE prompt cue, NOT the captured output text.
    assert res.output == ""
    assert res.lines == []
    # Nothing spawned: a real CommandRunner would have raised resolving the missing venv.


async def test_run_command_simulate_skips_approval(tmp_path):
    approve = AsyncMock(return_value=True)
    ctx = _ctx(tmp_path, simulate=True, runner=SimRunner({}), request_approval=approve)

    # Sanity: this command IS mutating per the real allowlist + frozen catalog.
    decision = ctx.allowlist.validate(STANDUP, catalog=ctx.catalog_for_allowlist())
    assert decision.allowed and decision.requires_approval

    res = await ctx.run_command(STANDUP)
    assert res.exit_code == 0
    approve.assert_not_awaited()  # the per-command approval gate is skipped in simulate


async def test_run_command_real_requires_approval_and_rejects(tmp_path):
    # request_approval returns False → ApprovalRejected before any subprocess runs.
    approve = AsyncMock(return_value=False)
    ctx = _ctx(tmp_path, simulate=False, runner=CommandRunner({}), request_approval=approve)

    with pytest.raises(ApprovalRejected):
        await ctx.run_command(STANDUP)
    approve.assert_awaited_once()  # the gate WAS consulted when not simulating


def test_system_prompt_includes_simulate_note_only_when_on(tmp_path):
    on = _ctx(tmp_path, simulate=True, runner=SimRunner({}))
    off = _ctx(tmp_path, simulate=False, runner=SimRunner({}))
    assert SIMULATE_NOTE in build_system_prompt(on)
    assert "DRY SIMULATION" in build_system_prompt(on)
    assert SIMULATE_NOTE not in build_system_prompt(off)
    assert "DRY SIMULATION" not in build_system_prompt(off)


def test_simulate_note_carries_probe_honesty_cue(tmp_path):
    """D5: the SIMULATE probe-honesty floor (never narrate no-op probe output as confirmed
    real host state) must be ACTIVE whenever SIMULATE is on. Previously this rule lived only
    in knowledge/sim_integration.md, which sits in the on-demand index with no cue to load it
    — so it was missing exactly when it mattered. It is now inlined into SIMULATE_NOTE (which
    is config-stable, so it does not perturb the prompt-cache prefix) with a pointer to the
    full guide. Guard it so a future edit can't silently strip it back out."""
    on_prompt = build_system_prompt(_ctx(tmp_path, simulate=True, runner=SimRunner({})))
    off_prompt = build_system_prompt(_ctx(tmp_path, simulate=False, runner=SimRunner({})))
    low = on_prompt.lower()
    # the honesty floor: no-op probe output is not real host state, no fabricated readiness
    assert "real host state" in low
    assert "readiness" in low
    assert "didn't actually run" in low
    # and a pointer to the full rule so the model can load the rest on demand
    assert "knowledge/sim_integration.md" in on_prompt
    # entirely absent when SIMULATE is off (it rides on SIMULATE_NOTE)
    assert "real host state" not in off_prompt.lower()
    assert "knowledge/sim_integration.md" not in off_prompt


def test_locate_report_synthesizes_in_simulate(tmp_path):
    empty = tmp_path / "results"
    empty.mkdir()

    sim = _ctx(tmp_path, simulate=True, runner=SimRunner({}))
    out = locate_and_parse_report(sim, results_dir=str(empty))
    assert out["found"] is True
    assert out["simulated"] is True
    assert out["valid"] is True
    assert "summary" in out and out["summary"]["requests"] == 120

    real = _ctx(tmp_path, simulate=False, runner=SimRunner({}))
    out2 = locate_and_parse_report(real, results_dir=str(empty))
    assert out2["found"] is False


async def test_simrunner_output_does_not_fabricate_parsed_data(tmp_path, monkeypatch):
    """SimRunner's RESULT contract must mirror the canonical test fake (CaptureRunner) and a
    real command that produced no stdout: an EMPTY ``output``/``lines``. SimRunner used to bake
    two human-readable ``[simulate] …`` lines into the captured output, so any read-only
    consumer that parses ``res.output`` as DATA (not just for display) silently fabricated
    structured results in SIMULATE mode.

    Concrete trigger: ``probe_environment(checks=["kind_clusters"])`` with ``kind`` on PATH.
    ``_probe_kind`` reports every non-"No kind clusters" stdout line as a cluster name, so the
    old SimRunner output turned into phantom clusters
    (``["[simulate] (no-op) would run: kind get clusters", "[simulate] exit_code=0"]``) — a
    success-shaped lie the agent could act on (e.g. "you already have a cluster, skipping
    standup"). The probe-honesty PROMPT cue (D5) can't undo a corrupt structured tool result.
    """
    from app.tools.probe import probe_environment

    # 1) Raw contract: a SimRunner no-op carries no captured stdout (matches CaptureRunner /
    #    a real command with empty output), so output-parsing consumers see nothing to parse.
    res = await SimRunner({}).execute(["kind", "get", "clusters"], None)
    assert res.exit_code == 0
    assert res.output == ""
    assert res.lines == []

    # 2) End-to-end: the kind-clusters probe must NOT invent clusters in SIMULATE mode.
    monkeypatch.setattr("app.tools.probe.shutil.which", lambda name: "/usr/bin/" + name)
    ctx = _ctx(tmp_path, simulate=True, runner=SimRunner({}))
    out = await probe_environment(ctx, checks=["kind_clusters"])
    assert out["kind_clusters"]["clusters"] == []
