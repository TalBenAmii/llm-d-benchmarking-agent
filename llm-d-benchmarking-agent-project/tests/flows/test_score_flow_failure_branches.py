"""score_flow's FAILURE branches — the live-eval grading logic that only fires on quota.

The scorer's pass path is exercised by the golden shadow scorer, but its subcommand / spec /
read-only / no-significant FAILURE branches are otherwise untested. Build synthetic FlowRuns
with hand-set run.commands and assert each branch fails with its note (and the matching
pass case succeeds). Hermetic, sibling-independent.
"""
from __future__ import annotations

from app.security.policy import MUTATING
from tests.flows.harness import CapturedCommand, score_flow
from tests.flows.test_eval_harness import _flow, _trivial_run


def _cmd(*argv, mode=MUTATING, approved=True):
    return CapturedCommand(argv=list(argv), mode=mode, approved=approved, cwd=None)


async def test_missing_required_subcommand_fails(tmp_path):
    """A required subcommand the run never issued fails scoring."""
    run = await _trivial_run(tmp_path)
    run.commands = [_cmd("llmdbenchmark", "standup")]
    ok, notes = score_flow(run, _flow(required_subcommands=["run"]), group_scoring=False)
    assert not ok
    assert any("missing required subcommand" in n for n in notes)


async def test_present_required_subcommand_passes(tmp_path):
    """When the required subcommand did run, that dimension passes."""
    run = await _trivial_run(tmp_path)
    run.commands = [_cmd("llmdbenchmark", "run")]
    ok, notes = score_flow(run, _flow(required_subcommands=["run"]), group_scoring=False)
    assert ok, notes


async def test_forbidden_subcommand_fails(tmp_path):
    """A forbidden subcommand that the run issued fails scoring."""
    run = await _trivial_run(tmp_path)
    run.commands = [_cmd("llmdbenchmark", "standup")]
    ok, notes = score_flow(run, _flow(forbidden_subcommands=["standup"]), group_scoring=False)
    assert not ok
    assert any("FORBIDDEN subcommand" in n for n in notes)


async def test_wrong_spec_fails(tmp_path):
    """A run whose --spec differs from required_spec fails scoring."""
    run = await _trivial_run(tmp_path)
    run.commands = [_cmd("llmdbenchmark", "--spec", "cicd/kind", "standup")]
    ok, notes = score_flow(run, _flow(required_spec="guides/optimized-baseline"), group_scoring=False)
    assert not ok
    assert any("expected --spec" in n for n in notes)


async def test_right_spec_passes(tmp_path):
    """A run whose --spec matches required_spec passes that dimension."""
    run = await _trivial_run(tmp_path)
    run.commands = [_cmd("llmdbenchmark", "--spec", "cicd/kind", "standup")]
    ok, notes = score_flow(run, _flow(required_spec="cicd/kind"), group_scoring=False)
    assert ok, notes


async def test_expect_all_readonly_violated_fails(tmp_path):
    """A mutating command fails a flow that expects everything read-only."""
    run = await _trivial_run(tmp_path)
    run.commands = [_cmd("llmdbenchmark", "standup", mode=MUTATING, approved=True)]
    ok, notes = score_flow(run, _flow(expect_all_readonly=True), group_scoring=False)
    assert not ok
    assert any("read-only-only" in n for n in notes)


async def test_expect_no_significant_violated_fails(tmp_path):
    """A significant command fails a flow that expects nothing to run."""
    run = await _trivial_run(tmp_path)
    run.commands = [_cmd("llmdbenchmark", "run", mode=MUTATING, approved=True)]
    ok, notes = score_flow(run, _flow(expect_no_significant=True), group_scoring=False)
    assert not ok
    assert any("expected nothing to run" in n for n in notes)
