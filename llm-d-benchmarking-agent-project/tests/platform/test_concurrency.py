"""Phase 2 — parallel sessions & parallel benchmark runs.

Three mechanisms:
  * a configurable, cross-session cap on concurrent *heavy* (mutating) executions, so several
    benchmark runs don't thrash the host (read-only probes are never capped);
  * SessionManager wires that shared cap + isolated workspaces into every session;
  * an in-flight (already-approved) run is NOT cancelled when its WebSocket drops, and a turn
    parked at an approval gate is NOT auto-rejected — both survive navigating away / running
    several chats in parallel, and a parked approval re-surfaces when the chat is reopened.
"""
from __future__ import annotations

import asyncio
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.agent.engine import LIVE_TURNS
from app.agent.session import SessionManager
from app.config import Settings, get_settings
from app.security.policy import CommandPolicy
from app.security.runner import CommandRunner, RunResult
from app.tools.context import ToolContext
from tests._scripted import AssistantTurn, ScriptedTransports, ToolCall, sdk_script
from tests.flows.catalog_snapshot import frozen_catalog

RO = ["kind", "get", "clusters"]
MUT_A = ["kind", "create", "cluster", "--name", "cap-a"]
MUT_B = ["kind", "create", "cluster", "--name", "cap-b"]


class _GatedRunner(CommandRunner):
    """Records concurrency; each execute() blocks on an asyncio.Event so the test controls
    when commands 'finish' and can observe how many run at once."""

    def __init__(self, repo_paths, gate: asyncio.Event):
        super().__init__(repo_paths)
        self._gate = gate
        self.active = 0
        self.peak = 0
        self.started = 0  # how many calls actually ENTERED execute (past the semaphore)

    async def execute(self, logical_argv, entry, *, on_line=None, timeout=None, cwd=None, extra_env=None):
        self.started += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await self._gate.wait()
        finally:
            self.active -= 1
        return RunResult(exit_code=0, duration_s=0.0, real_argv=list(logical_argv), cwd=None)


def _ctx(tmp_path, runner, *, sem=None):
    settings = Settings(_env_file=None, repos_dir=tmp_path / "repos", workspace_dir=tmp_path / "ws")

    async def approve(kind, payload):
        return True

    ctx = ToolContext(
        settings=settings,
        policy=CommandPolicy.from_file(settings.command_policy_path),
        runner=runner,
        workspace=tmp_path / "ws",
        request_approval=approve,
        run_semaphore=sem,
    )
    frozen = frozen_catalog()
    ctx._catalog = frozen
    ctx.catalog = lambda *, refresh=False: frozen
    return ctx


async def test_cap_serializes_concurrent_mutating_runs(tmp_path):
    gate = asyncio.Event()
    runner = _GatedRunner({}, gate)
    ctx = _ctx(tmp_path, runner, sem=asyncio.Semaphore(1))

    t1 = asyncio.create_task(ctx.run_command(MUT_A))
    t2 = asyncio.create_task(ctx.run_command(MUT_B))
    await asyncio.sleep(0.05)  # let both get approved and race for the semaphore

    assert runner.active == 1, "cap=1 should allow only one heavy run at a time"
    assert runner.peak == 1

    gate.set()
    await asyncio.gather(t1, t2)
    assert runner.peak == 1  # never exceeded the cap across the whole run


async def test_higher_cap_allows_more_concurrency(tmp_path):
    gate = asyncio.Event()
    runner = _GatedRunner({}, gate)
    ctx = _ctx(tmp_path, runner, sem=asyncio.Semaphore(2))

    tasks = [asyncio.create_task(ctx.run_command(c)) for c in (MUT_A, MUT_B)]
    await asyncio.sleep(0.05)
    assert runner.active == 2, "cap=2 should allow two heavy runs at once"
    gate.set()
    await asyncio.gather(*tasks)


async def test_readonly_is_never_capped(tmp_path):
    gate = asyncio.Event()
    gate.set()  # execute returns immediately
    runner = _GatedRunner({}, gate)
    # Exhausted cap: a mutating run could never acquire it, but read-only must bypass entirely.
    ctx = _ctx(tmp_path, runner, sem=asyncio.Semaphore(0))

    await asyncio.wait_for(ctx.run_command(RO), timeout=1.0)      # read-only via run_command
    await asyncio.wait_for(ctx.run_readonly(RO), timeout=1.0)     # and via run_readonly
    assert runner.started == 2  # both read-only commands ran despite the exhausted cap
    # A mutating run, by contrast, blocks on the exhausted cap (proves the gate is real)...
    blocked = asyncio.create_task(ctx.run_command(MUT_A))
    await asyncio.sleep(0.05)
    assert not blocked.done(), "mutating run should be blocked by the exhausted cap"
    # ...and crucially it is blocked at the semaphore acquire, BEFORE entering execute.
    assert runner.started == 2, "mutating run must not have entered execute while gated"
    blocked.cancel()
    with pytest.raises(asyncio.CancelledError):
        await blocked


def test_session_manager_wires_shared_cap_and_isolated_workspaces(tmp_path):
    settings = Settings(_env_file=None, repos_dir=tmp_path / "repos", workspace_dir=tmp_path / "ws")
    sem = asyncio.Semaphore(2)
    mgr = SessionManager(
        settings, CommandPolicy.from_file(settings.command_policy_path),
        CommandRunner(settings.repo_paths), run_semaphore=sem,
    )
    s1, s2 = mgr.create(), mgr.create()
    assert s1.id != s2.id
    assert s1.ctx.workspace != s2.ctx.workspace          # isolated per-session workspace
    assert s1.ctx.run_semaphore is sem is s2.ctx.run_semaphore  # one shared cap across sessions


class _ThreadGatedRunner(CommandRunner):
    """Like _GatedRunner but blocks on a threading.Event so the (sync) TestClient thread can
    release it across the portal boundary."""

    def __init__(self, repo_paths, started: threading.Event, gate: threading.Event):
        super().__init__(repo_paths)
        self._started = started
        self._gate = gate

    async def execute(self, logical_argv, entry, *, on_line=None, timeout=None, cwd=None, extra_env=None):
        self._started.set()
        while not self._gate.is_set():
            await asyncio.sleep(0.01)
        return RunResult(exit_code=0, duration_s=0.0, real_argv=list(logical_argv),
                         cwd=None, output="standup ok")


@pytest.mark.skipif(not get_settings().bench_repo.is_dir(), reason="repo not present")
def test_inflight_run_survives_disconnect(tmp_path):
    """An approved standup is mid-execution when the socket drops; the turn must keep running
    in the background and persist its result rather than being cancelled."""
    from app.main import app

    started, gate = threading.Event(), threading.Event()

    with TestClient(app) as client:
        st = ScriptedTransports().install(app)
        st.add_turns(
            AssistantTurn(text="Standing up.", tool_calls=[ToolCall("c1", "execute_llmdbenchmark", {
                "subcommand": "standup", "spec": "cicd/kind", "namespace": "llmd-quickstart",
                "flags": {"skip_smoketest": True},
            })]),
            AssistantTurn(text="Done."),
        )
        app.state.sessions._runner = _ThreadGatedRunner(get_settings().repo_paths, started, gate)

        with client.websocket_connect("/ws") as ws:
            ready = ws.receive_json()
            assert ready["type"] == "ready"
            sid = ready["data"]["session_id"]
            ws.send_json({"type": "user_message", "text": "stand up a tiny stack"})
            # Approve the standup, then wait until it is actually executing.
            for _ in range(80):
                ev = ws.receive_json()
                if ev["type"] == "approval_request":
                    ws.send_json({"type": "approval", "request_id": ev["data"]["request_id"], "approved": True})
                    break
            assert started.wait(timeout=5), "runner never started executing the approved command"
        # Socket is now closed (disconnect). BEFORE releasing the run, confirm the server
        # processed the disconnect and DETACHED the still-in-flight turn (rather than
        # cancelling it) — this is what makes the test prove background survival, not mere
        # eventual completion.
        detached = None
        for _ in range(250):
            detached = next((t for t in app.state.background_tasks if not t.done()), None)
            if detached is not None:
                break
            time.sleep(0.02)
        assert detached is not None, "in-flight turn was not detached as a background task on disconnect"

        gate.set()  # let the blocked execute finish
        for _ in range(250):
            if detached.done():
                break
            time.sleep(0.02)
        assert detached.done() and not detached.cancelled(), "background turn was cancelled, not run to completion"
        s = app.state.sessions.get(sid)
        assert s and any(m.get("role") == "tool_results" for m in s.messages), \
            "background turn finished but did not persist the standup result"


def _tool_results_rejected(session) -> bool:
    for m in session.messages:
        if m.get("role") == "tool_results":
            for r in m.get("results", []):
                if '"rejected": true' in r.get("content", ""):
                    return True
    return False


@pytest.mark.skipif(not get_settings().bench_repo.is_dir(), reason="repo not present")
def test_post_disconnect_approval_stays_pending_and_reemits_on_reconnect(tmp_path):
    """Switching chats must NOT reject a pending approval. A turn parked at a SECOND approval
    when the socket drops stays parked (it holds no concurrency slot) rather than auto-
    rejecting; reopening the chat re-surfaces the same approval so the user can decide and the
    turn resumes — and nothing is ever recorded as rejected."""
    from app.main import app

    started, gate = threading.Event(), threading.Event()
    with TestClient(app) as client:
        st = ScriptedTransports().install(app)
        st.add_turns(
            AssistantTurn(text="standup", tool_calls=[ToolCall("c1", "execute_llmdbenchmark", {
                "subcommand": "standup", "spec": "cicd/kind", "namespace": "llmd-quickstart",
                "flags": {"skip_smoketest": True}})]),
            AssistantTurn(text="teardown", tool_calls=[ToolCall("c2", "execute_llmdbenchmark", {
                "subcommand": "teardown", "spec": "cicd/kind", "namespace": "llmd-quickstart"})]),
            AssistantTurn(text="done"),
        )
        app.state.sessions._runner = _ThreadGatedRunner(get_settings().repo_paths, started, gate)
        with client.websocket_connect("/ws") as ws:
            ready = ws.receive_json()
            sid = ready["data"]["session_id"]
            ws.send_json({"type": "user_message", "text": "stand up then tear down"})
            for _ in range(80):
                ev = ws.receive_json()
                if ev["type"] == "approval_request":
                    ws.send_json({"type": "approval", "request_id": ev["data"]["request_id"], "approved": True})
                    break
            assert started.wait(timeout=5)
            gate.set()  # let the standup execute finish; the turn advances to the teardown gate

            # The teardown approval must now be PARKED (pending), not auto-rejected. Wait for the
            # TEARDOWN gate specifically (tool_call_id "c2"): there is a brief window where the
            # just-approved standup gate ("c1") is still being popped from `pending`, so grabbing
            # an arbitrary first key could capture the stale standup rid instead.
            pending_rid = None
            for _ in range(250):
                ch = app.state.channels.get(sid)
                if ch:
                    pending_rid = next(
                        (rid for rid, p in ch.pending.items() if p.get("tool_call_id") == "c2"),
                        None,
                    )
                    if pending_rid:
                        break
                time.sleep(0.02)
            assert pending_rid, "teardown approval never parked as pending"
            assert not _tool_results_rejected(app.state.sessions.get(sid)), \
                "teardown was rejected while still parked"
            running = app.state.running.get(sid)
            assert running is not None and not running.done(), "turn should be parked, not finished"

        # ws is now closed (chat switch). The parked approval must SURVIVE: the turn is detached
        # to the background, the pending approval is intact, and nothing is recorded as rejected.
        detached = None
        for _ in range(250):
            detached = next((t for t in app.state.background_tasks if not t.done()), None)
            if detached is not None:
                break
            time.sleep(0.02)
        assert detached is not None, "parked turn was not detached as a background task"
        ch = app.state.channels.get(sid)
        assert ch is not None and pending_rid in ch.pending, "pending approval did not survive disconnect"
        assert not _tool_results_rejected(app.state.sessions.get(sid)), "disconnect rejected the parked approval"

        # Reopen the chat: the same approval is re-emitted so the user can finally decide.
        with client.websocket_connect(f"/ws?session={sid}") as ws2:
            r2 = ws2.receive_json()
            assert r2["type"] == "ready" and r2["data"]["running"] is True
            reemitted = None
            for _ in range(80):
                ev = ws2.receive_json()
                if ev["type"] == "approval_request":
                    reemitted = ev["data"]["request_id"]
                    break
            assert reemitted == pending_rid, "the parked approval was not re-emitted on reconnect"
            ws2.send_json({"type": "approval", "request_id": reemitted, "approved": True})
            # gate is already set, so the teardown execute completes and the turn finishes.
            for _ in range(250):
                if detached.done():
                    break
                time.sleep(0.02)
            assert detached.done() and not detached.cancelled(), "resumed turn did not run to completion"

        s = app.state.sessions.get(sid)
        assert not _tool_results_rejected(s), "the resumed approval was recorded as rejected"
        # Both the standup (c1) and the teardown (c2) produced real (non-rejected) tool_results.
        assert sum(1 for m in s.messages if m.get("role") == "tool_results") >= 2


@pytest.mark.skipif(not get_settings().bench_repo.is_dir(), reason="repo not present")
def test_second_message_to_running_session_is_queued_as_steer(tmp_path):
    """A message sent to a session whose turn is still running must NOT start a concurrent turn
    (two turns mutating one chat). Instead of the old "still working" rejection it is QUEUED as a
    steer (Claude-Code style): the running loop drains it at its next step. Here a 2nd connection
    sends mid-standup; we assert no concurrent turn starts, the message is queued (not rejected),
    and once the standup completes it's threaded into the SAME turn as a user message."""
    from app.main import app

    started, gate = threading.Event(), threading.Event()
    with TestClient(app) as client:
        st = ScriptedTransports().install(app)
        # One transport script: the standup turn, then the wire turn the queued steer's
        # follow-up query consumes (the engine keeps the SAME app-level turn alive).
        st.add_script(sdk_script([
            AssistantTurn(text="standup", tool_calls=[ToolCall("c1", "execute_llmdbenchmark", {
                "subcommand": "standup", "spec": "cicd/kind", "namespace": "llmd-quickstart",
                "flags": {"skip_smoketest": True}})]),
            AssistantTurn(text="done"),
        ]) + sdk_script([AssistantTurn(text="switching to the other thing")]))
        # Count engine connects: a second concurrent turn on the session would pop a 2nd one.
        factory_calls = []
        inner = app.state.sdk_transport_factory
        app.state.sdk_transport_factory = lambda: (factory_calls.append(1) or inner)()
        app.state.sessions._runner = _ThreadGatedRunner(get_settings().repo_paths, started, gate)
        with client.websocket_connect("/ws") as ws1:
            ready = ws1.receive_json()
            sid = ready["data"]["session_id"]
            ws1.send_json({"type": "user_message", "text": "stand up"})
            for _ in range(80):
                ev = ws1.receive_json()
                if ev["type"] == "approval_request":
                    ws1.send_json({"type": "approval", "request_id": ev["data"]["request_id"], "approved": True})
                    break
            assert started.wait(timeout=5)  # ws1's turn is now blocked mid-standup

            with client.websocket_connect(f"/ws?session={sid}") as ws2:
                r2 = ws2.receive_json()
                assert r2["type"] == "ready" and r2["data"]["running"] is True
                ws2.send_json({"type": "user_message", "text": "do something else"})
                # The send is QUEUED as a steer, not rejected and not a new turn. The turn is parked
                # in the gated runner so we can't block-read a reply on ws2 — check server state.
                for _ in range(100):
                    live = LIVE_TURNS.get(sid)
                    if live is not None and "do something else" in live._steers:
                        break
                    time.sleep(0.02)
                assert "do something else" in LIVE_TURNS[sid]._steers, "the message was not queued as a steer"
                assert len(factory_calls) == 1, "a second concurrent turn was started on one session"

                gate.set()  # release the standup so the SAME turn drains the steer + finishes
                seen = []
                for _ in range(120):
                    ev = ws2.receive_json()
                    seen.append(ev["type"])
                    if ev["type"] == "done":
                        break
                assert "error" not in seen, f"the steer produced an error frame: {seen}"
                assert seen[-1] == "done"

        # The steer was threaded into the running turn as a real user message (not dropped).
        s = app.state.sessions.get(sid)
        assert any(m.get("role") == "user" and m.get("content") == "do something else"
                   for m in s.messages), "the steer was not threaded into the running turn"


async def test_runner_timeout_bounds_whole_lifecycle(tmp_path):
    """Directly exercises the runner fix: a child that closes stdout but keeps running is
    still killed at the deadline (the whole lifecycle is bounded, not just the stdout pump),
    so a heavy run cannot pin a concurrency slot forever."""
    runner = CommandRunner({})
    script = "import os, time; os.close(1); time.sleep(30)"
    start = time.monotonic()
    res = await runner.execute([sys.executable, "-c", script], None, timeout=0.5)
    elapsed = time.monotonic() - start
    assert res.timed_out is True
    assert elapsed < 5.0, f"runner did not bound the process lifecycle (took {elapsed:.1f}s)"
