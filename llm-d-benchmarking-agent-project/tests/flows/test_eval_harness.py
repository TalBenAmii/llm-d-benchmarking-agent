"""Hermetic guards for the LIVE-eval harness extensions (token-budget work).

These pin the two pieces the live/simulate eval gained for the phase-group lazy-loading feature
WITHOUT spending any quota — they run in plain ``pytest`` like every other hermetic test:

  1. The per-call fail-fast WATCHDOG: a hung LLM call must surface as a clean error fast (the
     flow fails and the suite moves on) instead of stalling to the 300s test backstop.
  2. The ``load_tools`` group-loading SCORING dimension in ``score_flow``: the live model must
     load the RIGHT tool group(s); loading an extra one is allowed (a NOTE), never loading a
     needed one is a failure, and a loaded group with no ``load_tools`` call is a mechanism bug.

``score_flow`` is the live-eval scorer (the deterministic gate never calls it), so without these
its new logic would only ever be exercised when actually spending quota — exactly what we don't
want to depend on. We drive it here over real ``FlowRun`` objects produced by the hermetic harness.
"""
from __future__ import annotations

import asyncio
from typing import Any

from app.llm.provider import AssistantTurn, LLMProvider, ToolCall
from app.security.allowlist import MUTATING, READ_ONLY
from app.tools.registry import _group_of

from .flows import Flow
from .harness import gating_problems, kill_wedged_sdk_subprocesses, run_flow, score_flow


def _flow(**kw: Any) -> Flow:
    """A throwaway Flow for harness-level tests. Built locally (NOT added to ALL_FLOWS), so the
    parametrized gate/coverage tests never see it; only the fields a test sets matter here."""
    params: dict[str, Any] = dict(
        name="harness-probe", title="t", description="t", mock_user_input="x", turns=[])
    params.update(kw)
    return Flow(**params)


async def _trivial_run(tmp_path):
    """Run a do-nothing scripted flow through the real harness to obtain a genuine ``FlowRun``
    (ended cleanly, no commands/errors) whose ``session.loaded_groups`` / ``tool_calls`` we then
    set to model what a live model did — so we score the REAL ``score_flow`` over a REAL run."""
    flow = _flow(turns=[AssistantTurn(text="ok", tool_calls=[])])
    return await run_flow(flow, tmp_path=tmp_path)


# ---- 1) the per-call fail-fast watchdog -----------------------------------------------------

class _HangingProvider(LLMProvider):
    """A provider whose every call never returns — stands in for a wedged network / CLI."""

    async def chat(self, *, system, messages, tools, cache_key=None) -> AssistantTurn:
        await asyncio.sleep(3600)  # far longer than any test would wait
        raise AssertionError("unreachable — the watchdog must cancel this first")


async def test_watchdog_fails_a_hung_llm_call_fast_instead_of_hanging(tmp_path):
    # A real (non-None) provider is supplied, so run_flow wraps it in the per-call watchdog. With
    # a 0.2s deadline the hung call is cancelled almost immediately; if the watchdog were absent
    # this test would itself hang (and only the 300s mark would eventually fail it).
    run = await run_flow(
        _flow(mock_user_input="do something useful"),
        tmp_path=tmp_path,
        provider=_HangingProvider(),
        call_timeout=0.2,
    )
    assert run.errors, "a hung LLM call must surface as a clean error, not hang the flow"
    assert any("LLM call failed" in e for e in run.errors), run.errors
    assert run.ended_done, "the loop should still end cleanly (DONE) after the timeout"
    assert not run.commands, "nothing should have executed once the very first call timed out"


async def test_kill_helper_terminates_only_a_matching_descendant():
    """The watchdog's force-kill (kill_wedged_sdk_subprocesses) must reach a wedged SDK subprocess
    yet be doubly scoped — descendants-only AND marker-matched — so it can never hit a co-running
    live app's CLI subprocess. Verify both halves with a throwaway `sleep` child: a non-matching
    marker leaves it alone; a matching one kills it."""
    proc = await asyncio.create_subprocess_exec(
        "sleep", "30",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        # Non-matching marker → no kill (selectivity: must not signal an unrelated descendant).
        assert kill_wedged_sdk_subprocesses(markers=("marker-not-in-any-cmdline-xyz",)) == 0
        assert proc.returncode is None, "a non-matching marker must not kill the descendant"
        # Matching marker (its cmdline contains 'sleep') → killed, unblocking the 'await'.
        assert kill_wedged_sdk_subprocesses(markers=("sleep",)) >= 1
        await asyncio.wait_for(proc.wait(), timeout=5)
        assert proc.returncode is not None and proc.returncode != 0
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def test_watchdog_is_disabled_when_timeout_non_positive(tmp_path):
    # call_timeout <= 0 disables wrapping; a normal (fast) scripted-style provider still completes.
    class _OkProvider(LLMProvider):
        async def chat(self, *, system, messages, tools, cache_key=None) -> AssistantTurn:
            return AssistantTurn(text="done", tool_calls=[])

    run = await run_flow(_flow(), tmp_path=tmp_path, provider=_OkProvider(), call_timeout=0)
    assert run.ended_done and not run.errors


# ---- 2) the load_tools group-loading scoring dimension --------------------------------------

async def test_score_passes_when_the_needed_group_was_loaded(tmp_path):
    run = await _trivial_run(tmp_path)
    run.session.loaded_groups = {"analyze"}          # analyze_results lives in the 'analyze' group
    # The realistic shape: the model loads the group, THEN calls the grouped tool (score_flow also
    # checks required_tools against the actual calls, so the tool call must be present for a PASS).
    run.tool_calls = [
        {"name": "load_tools", "input": {"groups": ["analyze"]}},
        {"name": "analyze_results", "input": {}},
    ]
    ok, notes = score_flow(run, _flow(required_tools=["analyze_results"]))
    assert ok, notes
    assert any("loaded the needed group(s) ['analyze']" in n for n in notes), notes


async def test_score_fails_when_a_needed_group_was_never_loaded(tmp_path):
    run = await _trivial_run(tmp_path)
    run.session.loaded_groups = set()                # the model never reached the group
    run.tool_calls = []
    ok, notes = score_flow(run, _flow(required_tools=["analyze_results"]))
    assert not ok
    assert any("never loaded tool group(s) ['analyze']" in n for n in notes), notes


async def test_score_allows_an_extra_group_but_notes_it(tmp_path):
    run = await _trivial_run(tmp_path)
    run.session.loaded_groups = {"analyze", "run"}   # 'run' was not needed for analyze_results
    run.tool_calls = [
        {"name": "load_tools", "input": {"groups": ["analyze", "run"]}},
        {"name": "analyze_results", "input": {}},
    ]
    ok, notes = score_flow(run, _flow(required_tools=["analyze_results"]))
    assert ok, notes  # extras are allowed per the chosen policy ("right group, extras allowed")
    assert any("also loaded unneeded group(s) ['run']" in n for n in notes), notes


async def test_score_flags_loaded_groups_with_no_load_tools_call(tmp_path):
    # Mechanism integrity: a group can only become loaded via load_tools. A run that shows a
    # loaded group but no load_tools call is a regression (a grouped tool leaked into the kit).
    run = await _trivial_run(tmp_path)
    run.session.loaded_groups = {"advanced"}
    run.tool_calls = []                              # nothing called load_tools
    ok, notes = score_flow(run, _flow(required_tools=[]))   # no required tools → only integrity fires
    assert not ok
    assert any("load_tools was never called" in n for n in notes), notes


def test_group_dimension_matches_the_real_registry_groups():
    # Guards the assumption the tests above bake in: analyze_results is in 'analyze', and a
    # starter-kit tool maps to no group — so needed_groups derivation is meaningful.
    assert _group_of("analyze_results") == "analyze"
    assert _group_of("load_tools") is None


# ---- 3) run_shell is classified by the shell classifier, not flagged as an allowlist bypass --

async def test_read_only_run_shell_is_not_flagged_as_an_allowlist_bypass(tmp_path):
    """A real model often improvises with run_shell (the ad-hoc `bash -lc` surface). It is governed
    by the read-only/mutating classifier, NOT the allowlist — so a read-only run_shell must be
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
    it is still never mislabeled a 'denied' allowlist bypass."""
    flow = _flow(turns=[
        AssistantTurn(text="", tool_calls=[ToolCall(id="s1", name="run_shell", input={"command": "rm -rf build"})]),
        AssistantTurn(text="done", tool_calls=[]),
    ])
    run = await run_flow(flow, tmp_path=tmp_path)  # default approve = yes
    muts = [c for c in run.commands if c.argv[:2] == ["bash", "-lc"]]
    assert muts and all(c.mode == MUTATING for c in muts), [c.mode for c in muts]
    assert all(c.approved for c in muts), "a mutating run_shell must pass through the approval gate"
    assert not (g := gating_problems(run)), g
