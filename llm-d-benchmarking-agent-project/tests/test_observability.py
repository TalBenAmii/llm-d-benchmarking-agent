"""Phase 7 — observability instrumentation, the /metrics endpoint, and observe_run_metrics.

Hermetic: no live cluster, no scrape server. Command instrumentation runs through the REAL
ToolContext + allowlist (CaptureRunner stands in for the subprocess); orchestrator metrics run
through the REAL controller against the FakeKubeClient; the endpoint is hit via FastAPI's
TestClient. Every test isolates the metric registry with use_registry so global state never
leaks between tests or into the process REGISTRY.
"""
from __future__ import annotations

import json

import pytest

from app.config import Settings
from app.observability import instrument
from app.observability.metrics import MetricsRegistry, render_prometheus
from app.security.allowlist import Allowlist
from app.tools.context import ApprovalRejected, ToolContext
from tests.flows.catalog_snapshot import frozen_catalog
from tests.flows.harness import CaptureRunner


# ---- helpers ---------------------------------------------------------------

def _ctx(tmp_path, *, approve=None):
    settings = Settings(_env_file=None, repos_dir=tmp_path / "repos", workspace_dir=tmp_path / "ws")
    runner = CaptureRunner(settings.repo_paths)
    ctx = ToolContext(
        settings=settings,
        allowlist=Allowlist.from_file(settings.allowlist_path),
        runner=runner,
        workspace=tmp_path / "ws" / "sessions" / "s1",
        request_approval=approve,
    )
    frozen = frozen_catalog()
    ctx._catalog = frozen
    ctx.catalog = lambda *, refresh=False: frozen
    return ctx, runner


RO = ["kind", "get", "clusters"]
MUT = ["kind", "create", "cluster", "--name", "test-cluster"]


# ---- command instrumentation (the central ToolContext mechanism point) -----

async def test_readonly_command_records_count_and_duration(tmp_path):
    reg = MetricsRegistry()
    with instrument.use_registry(reg):
        ctx, _ = _ctx(tmp_path)
        await ctx.run_readonly(RO)
    out = render_prometheus(reg)
    assert ('llmdbench_agent_commands_total{auto_run="true",exe="kind",mode="read_only"} 1'
            in out)
    # A duration histogram series was recorded for this exe/mode.
    assert 'llmdbench_agent_command_duration_seconds_count{exe="kind",mode="read_only"} 1' in out


async def test_mutating_command_records_after_approval(tmp_path):
    reg = MetricsRegistry()

    async def approve(kind, payload):
        return True

    with instrument.use_registry(reg):
        ctx, _ = _ctx(tmp_path, approve=approve)
        await ctx.run_command(MUT)
    out = render_prometheus(reg)
    assert ('llmdbench_agent_commands_total{auto_run="false",exe="kind",mode="mutating"} 1'
            in out)


async def test_rejected_command_records_nothing(tmp_path):
    """A command the user declined never ran, so it must not be counted — the metric trail
    equals the executed-command trail (same invariant as the Phase-1 `command` event)."""
    reg = MetricsRegistry()

    async def reject(kind, payload):
        return False

    with instrument.use_registry(reg):
        ctx, _ = _ctx(tmp_path, approve=reject)
        with pytest.raises(ApprovalRejected):
            await ctx.run_command(MUT)
    out = render_prometheus(reg)
    # The metric is registered (HELP/TYPE lines exist) but no command SERIES was recorded —
    # a recorded command would emit a `..._commands_total{...}` value line.
    assert "llmdbench_agent_commands_total{" not in out


async def test_command_count_increments_across_invocations(tmp_path):
    reg = MetricsRegistry()
    with instrument.use_registry(reg):
        ctx, _ = _ctx(tmp_path)
        await ctx.run_readonly(RO)
        await ctx.run_command(RO)   # read-only via run_command too
    out = render_prometheus(reg)
    assert ('llmdbench_agent_commands_total{auto_run="true",exe="kind",mode="read_only"} 2'
            in out)


# ---- orchestrator instrumentation (through the real controller) ------------

def _orch_ctx(tmp_path):
    from tests.orchestrator_fakes import FakeKubeClient  # local import (test-only fakes)
    from app.orchestrator.controller import BenchmarkOrchestrator

    fake = FakeKubeClient()
    orch = BenchmarkOrchestrator(fake, tmp_path / "ws")
    return orch, fake


def _spec(run_id="r1", namespace="bench"):
    from app.orchestrator.job import JobSpec
    return JobSpec(run_id=run_id, namespace=namespace, image="img",
                   command=["llmdbenchmark", "run"])


async def test_successful_run_records_submitted_attempt_and_outcome(tmp_path):
    reg = MetricsRegistry()
    with instrument.use_registry(reg):
        orch, fake = _orch_ctx(tmp_path)
        # -a1 attempt succeeds immediately.
        fake.program("r1-a1", phases=["succeeded"])
        outcome = await orch.run_with_retries(_spec(), max_attempts=1, poll_interval=0)
        assert outcome.succeeded
    out = render_prometheus(reg)
    assert "llmdbench_orchestrator_runs_submitted_total 1" in out
    assert 'llmdbench_orchestrator_run_attempts_total{phase="succeeded"} 1' in out
    assert 'llmdbench_orchestrator_runs_terminal_total{outcome="succeeded"} 1' in out
    # The in-flight gauge returns to 0 after the run completes.
    assert "llmdbench_orchestrator_runs_in_flight 0" in out
    # No fault SERIES recorded for a success (HELP/TYPE lines for the registered metric always
    # appear; a fault would add a `..._faults_total{kind=...}` value line, which must not exist).
    assert "llmdbench_orchestrator_run_faults_total{" not in out


async def test_oom_run_records_dead_letter_and_fault_kind(tmp_path):
    from tests.orchestrator_fakes import make_pod

    reg = MetricsRegistry()
    with instrument.use_registry(reg):
        orch, fake = _orch_ctx(tmp_path)
        fake.program("r1-a1", phases=["failed"],
                     pods=[make_pod("r1-a1", phase="Failed", terminated="OOMKilled", exit_code=137)])
        outcome = await orch.run_with_retries(_spec(), max_attempts=1, poll_interval=0)
        assert outcome.dead_lettered and not outcome.succeeded
    out = render_prometheus(reg)
    assert 'llmdbench_orchestrator_runs_terminal_total{outcome="dead_lettered"} 1' in out
    assert 'llmdbench_orchestrator_run_faults_total{kind="oom"} 1' in out
    assert "llmdbench_orchestrator_runs_in_flight 0" in out


async def test_transient_retry_records_two_submits_and_attempts(tmp_path):
    """An evicted (transient) attempt retries as a fresh Job, then succeeds: two submits, two
    terminal attempts (failed then succeeded), one successful run outcome."""
    from tests.orchestrator_fakes import make_pod
    from app.orchestrator.faults import EVICTED

    reg = MetricsRegistry()
    with instrument.use_registry(reg):
        orch, fake = _orch_ctx(tmp_path)
        # attempt 1: evicted (transient) → retry; attempt 2: succeeds.
        fake.program("r1-a1", phases=["failed"],
                     pods=[make_pod("r1-a1", phase="Failed", reason="Evicted")])
        fake.program("r1-a2", phases=["succeeded"])
        outcome = await orch.run_with_retries(
            _spec(), max_attempts=2, retryable=frozenset({EVICTED}), poll_interval=0)
        assert outcome.succeeded and len(outcome.attempts) == 2
    out = render_prometheus(reg)
    assert "llmdbench_orchestrator_runs_submitted_total 2" in out
    assert 'llmdbench_orchestrator_run_attempts_total{phase="failed"} 1' in out
    assert 'llmdbench_orchestrator_run_attempts_total{phase="succeeded"} 1' in out
    assert 'llmdbench_orchestrator_runs_terminal_total{outcome="succeeded"} 1' in out


async def test_submit_only_counts_submitted_but_no_outcome(tmp_path):
    reg = MetricsRegistry()
    with instrument.use_registry(reg):
        orch, fake = _orch_ctx(tmp_path)
        await orch.submit(_spec())
    out = render_prometheus(reg)
    assert "llmdbench_orchestrator_runs_submitted_total 1" in out
    # submit() alone is not a terminal outcome — no terminal-outcome SERIES line.
    assert "llmdbench_orchestrator_runs_terminal_total{" not in out


# ---- the /metrics endpoint -------------------------------------------------

def test_metrics_endpoint_exposes_prometheus_text(tmp_path):
    from fastapi.testclient import TestClient
    from app.config import get_settings

    if not get_settings().bench_repo.is_dir():
        pytest.skip("repo not present")
    from app.main import app

    # Record one fact into the PROCESS registry the endpoint renders.
    instrument.record_run_submitted()
    with TestClient(app) as client:
        resp = client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "version=0.0.4" in resp.headers["content-type"]
    body = resp.text
    assert "# TYPE llmdbench_orchestrator_runs_submitted_total counter" in body
    assert "# HELP llmdbench_agent_commands_total" in body


# ---- observe_run_metrics tool ----------------------------------------------

TOP_PODS = (
    "NAME                       CPU(cores)   MEMORY(bytes)\n"
    "llmd-bench-r1-a1-abc       250m         512Mi\n"
    "llmd-bench-r1-a1-def       10m          64Mi\n"
)
TOP_NODES = (
    "NAME       CPU(cores)   CPU%   MEMORY(bytes)   MEMORY%\n"
    "kind-cp    900m         45%    3Gi             60%\n"
)


async def test_observe_pods_parses_top_output(tmp_path):
    from app.tools.observe import observe_run_metrics

    ctx, runner = _ctx(tmp_path)
    runner._canned = {"top pods": TOP_PODS}
    res = await observe_run_metrics(ctx, namespace="bench", scope="pods", run_id="r1-a1")
    assert res["available"] is True and res["row_count"] == 2
    assert res["rows"][0]["name"] == "llmd-bench-r1-a1-abc"
    assert res["rows"][0]["cpu(cores)"] == "250m"
    assert res["rows"][0]["memory(bytes)"] == "512Mi"
    # Scoped to the run via the run-id label selector.
    argv = next(c["argv"] for c in runner.calls if c["argv"][:3] == ["kubectl", "top", "pods"])
    assert "-l" in argv and "llmd-bench/run-id=r1-a1" in argv


async def test_observe_nodes_scope(tmp_path):
    from app.tools.observe import observe_run_metrics

    ctx, runner = _ctx(tmp_path)
    runner._canned = {"top nodes": TOP_NODES}
    res = await observe_run_metrics(ctx, namespace="bench", scope="nodes")
    assert res["available"] is True and res["scope"] == "nodes"
    assert res["rows"][0]["name"] == "kind-cp" and res["rows"][0]["memory%"] == "60%"
    argv = next(c["argv"] for c in runner.calls if c["argv"][:3] == ["kubectl", "top", "nodes"])
    assert "-n" not in argv  # node usage is cluster-wide, not namespaced


async def test_observe_handles_metrics_server_absent(tmp_path):
    """If `kubectl top` fails (no metrics-server), the tool reports unavailable read-only —
    never raising, never claiming numbers it doesn't have."""
    from app.security.runner import RunResult
    from app.tools.observe import observe_run_metrics

    ctx, runner = _ctx(tmp_path)

    async def fail_top(logical_argv, entry, *, on_line=None, timeout=None, cwd=None):
        runner.calls.append({"argv": list(logical_argv), "entry": entry, "cwd": None})
        return RunResult(exit_code=1, duration_s=0.0, real_argv=list(logical_argv), cwd=None,
                         output="error: Metrics API not available")

    runner.execute = fail_top  # type: ignore[method-assign]
    res = await observe_run_metrics(ctx, namespace="bench", scope="pods")
    assert res["available"] is False and "metrics-server" in res["note"]
    assert "Metrics API not available" in res["error_tail"]


def test_kubectl_top_is_allowlisted_read_only():
    """The new live-metrics command is DATA-gated (allowlist.yaml), read-only, and constrained:
    only pod/node usage, namespaced, never a mutation."""
    from app.security.allowlist import READ_ONLY

    al = Allowlist.from_file(Settings(_env_file=None).allowlist_path)
    d = al.validate(["kubectl", "top", "pods", "-n", "bench", "-l", "llmd-bench/run-id=r1"])
    assert d.allowed and d.mode == READ_ONLY
    assert al.validate(["kubectl", "top", "nodes"]).allowed
    # Not a free-for-all: an unknown resource is rejected.
    assert not al.validate(["kubectl", "top", "secrets", "-n", "bench"]).allowed


async def test_observe_run_metrics_dispatches_as_a_tool(tmp_path):
    """End-to-end through the registry/dispatch path the agent uses."""
    from app.tools.registry import dispatch, tool_definitions

    assert "observe_run_metrics" in {d["name"] for d in tool_definitions()}
    ctx, runner = _ctx(tmp_path)
    runner._canned = {"top pods": TOP_PODS}
    res = await dispatch(ctx, "observe_run_metrics", {"namespace": "bench", "scope": "pods"})
    assert res["available"] is True and res["row_count"] == 2
