"""Hermetic guards for the harness pieces only the LIVE/simulate eval exercises.

``score_flow`` is the live-eval scorer (the deterministic gate never calls it), so without
these its logic would only ever be exercised when actually spending quota — exactly what we
don't want to depend on. We drive it here over real ``FlowRun`` objects produced by the
hermetic harness, plus guards for the CannedResult failing-command primitive and the
run_shell classification path.
"""
from __future__ import annotations

from typing import Any

from app.security.policy import MUTATING, READ_ONLY
from tests._scripted import AssistantTurn, ToolCall

from .flows import Flow
from .harness import CannedResult, gating_problems, run_flow, score_flow


def _flow(**kw: Any) -> Flow:
    """A throwaway Flow for harness-level tests. Built locally (NOT added to ALL_FLOWS), so the
    parametrized gate/coverage tests never see it; only the fields a test sets matter here."""
    params: dict[str, Any] = dict(
        name="harness-probe", title="t", description="t", mock_user_input="x", turns=[])
    params.update(kw)
    return Flow(**params)


async def _trivial_run(tmp_path):
    """Run a do-nothing scripted flow through the real harness to obtain a genuine ``FlowRun``
    (ended cleanly, no commands/errors) whose ``tool_calls`` we then set to model what a live
    model did — so we score the REAL ``score_flow`` over a REAL run."""
    flow = _flow(turns=[AssistantTurn(text="ok", tool_calls=[])])
    return await run_flow(flow, tmp_path=tmp_path)


# ---- 1) score_flow's tool-choice scoring ----------------------------------------------------

async def test_score_passes_when_required_tools_were_called(tmp_path):
    run = await _trivial_run(tmp_path)
    run.tool_calls = [{"name": "analyze_results", "input": {}}]
    ok, notes = score_flow(run, _flow(required_tools=["analyze_results"]))
    assert ok, notes
    assert any("called required tool(s)" in n for n in notes), notes


async def test_score_fails_when_a_required_tool_was_never_called(tmp_path):
    run = await _trivial_run(tmp_path)
    run.tool_calls = []
    ok, notes = score_flow(run, _flow(required_tools=["analyze_results"]))
    assert not ok
    assert any("missing required tool call(s)" in n for n in notes), notes


async def test_score_fails_when_a_forbidden_tool_was_called(tmp_path):
    run = await _trivial_run(tmp_path)
    run.tool_calls = [{"name": "orchestrate_sweep", "input": {}}]
    ok, notes = score_flow(run, _flow(forbidden_tools=["orchestrate_sweep"]))
    assert not ok
    assert any("FORBIDDEN tool" in n for n in notes), notes


# ---- 2) the CannedResult failing-command primitive ------------------------------------------

async def test_canned_result_surfaces_a_failing_command_to_the_agent(tmp_path):
    """A ``CannedResult`` needle must reach the agent as the same structured failure production
    would produce (non-zero exit + error output RETURNED, not raised), so error-path flows can
    assert recovery instead of blind success."""
    flow = _flow(
        turns=[
            AssistantTurn(text="", tool_calls=[
                ToolCall(id="s1", name="run_shell", input={"command": "ls -la"})]),
            AssistantTurn(text="done", tool_calls=[]),
        ],
        canned={"ls -la": CannedResult(output="boom: no such dir", exit_code=2)},
    )
    run = await run_flow(flow, tmp_path=tmp_path)
    assert run.ended_done and not run.errors
    result = run.tool_result("run_shell")
    assert result is not None and result.get("exit_code") == 2, result
    assert "boom" in str(result), result


# ---- 3) run_shell is classified by the shell classifier, not flagged as a policy bypass -----

async def test_read_only_run_shell_is_not_flagged_as_a_policy_bypass(tmp_path):
    """A real model often improvises with run_shell (the ad-hoc `bash -lc` surface). It is governed
    by the read-only/mutating classifier, NOT the policy — so a read-only run_shell must be
    classified read_only (auto-run) and must NOT trip gating_problems' 'denied bypass' check (which
    is for DEDICATED tools). This is the false-positive the live eval would otherwise hit."""
    flow = _flow(turns=[
        AssistantTurn(text="", tool_calls=[ToolCall(id="s1", name="run_shell", input={"command": "ls -la"})]),
        AssistantTurn(text="done", tool_calls=[]),
    ])
    run = await run_flow(flow, tmp_path=tmp_path)
    shells = [c for c in run.commands if c.argv[:2] == ["bash", "-lc"]]
    assert shells, "the run_shell command should have been captured"
    assert all(c.mode == READ_ONLY for c in shells), [c.mode for c in shells]
    assert all(not c.approved for c in shells), "a read-only run_shell must auto-run, not be gated"
    assert not (g := gating_problems(run)), g


async def test_mutating_run_shell_is_classified_and_must_be_approval_gated(tmp_path):
    """The flip side: a mutating run_shell is classified mutating and the SAME safety invariant
    applies — it must pass the approval gate. With the default approver (yes) it is gated and clean;
    it is still never mislabeled a 'denied' policy bypass."""
    flow = _flow(turns=[
        AssistantTurn(text="", tool_calls=[ToolCall(id="s1", name="run_shell", input={"command": "rm -rf build"})]),
        AssistantTurn(text="done", tool_calls=[]),
    ])
    run = await run_flow(flow, tmp_path=tmp_path)  # default approve = yes
    muts = [c for c in run.commands if c.argv[:2] == ["bash", "-lc"]]
    assert muts and all(c.mode == MUTATING for c in muts), [c.mode for c in muts]
    assert all(c.approved for c in muts), "a mutating run_shell must pass through the approval gate"
    assert not (g := gating_problems(run)), g
