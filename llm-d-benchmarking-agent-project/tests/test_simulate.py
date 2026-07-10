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
from app.security.allowlist import MUTATING, READ_ONLY, Allowlist
from app.security.runner import CommandRunner, RunResult, SimRunner
from app.tools.analyze.report_locate import locate_and_parse_report
from app.tools.context import ApprovalRejected, ToolContext
from app.tools.run.shell import run_shell
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
# A representative READ-ONLY command (allowlisted; probe.py runs exactly this).
GET_NODES = ["kubectl", "get", "nodes", "-o", "json"]


class _SpyRunner(CommandRunner):
    """A runner that EMULATES the real executor (``runs_real_subprocess = True``) so the SIMULATE
    caller-gate engages, but records every command it is asked to run and returns a sentinel
    stdout instead of spawning a process. Lets a test prove that under SIMULATE a READ-ONLY
    command actually reaches the runner (real context) while a MUTATING one is no-opped before it."""

    runs_real_subprocess = True

    def __init__(self) -> None:
        super().__init__({})
        self.calls: list[list[str]] = []

    async def execute(self, logical_argv, entry, *, on_line=None, timeout=None, cwd=None, extra_env=None):
        self.calls.append(list(logical_argv))
        return RunResult(exit_code=0, duration_s=0.1, real_argv=list(logical_argv),
                         cwd=None, output="SENTINEL", lines=["SENTINEL"])


async def test_simulate_runs_readonly_command_for_real(tmp_path):
    """THE fix: under SIMULATE a READ-ONLY allowlisted command must actually reach the runner so
    the agent gathers genuine context — it is NOT no-opped to empty (the old all-no-op behaviour)."""
    spy = _SpyRunner()
    ctx = _ctx(tmp_path, simulate=True, runner=spy)
    decision = ctx.allowlist.validate(GET_NODES, catalog=ctx.catalog_for_allowlist())
    assert decision.allowed and decision.mode == READ_ONLY  # sanity: really read-only
    res = await ctx.run_readonly(GET_NODES)
    assert spy.calls == [GET_NODES]      # it RAN
    assert res.output == "SENTINEL"      # real output reached the agent, not empty


async def test_simulate_noops_mutating_command_before_real_runner(tmp_path):
    """Under SIMULATE a MUTATING command is announced but NEVER reaches a real runner — synthetic
    no-op, empty output, no approval prompt, nothing mutated."""
    approve = AsyncMock(return_value=True)
    spy = _SpyRunner()
    ctx = _ctx(tmp_path, simulate=True, runner=spy, request_approval=approve)
    decision = ctx.allowlist.validate(STANDUP, catalog=ctx.catalog_for_allowlist())
    assert decision.allowed and decision.mode == MUTATING  # sanity: really mutating
    res = await ctx.run_command(STANDUP)
    assert spy.calls == []               # the mutation never reached the runner
    assert res.exit_code == 0 and res.output == ""
    approve.assert_not_awaited()         # the per-command gate is still skipped in simulate


async def test_simulate_run_shell_runs_readonly_grep_for_real(tmp_path):
    """THE bug report: under SIMULATE, an ad-hoc read-only ``grep``/``ls``/``cat`` must run for
    real so the agent can gather context — not return empty."""
    spy = _SpyRunner()
    ctx = _ctx(tmp_path, simulate=True, runner=spy)
    out = await run_shell(ctx, command="grep -r needle /some/path")
    assert spy.calls == [["bash", "-lc", "grep -r needle /some/path"]]  # it RAN
    assert out["mode"] == READ_ONLY and out["stdout_tail"] == "SENTINEL"


async def test_simulate_run_shell_noops_mutating_command(tmp_path):
    """Under SIMULATE an ad-hoc MUTATING shell command is announced but never executed."""
    spy = _SpyRunner()
    ctx = _ctx(tmp_path, simulate=True, runner=spy)
    out = await run_shell(ctx, command="kubectl apply -f deploy.yaml")
    assert spy.calls == []               # never reached the runner
    assert out["mode"] == MUTATING and out["stdout_tail"] == "" and out["exit_code"] == 0


async def test_simulate_command_event_badges_only_noopped_commands(tmp_path):
    """The emitted `command` event's `simulated` flag (→ the UI "SIMULATED" badge) marks a command
    that was a no-op PREVIEW (mutating-under-SIMULATE). A read-only command runs for real under
    SIMULATE, so it must NOT be badged — else the UI mislabels a genuinely-executed probe/grep."""
    events: list[tuple[str, dict]] = []

    async def emit(kind, payload):
        events.append((kind, payload))

    spy = _SpyRunner()
    ctx = _ctx(tmp_path, simulate=True, runner=spy)
    ctx.emit = emit
    await ctx.run_readonly(GET_NODES)                              # read-only → really runs
    await ctx.run_command(STANDUP)                                 # mutating → no-op preview
    await run_shell(ctx, command="grep -r needle /x")             # read-only → really runs
    await run_shell(ctx, command="kubectl apply -f deploy.yaml")  # mutating → no-op preview

    badge = {tuple(p["argv"]): p["simulated"] for (k, p) in events if k == "command"}
    assert badge[tuple(GET_NODES)] is False                          # real read-only — not badged
    assert badge[("bash", "-lc", "grep -r needle /x")] is False      # real grep — not badged
    assert badge[tuple(STANDUP)] is True                             # no-opped mutation — badged
    assert badge[("bash", "-lc", "kubectl apply -f deploy.yaml")] is True


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


def test_simulate_note_carries_honesty_cues(tmp_path):
    """The SIMULATE honesty floor must be ACTIVE whenever SIMULATE is on. Under the new model the
    floor has two halves: (a) read-only probes RUN FOR REAL, so their output IS real host state and
    should be trusted/reported; (b) the OUTCOME of a simulated mutation (deployed stack, results)
    must never be presented as real. Both are inlined into SIMULATE_NOTE (config-stable, so the
    prompt-cache prefix is undisturbed) with a pointer to the full guide. Guard them so a future
    edit can't silently strip them back out."""
    on_prompt = build_system_prompt(_ctx(tmp_path, simulate=True, runner=SimRunner({})))
    off_prompt = build_system_prompt(_ctx(tmp_path, simulate=False, runner=SimRunner({})))
    low = on_prompt.lower()
    # (a) read-only commands run for real → trust the genuine output as real host state
    assert "run for real" in low
    assert "real host state" in low
    # (b) a simulated mutation's outcome is synthetic — never narrate it as deployed/benchmarked
    assert "nothing was actually deployed or benchmarked" in low
    # and a pointer to the full rule so the model can load the rest on demand
    assert "knowledge/reference/sim_integration.md" in on_prompt
    # entirely absent when SIMULATE is off (it rides on SIMULATE_NOTE)
    assert "real host state" not in off_prompt.lower()
    assert "knowledge/reference/sim_integration.md" not in off_prompt


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
    from app.tools.setup.probe import probe_environment

    # 1) Raw contract: a SimRunner no-op carries no captured stdout (matches CaptureRunner /
    #    a real command with empty output), so output-parsing consumers see nothing to parse.
    res = await SimRunner({}).execute(["kind", "get", "clusters"], None)
    assert res.exit_code == 0
    assert res.output == ""
    assert res.lines == []

    # 2) End-to-end: the kind-clusters probe must NOT invent clusters in SIMULATE mode.
    monkeypatch.setattr("app.tools.setup.probe.shutil.which", lambda name: "/usr/bin/" + name)
    ctx = _ctx(tmp_path, simulate=True, runner=SimRunner({}))
    out = await probe_environment(ctx, checks=["kind_clusters"])
    assert out["kind_clusters"]["clusters"] == []
