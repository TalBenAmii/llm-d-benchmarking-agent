"""Skill-grounding gate (app/tools/skill_gate.py).

De-inlining the kind runbook (knowledge/quickstart_playbook.md is no longer in CORE; it loads via
fetch_key_docs(task="quickstart")) removed the always-on steering that kept the kind demo on
procedure. This gate replaces it: a mutating llmdbenchmark op (at the command chokepoint) and the
plan proposing it (at propose_session_plan) are REFUSED until the op's grounding task is in
ctx.consulted_skills — the per-session ledger fetch_key_docs writes keyed on the task ARG. Spec-
aware: on the kind path (spec cicd/kind) the required task is 'quickstart' regardless of subcommand;
off it, the op's own *_skill. run_shell is intentionally NOT skill-gated (only the chokepoint is).

These tests exercise: the pure block logic, the deploy plan gate (spec-aware), the ledger recording
(even when the docs are absent), and the two enforcement surfaces + the run_shell exemption.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.security.allowlist import MUTATING
from app.tools.context import ToolError
from app.tools.execute import _SUBCOMMANDS
from app.tools.knowledge_access import fetch_key_docs
from app.tools.plan import propose_session_plan
from app.tools.shell import run_shell
from app.tools.skill_gate import _TASK_BY_SUBCOMMAND, plan_skill_gate_block, skill_gate_block
from tests._helpers import _approve_all, _capture_ctx

_KIND_PLAN = {
    "use_case_summary": "tiny chat benchmark",
    "spec": "cicd/kind", "namespace": "llmd-quickstart",
    "harness": "inference-perf", "workload": "sanity_random.yaml",
}


def _sctx(*skills):
    """A minimal stand-in: skill_gate_block only reads ctx.consulted_skills."""
    return SimpleNamespace(consulted_skills=set(skills))


def _dec(*argv):
    """A minimal Decision stand-in: skill_gate_block only reads decision.argv."""
    return SimpleNamespace(argv=list(argv))


def _ungrounded(tmp_path, **kw):
    """A real ToolContext (real allowlist + frozen catalog + CaptureRunner) with an EMPTY
    consulted_skills — undoing the conftest autouse pre-grounding so the gate is live here."""
    ctx, runner = _capture_ctx(tmp_path, **kw)
    ctx.consulted_skills.clear()
    return ctx, runner


# --- pure command-chokepoint gate logic ---------------------------------------------------

def test_kind_op_requires_quickstart_regardless_of_subcommand():
    for sub in ("standup", "smoketest", "run", "teardown", "experiment"):
        dec = _dec("llmdbenchmark", "--spec", "cicd/kind", sub, "-p", "q")
        msg = skill_gate_block(_sctx(), dec)
        assert msg is not None and 'task="quickstart"' in msg, sub
        # once quickstart is consulted, every kind op is allowed
        assert skill_gate_block(_sctx("quickstart"), dec) is None, sub


def test_guide_ops_require_their_own_skill():
    cases = {
        "standup": "deploy_skill",
        "smoketest": "benchmark_skill",
        "run": "benchmark_skill",
        "teardown": "teardown_skill",
        "experiment": "compare_skill",
    }
    for sub, task in cases.items():
        dec = _dec("llmdbenchmark", "--spec", "guides/pd-disaggregation", sub)
        msg = skill_gate_block(_sctx(), dec)
        assert msg is not None and f'task="{task}"' in msg, (sub, task)
        assert skill_gate_block(_sctx(task), dec) is None, (sub, task)


def test_non_operation_subcommands_and_non_llmd_never_gated():
    for sub in ("plan", "results"):
        assert skill_gate_block(_sctx(), _dec("llmdbenchmark", "--spec", "cicd/kind", sub)) is None
    assert skill_gate_block(_sctx(), _dec("git", "clone", "x")) is None
    assert skill_gate_block(_sctx(), _dec("install.sh", "--uv")) is None


def test_equals_form_spec_is_parsed_for_kind():
    dec = _dec("llmdbenchmark", "--spec=cicd/kind", "standup")
    assert 'task="quickstart"' in skill_gate_block(_sctx(), dec)


# --- the deploy plan gate (spec-aware) -----------------------------------------------------

def test_plan_gate_grounds_deploy_spec_aware():
    # A SessionPlan always proposes a DEPLOY: kind path -> quickstart, guide path -> deploy_skill.
    assert 'task="quickstart"' in plan_skill_gate_block(_sctx(), spec="cicd/kind")
    assert 'task="deploy_skill"' in plan_skill_gate_block(_sctx(), spec="guides/pd-disaggregation")
    # consulted -> allowed
    assert plan_skill_gate_block(_sctx("quickstart"), spec="cicd/kind") is None
    assert plan_skill_gate_block(_sctx("deploy_skill"), spec="guides/pd-disaggregation") is None


# --- the ledger is populated on the task ARG, regardless of read success -------------------

def test_fetch_key_docs_records_consulted_task_even_when_docs_absent(tmp_path):
    # Empty fake repos -> the llm-d-skills docs won't resolve, but the task must STILL be recorded
    # (the gate keys on the fetch CALL, not read success — mirrors the live eval's premise).
    ctx, _ = _ungrounded(tmp_path)
    out = fetch_key_docs(ctx, task="deploy_skill")
    assert "deploy_skill" in ctx.consulted_skills
    assert out["docs"] and all(not d["found"] for d in out["docs"])  # skill docs absent (empty repos)


# --- enforcement at the command chokepoint (execute_llmdbenchmark path) --------------------

async def test_command_gate_blocks_kind_standup_until_grounded(tmp_path):
    ctx, runner = _ungrounded(tmp_path, approve=_approve_all)
    argv = ["llmdbenchmark", "--spec", "cicd/kind", "standup", "-p", "q", "--skip-smoketest"]
    with pytest.raises(ToolError) as ei:
        await ctx.run_command(argv)
    assert "skill-grounding gate" in str(ei.value)
    assert runner.calls == []  # blocked before execution
    ctx.consulted_skills.add("quickstart")
    await ctx.run_command(argv)
    assert len(runner.calls) == 1


async def test_command_gate_blocks_guide_standup_until_deploy_skill(tmp_path):
    ctx, runner = _ungrounded(tmp_path, approve=_approve_all)
    # A guide standup validates against the frozen catalog spec 'guides/workload-autoscaling'.
    argv = ["llmdbenchmark", "--spec", "guides/workload-autoscaling", "standup",
            "-p", "q", "--skip-smoketest"]
    with pytest.raises(ToolError) as ei:
        await ctx.run_command(argv)
    assert "deploy_skill" in str(ei.value)
    assert runner.calls == []
    ctx.consulted_skills.add("deploy_skill")
    await ctx.run_command(argv)
    assert len(runner.calls) == 1


# --- run_shell is intentionally NOT skill-gated -------------------------------------------

async def test_run_shell_is_not_skill_gated(tmp_path):
    # A kind standup via the ad-hoc shell must NOT be skill-gated (only the command chokepoint is);
    # with consulted_skills empty the chokepoint WOULD block, but run_shell bypasses it and runs.
    ctx, runner = _ungrounded(tmp_path, approve=_approve_all)
    await run_shell(ctx, command="llmdbenchmark --spec cicd/kind standup -p q")
    assert len(runner.calls) == 1


# --- the early friendly plan gate (propose_session_plan) ----------------------------------

async def test_plan_gate_blocks_kind_plan_until_quickstart(tmp_path):
    ctx, _ = _ungrounded(tmp_path, approve=_approve_all)
    with pytest.raises(ToolError) as ei:
        await propose_session_plan(ctx, **_KIND_PLAN)
    assert "skill-grounding gate" in str(ei.value)
    ctx.consulted_skills.add("quickstart")
    out = await propose_session_plan(ctx, **_KIND_PLAN)
    assert out["approved"] is True


# --- denylist completeness: the gate map covers every MUTATING subcommand --------------------

def test_gate_covers_every_mutating_subcommand(allowlist):
    """A new mutating subcommand added to execute.py::_SUBCOMMANDS but forgotten in
    _TASK_BY_SUBCOMMAND would run ungrounded (the command chokepoint only gates keys in this map).
    Derive the mutating subset PROGRAMMATICALLY from the allowlist's per-subcommand `mode` (matching
    the executor's own conservative default of MUTATING for an unclassified subcommand) and assert
    the gate map covers it. plan/results are read_only, so they are legitimately absent."""
    subs = allowlist.executable("llmdbenchmark")["subcommands"]
    mutating = {name for name in _SUBCOMMANDS if subs.get(name, {}).get("mode", MUTATING) == MUTATING}
    # Sanity-guard the derivation: an allowlist shape change that silently emptied `mutating` would
    # make the >= below trivially pass. Pin the known mutating set the gate exists to cover.
    assert mutating == {"standup", "smoketest", "run", "teardown", "experiment"}
    assert set(_TASK_BY_SUBCOMMAND) >= mutating
