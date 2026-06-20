"""W3 — live resource-stats poller (backend-streamed, ZERO LLM cost).

Reuses the real ToolContext + allowlist (CaptureRunner stands in for the subprocess) from the
observability tests, plus its canned `kubectl top pods` output. Asserts the poller emits parsed
{available:true} samples, emits the {available:false} note exactly once on metrics-server absence,
no-ops in simulate mode and when no emitter is wired, and cleans up its background task on exit.
"""
from __future__ import annotations

import asyncio

from app.observability.resource_poller import _MAX_CONSECUTIVE_FAILURES, resource_stats_poller
from app.security.runner import RunResult
from app.tools.context import ToolError
from tests.test_observability import TOP_PODS, _ctx


def _collector(ctx):
    """Wire ctx.emit to an async collector and return the list it appends (type, payload) to."""
    events: list[tuple[str, dict]] = []

    async def emit(t, p):
        events.append((t, p))

    ctx.emit = emit
    return events


async def _drain(predicate, *, tries=500, step=0.005):
    """Sleep in small real-time slices until `predicate()` holds (so the poller's interval timer
    actually fires) or we give up. step > 0 lets several poll ticks at interval=0.01 elapse."""
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(step)
    raise AssertionError("condition not met within the poll window")


async def test_poller_emits_parsed_rows(tmp_path):
    ctx, runner = _ctx(tmp_path)
    runner._canned = {"top pods": TOP_PODS}
    events = _collector(ctx)

    async with resource_stats_poller(ctx, namespace="bench", run_id="r1-a1", interval=0.01):
        await _drain(lambda: any(p.get("available") for _, p in events))

    stats = [p for t, p in events if t == "resource_stats" and p.get("available")]
    assert stats, "expected at least one available resource_stats event"
    rows = stats[0]["rows"]
    assert stats[0]["namespace"] == "bench"
    assert rows[0]["name"] == "llmd-bench-r1-a1-abc"
    assert rows[0]["cpu(cores)"] == "250m"
    # Scoped to the run via the run-id label selector (quiet read-only call still hits the runner).
    argv = next(c["argv"] for c in runner.calls if c["argv"][:3] == ["kubectl", "top", "pods"])
    assert "-l" in argv and "llmd-bench/run-id=r1-a1" in argv


def _always_fail(runner):
    """Replace the runner's execute with one that records the call and returns exit_code 1
    (metrics-server absent), so the poller sees a persistent failure on every tick."""
    async def fail_top(logical_argv, entry, *, on_line=None, timeout=None, cwd=None, extra_env=None):
        runner.calls.append({"argv": list(logical_argv), "entry": entry, "cwd": None})
        return RunResult(exit_code=1, duration_s=0.0, real_argv=list(logical_argv), cwd=None,
                         output="error: Metrics API not available")

    runner.execute = fail_top  # type: ignore[method-assign]


async def test_poller_announces_unavailable_exactly_once(tmp_path):
    ctx, runner = _ctx(tmp_path)
    events = _collector(ctx)
    _always_fail(runner)

    async with resource_stats_poller(ctx, namespace="bench", interval=0.01):
        # Let several poll ticks elapse so a per-tick (buggy) implementation would emit > 1.
        await _drain(lambda: len(runner.calls) >= _MAX_CONSECUTIVE_FAILURES)

    notes = [p for t, p in events if t == "resource_stats" and p.get("available") is False]
    assert len(notes) == 1, f"expected exactly one unavailable note, got {len(notes)}"
    assert "metrics-server" in notes[0]["note"]


async def test_poller_stops_after_repeated_failures(tmp_path):
    """The fix for the exit_code-1 poll spam: after _MAX_CONSECUTIVE_FAILURES consecutive
    failing `kubectl top` polls the loop STOPS issuing them — it does not keep firing one every
    interval forever (the old announce-once only silenced the UI emit, not the kubectl calls)."""
    ctx, runner = _ctx(tmp_path)
    _collector(ctx)  # wire an emitter so the poller actually spawns (no-ops without one)
    _always_fail(runner)

    async with resource_stats_poller(ctx, namespace="bench", interval=0.001):
        # The loop should give up at exactly _MAX_CONSECUTIVE_FAILURES calls; wait well past the
        # point where a never-stopping poller would have fired many more, then confirm it capped.
        await _drain(lambda: len(runner.calls) >= _MAX_CONSECUTIVE_FAILURES)
        for _ in range(200):
            await asyncio.sleep(0.001)

    top_calls = [c for c in runner.calls if c["argv"][:3] == ["kubectl", "top", "pods"]]
    assert len(top_calls) == _MAX_CONSECUTIVE_FAILURES, (
        f"poller kept firing kubectl top after repeated failures: {len(top_calls)} calls"
    )


async def test_poller_stops_after_repeated_hard_errors(tmp_path):
    """A HARD error per poll (e.g. no cluster reachable → run_readonly raises) also counts as a
    failed tick, so the poller backs off and STOPS rather than spinning the same erroring call."""
    ctx, runner = _ctx(tmp_path)
    _collector(ctx)
    calls = {"n": 0}

    async def boom(logical_argv, entry, *, on_line=None, timeout=None, cwd=None, extra_env=None):
        calls["n"] += 1
        raise ToolError("no cluster reachable")

    runner.execute = boom  # type: ignore[method-assign]

    async with resource_stats_poller(ctx, namespace="bench", interval=0.001):
        await _drain(lambda: calls["n"] >= _MAX_CONSECUTIVE_FAILURES)
        for _ in range(200):
            await asyncio.sleep(0.001)

    assert calls["n"] == _MAX_CONSECUTIVE_FAILURES, (
        f"poller kept retrying a hard-erroring poll: {calls['n']} attempts"
    )


async def test_poller_failure_counter_resets_on_a_good_sample(tmp_path):
    """A successful sample re-arms the give-up counter, so an occasional transient failure does
    NOT stop a poller that is otherwise getting live stats (still show live stats during a run)."""
    ctx, runner = _ctx(tmp_path)
    events = _collector(ctx)
    state = {"n": 0}

    async def flaky(logical_argv, entry, *, on_line=None, timeout=None, cwd=None, extra_env=None):
        state["n"] += 1
        runner.calls.append({"argv": list(logical_argv), "entry": entry, "cwd": None})
        # Fail every other call: never _MAX_CONSECUTIVE_FAILURES failures in a row.
        if state["n"] % 2 == 0:
            return RunResult(exit_code=1, duration_s=0.0, real_argv=list(logical_argv), cwd=None,
                             output="error: Metrics API not available")
        return RunResult(exit_code=0, duration_s=0.0, real_argv=list(logical_argv), cwd=None,
                         output=TOP_PODS)

    runner.execute = flaky  # type: ignore[method-assign]

    async with resource_stats_poller(ctx, namespace="bench", interval=0.001):
        # A poller that wrongly stopped on intermittent failures would never reach this many.
        await _drain(lambda: state["n"] >= _MAX_CONSECUTIVE_FAILURES * 4)

    assert any(p.get("available") for _, p in events), "expected live samples to keep flowing"


async def test_poller_noop_in_simulate(tmp_path):
    ctx, runner = _ctx(tmp_path)
    ctx.settings.simulate = True
    runner._canned = {"top pods": TOP_PODS}
    events = _collector(ctx)

    async with resource_stats_poller(ctx, namespace="bench", interval=0.01):
        for _ in range(50):
            await asyncio.sleep(0)

    assert events == []
    assert not any(c["argv"][:3] == ["kubectl", "top", "pods"] for c in runner.calls)


async def test_poller_noop_when_emit_is_none(tmp_path):
    ctx, runner = _ctx(tmp_path)
    runner._canned = {"top pods": TOP_PODS}
    ctx.emit = None

    async with resource_stats_poller(ctx, namespace="bench", interval=0.01):
        for _ in range(50):
            await asyncio.sleep(0)

    assert not any(c["argv"][:3] == ["kubectl", "top", "pods"] for c in runner.calls)


async def test_poller_carries_dashboard_url_when_configured(tmp_path):
    """G6: a configured GRAFANA_DASHBOARD_URL rides EVERY available tick so the UI can embed the
    user's own llm-d Grafana alongside the agent's kubectl-top view during a run."""
    ctx, runner = _ctx(tmp_path)
    ctx.settings.grafana_dashboard_url = "https://grafana.example/d/llm-d/overview"
    runner._canned = {"top pods": TOP_PODS}
    events = _collector(ctx)

    async with resource_stats_poller(ctx, namespace="bench", interval=0.01):
        await _drain(lambda: any(p.get("available") for _, p in events))

    stats = [p for t, p in events if t == "resource_stats" and p.get("available")]
    assert stats and all(p["dashboard_url"] == "https://grafana.example/d/llm-d/overview" for p in stats)


async def test_poller_dashboard_url_rides_unavailable_tick(tmp_path):
    """G6 is independent of metrics-server: even when `kubectl top` is unavailable, the configured
    dashboard URL still rides the one {available:false} note so the Grafana embed shows regardless."""
    ctx, runner = _ctx(tmp_path)
    ctx.settings.grafana_dashboard_url = "https://grafana.example/d/llm-d/overview"
    _always_fail(runner)
    events = _collector(ctx)

    async with resource_stats_poller(ctx, namespace="bench", interval=0.01):
        await _drain(lambda: any(p.get("available") is False for _, p in events))

    note = next(p for t, p in events if t == "resource_stats" and p.get("available") is False)
    assert note["dashboard_url"] == "https://grafana.example/d/llm-d/overview"


async def test_poller_omits_dashboard_url_when_unconfigured(tmp_path):
    """Default (unset) carries NO dashboard_url key — today's behavior (table/sparklines only).
    Whitespace-only is treated as unset too (stripped by Settings.metrics_dashboard_url)."""
    ctx, runner = _ctx(tmp_path)
    ctx.settings.grafana_dashboard_url = "   "
    runner._canned = {"top pods": TOP_PODS}
    events = _collector(ctx)

    async with resource_stats_poller(ctx, namespace="bench", interval=0.01):
        await _drain(lambda: any(p.get("available") for _, p in events))

    stats = [p for t, p in events if t == "resource_stats" and p.get("available")]
    assert stats and all("dashboard_url" not in p for p in stats)


async def test_poller_task_finishes_after_exit(tmp_path):
    ctx, runner = _ctx(tmp_path)
    runner._canned = {"top pods": TOP_PODS}
    events = _collector(ctx)

    before = set(asyncio.all_tasks())
    async with resource_stats_poller(ctx, namespace="bench", interval=0.01):
        await _drain(lambda: any(p.get("available") for _, p in events))
    # After the context manager exits, no poller task should be left running.
    leftover = [t for t in asyncio.all_tasks() if t not in before and not t.done()]
    assert leftover == [], f"poller task still running after exit: {leftover}"
