"""The universal safety invariant gating_problems() — its two "must never happen" branches.

Every mutating command must be approval-gated (covered elsewhere), but the deny-bypass and
read-only-gating branches of gating_problems() have had zero coverage. These build synthetic
FlowRuns and assert the guard fires AND that score_flow fails as a result. Hermetic,
sibling-independent.
"""
from __future__ import annotations

from app.security.allowlist import MUTATING, READ_ONLY
from tests.flows.harness import CapturedCommand, gating_problems, score_flow
from tests.flows.test_eval_harness import _flow, _trivial_run


async def test_denied_command_reaching_runner_is_flagged(tmp_path):
    """A denied command that reached the runner is an allowlist bypass — flagged + fails scoring."""
    run = await _trivial_run(tmp_path)
    run.commands = [
        CapturedCommand(argv=["kubectl", "delete", "ns", "x"], mode="denied", approved=False, cwd=None)
    ]
    assert any("denied command reached the runner" in p for p in gating_problems(run))
    ok, notes = score_flow(run, _flow(), group_scoring=False)
    assert not ok
    assert any("denied command reached the runner" in n for n in notes)


async def test_readonly_command_through_approval_gate_is_flagged(tmp_path):
    """A read-only command that went through the approval gate should have auto-run — flagged."""
    run = await _trivial_run(tmp_path)
    run.commands = [
        CapturedCommand(argv=["llmdbenchmark", "results"], mode=READ_ONLY, approved=True, cwd=None)
    ]
    assert any("read-only command went through the approval gate" in p for p in gating_problems(run))
    ok, _notes = score_flow(run, _flow(), group_scoring=False)
    assert not ok


async def test_properly_gated_mutating_command_is_clean(tmp_path):
    """A properly approval-gated mutating command raises no gating problem."""
    run = await _trivial_run(tmp_path)
    run.commands = [
        CapturedCommand(argv=["llmdbenchmark", "--spec", "cicd/kind", "standup"],
                        mode=MUTATING, approved=True, cwd=None)
    ]
    assert gating_problems(run) == []


async def test_ungated_mutating_flagged_in_simulate_too(tmp_path):
    """An un-gated mutating command is a violation in BOTH modes. Simulate previews the mutation
    but does not waive its approval card, so it buys no tolerance here — the guardrail is the
    same on both paths."""
    run = await _trivial_run(tmp_path)
    run.commands = [
        CapturedCommand(argv=["llmdbenchmark", "--spec", "cicd/kind", "standup"],
                        mode=MUTATING, approved=False, cwd=None)
    ]
    run.simulate = False
    assert any("NOT approval-gated" in p for p in gating_problems(run))
    run.simulate = True
    assert any("NOT approval-gated" in p for p in gating_problems(run))
