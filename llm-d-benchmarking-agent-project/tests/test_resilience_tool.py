"""The run_resilience_drill agent tool — end-to-end through dispatch + a ToolContext, no cluster.

Double-gate proof: refuses with a ToolError (surfaced as {"error": ...} via dispatch) unless
chaos_enabled; when enabled, returns a resilience report dict with the right keys. The drill
drives the UNMODIFIED retry/dead-letter + reconstruct path against an in-process driver — it
never touches a real cluster (the CaptureRunner records zero kubectl calls).
"""
from __future__ import annotations

import pytest

from app.config import Settings
from app.security.allowlist import Allowlist
from app.tools.context import ToolContext, ToolError
from app.tools.registry import dispatch
from app.tools.resilience import run_resilience_drill
from tests.flows.harness import CaptureRunner


def _ctx(tmp_path, *, chaos_enabled: bool):
    settings = Settings(_env_file=None, repos_dir=tmp_path / "repos",
                        workspace_dir=tmp_path / "ws", chaos_enabled=chaos_enabled)

    async def approve(kind, payload):
        return True

    runner = CaptureRunner(settings.repo_paths)
    ws = settings.resolved_workspace_dir / "sessions" / "s1"
    ws.mkdir(parents=True, exist_ok=True)
    ctx = ToolContext(
        settings=settings, allowlist=Allowlist.from_file(settings.allowlist_path),
        runner=runner, workspace=ws, request_approval=approve,
    )
    return ctx, runner


_PLAN = {"seed": 7, "injections": [
    {"kind": "evicted", "at_attempt": 1},
    {"kind": "oom", "at_attempt": 2},
]}


async def test_drill_refused_when_chaos_disabled(tmp_path):
    """The production guard: chaos_enabled=False ⇒ ToolError, and nothing touches a cluster."""
    ctx, runner = _ctx(tmp_path, chaos_enabled=False)
    with pytest.raises(ToolError):
        await run_resilience_drill(ctx, namespace="bench", chaos_plan=_PLAN)
    assert runner.calls == []           # refused before any command ran


async def test_dispatch_propagates_guard_tool_error(tmp_path):
    """Through dispatch, the guard refusal is a ToolError (the agent loop turns it into a clean
    {"error": ...} — dispatch itself only catches schema ValidationErrors)."""
    ctx, runner = _ctx(tmp_path, chaos_enabled=False)
    with pytest.raises(ToolError):
        await dispatch(ctx, "run_resilience_drill", {"namespace": "bench", "chaos_plan": _PLAN})
    assert runner.calls == []


async def test_drill_runs_when_enabled_and_returns_report(tmp_path):
    """chaos_enabled=True ⇒ a resilience report with the right keys; evicted retries to success,
    oom dead-letters by design, and the restart proof shows 0 duplicate Jobs."""
    ctx, runner = _ctx(tmp_path, chaos_enabled=True)
    res = await dispatch(ctx, "run_resilience_drill", {
        "namespace": "bench", "chaos_plan": _PLAN, "max_attempts": 2, "prove_restart": True,
        "slo_budget_s": 600,
    })
    # Key presence (tool-result dicts are not schema-checked → assert keys per tools/CLAUDE.md).
    for key in ("kind", "run_id", "succeeded", "dead_lettered", "injected", "slo",
                "verdict_counts", "restart"):
        assert key in res, f"missing {key}"
    assert res["kind"] == "resilience"

    injected = {r["injected_kind"]: r for r in res["injected"]}
    assert injected["evicted"]["recovery_action"] == "retry"
    assert injected["evicted"]["classified_correctly"] is True
    assert injected["oom"]["recovery_action"] == "dead-letter"
    assert injected["oom"]["classified_correctly"] is True
    assert all(r["recovered_as_designed"] for r in res["injected"])

    vc = res["verdict_counts"]
    assert vc["faults_injected"] == 2 and vc["classified_correctly"] == 2
    assert res["restart"]["no_duplicates"] is True
    assert res["restart"]["duplicate_applies"] == 0
    assert res["slo"]["met"] is True

    # The drill never issued a real kubectl command (it runs against an in-process driver).
    assert runner.calls == []


async def test_drill_without_restart_proof(tmp_path):
    ctx, _ = _ctx(tmp_path, chaos_enabled=True)
    res = await dispatch(ctx, "run_resilience_drill", {
        "namespace": "bench", "chaos_plan": {"injections": [{"kind": "evicted", "at_attempt": 1}]},
        "max_attempts": 2, "prove_restart": False,
    })
    assert res["restart"] is None
    assert res["verdict_counts"]["restart_survived"] == 0


async def test_bad_chaos_plan_shape_raises_tool_error(tmp_path):
    """A malformed chaos_plan is a self-correctable ToolError (the loop renders it as an error
    dict the agent can fix), not a crash."""
    ctx, _ = _ctx(tmp_path, chaos_enabled=True)
    with pytest.raises(ToolError):
        await dispatch(ctx, "run_resilience_drill", {
            "namespace": "bench", "chaos_plan": {"injections": [{"kind": "not_a_real_fault"}]},
        })


async def test_all_clean_drill_with_no_faults(tmp_path):
    """An empty plan drills a clean run: no injected faults, run succeeds, restart survives."""
    ctx, _ = _ctx(tmp_path, chaos_enabled=True)
    res = await dispatch(ctx, "run_resilience_drill", {"namespace": "bench", "max_attempts": 1})
    assert res["succeeded"] is True
    assert res["injected"] == []
    assert res["verdict_counts"]["faults_injected"] == 0
    assert res["restart"]["no_duplicates"] is True
