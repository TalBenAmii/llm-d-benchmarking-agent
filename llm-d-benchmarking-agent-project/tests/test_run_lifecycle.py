"""Phase 16 — Run lifecycle & readiness.

Hermetic coverage of the four acceptance criteria, all with fakes (no live cluster, no GPU,
no network, no real OS signals):

  (a) cancelling a run RELEASES its concurrency-cap semaphore slot — a subsequent run can start;
  (b) the graceful-shutdown handler, CALLED DIRECTLY, cancels in-flight tasks without orphaning;
  (c) /readyz reports per-component readiness (provider / repos / runner / workspace), via the
      real FastAPI wiring (TestClient);
  (d) reattach replays the buffered live events of a still-running run (Phase 15 buffer + the
      cancel control message that stops it cleanly).

The cancel tool + the cancel control message + the SIGTERM handler are mechanism; the judgment
about WHEN to cancel lives in knowledge/run_lifecycle.md (asserted present + prompt-loaded by
test_cancel_judgment_lives_in_knowledge).
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.agent.lifecycle import RunRegistry
from app.config import Settings, get_settings
from app.llm.provider import AssistantTurn, ToolCall
from app.security.allowlist import Allowlist
from app.security.runner import CommandRunner, RunResult
from app.tools.context import ToolContext, ToolError
from app.tools.manage_runs import cancel_run
from tests.flows.catalog_snapshot import frozen_catalog

MUT = ["kind", "create", "cluster", "--name", "lc-a"]
MUT2 = ["kind", "create", "cluster", "--name", "lc-b"]


class _BlockingRunner(CommandRunner):
    """execute() blocks on an asyncio.Event so a 'heavy' run can be held mid-flight while we
    cancel it. It does NOT swallow CancelledError, so cancelling the awaiting task unwinds the
    caller's `async with run_semaphore` (the slot-release the test asserts)."""

    def __init__(self, repo_paths, gate: asyncio.Event):
        super().__init__(repo_paths)
        self._gate = gate
        self.entered = 0

    async def execute(self, logical_argv, entry, *, on_line=None, timeout=None, cwd=None, extra_env=None):
        self.entered += 1
        await self._gate.wait()  # block until released; cancellation here unwinds the semaphore
        return RunResult(exit_code=0, duration_s=0.0, real_argv=list(logical_argv), cwd=None)


def _ctx(tmp_path, runner, *, sem, session_id="s", runs=None):
    settings = Settings(_env_file=None, repos_dir=tmp_path / "repos", workspace_dir=tmp_path / "ws")

    async def approve(kind, payload):
        return True

    ctx = ToolContext(
        settings=settings,
        allowlist=Allowlist.from_file(settings.allowlist_path),
        runner=runner,
        workspace=tmp_path / "ws",
        request_approval=approve,
        run_semaphore=sem,
        runs=runs,
        session_id=session_id,
    )
    frozen = frozen_catalog()
    ctx._catalog = frozen
    ctx.catalog = lambda *, refresh=False: frozen
    return ctx


# --- (a) cancel releases the concurrency semaphore --------------------------------------


async def test_cancel_releases_semaphore_slot(tmp_path):
    """A heavy run holds the only semaphore slot (cap=1); a second run cannot start. Cancelling
    the first run via the registry releases the slot so the second run THEN starts — the precise
    Phase-2 deferral this phase closes (an abandoned run no longer pins a slot until timeout)."""
    gate = asyncio.Event()
    runner = _BlockingRunner({}, gate)
    sem = asyncio.Semaphore(1)
    runs = RunRegistry()

    # Run A acquires the slot and blocks inside execute().
    ctx_a = _ctx(tmp_path, runner, sem=sem, session_id="A")
    task_a = asyncio.create_task(ctx_a.run_command(MUT))
    runs.register("A", task_a)
    await asyncio.sleep(0.05)
    assert runner.entered == 1, "run A should have entered execute (holding the slot)"
    assert sem.locked(), "the single slot is held by run A"

    # Run B wants the slot but is blocked at the semaphore acquire (never enters execute).
    ctx_b = _ctx(tmp_path, runner, sem=sem, session_id="B")
    task_b = asyncio.create_task(ctx_b.run_command(MUT2))
    await asyncio.sleep(0.05)
    assert not task_b.done() and runner.entered == 1, "run B must be blocked on the exhausted cap"

    # Cancel run A through the registry: this is what FREES the slot.
    cancelled = await runs.cancel("A")
    assert cancelled is True
    assert task_a.cancelled(), "run A's task should be cancelled"

    # Now run B can acquire the freed slot and start executing.
    await asyncio.sleep(0.05)
    assert runner.entered == 2, "run B should start once the cancelled run released its slot"

    gate.set()
    await task_b
    assert task_b.done() and not task_b.cancelled()


async def test_cancel_no_active_run_is_idempotent(tmp_path):
    """Cancelling a session with no in-flight run is a no-op that returns False (idempotent)."""
    runs = RunRegistry()
    assert await runs.cancel("ghost") is False
    assert runs.is_running("ghost") is False


async def test_shutdown_cancels_run_registered_during_the_sweep(tmp_path):
    """A turn registered DURING shutdown's awaited cancellation sweep must still be cancelled —
    otherwise it survives a SIGTERM and orphans its K8s Job / subprocess (the exact failure
    ``shutdown`` exists to prevent).

    Concrete interleaving (the steer backstop firing mid-shutdown): two turns A and B are both
    in-flight when SIGTERM lands. ``shutdown`` snapshots the live handles once ([A, B]) and starts
    cancelling A, *awaiting* its unwind. While that await yields the loop, B finishes its last step
    NORMALLY and — in ``run_turn``'s ``finally`` — the steer backstop spawns a follow-up turn C and
    ``runs.register(B, C)``. C is registered AFTER the snapshot, so the single-pass sweep never sees
    it; C keeps running past shutdown. We model this by having A's cancellation handler register C
    (deterministic, no sleeps): the new handle appears strictly during the sweep's await.
    """
    runs = RunRegistry()

    # C: the follow-up turn the backstop spawns mid-shutdown. It parks forever unless cancelled —
    # standing in for an approved benchmark run that must not survive SIGTERM.
    async def _followup() -> None:
        await asyncio.Event().wait()

    c_task: asyncio.Task | None = None

    # A: the first in-flight turn. Its unwind (driven BY shutdown's await) registers C on the same
    # session — exactly what the steer backstop does from run_turn's finally while a sibling turn is
    # being cancelled. Registering during the cancel means C lands AFTER shutdown snapshotted [A, B].
    async def _turn_a() -> None:
        nonlocal c_task
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            c_task = asyncio.create_task(_followup())
            runs.register("S", c_task)  # backstop replaces this session's handle mid-sweep
            raise

    # B: a second, independent in-flight turn so the sweep's snapshot has >1 entry and the await
    # that cancels A (the first) is where C sneaks in before the sweep reaches the rest.
    async def _turn_b() -> None:
        await asyncio.Event().wait()

    a_task = asyncio.create_task(_turn_a())
    b_task = asyncio.create_task(_turn_b())
    runs.register("S", a_task)
    runs.register("B", b_task)
    await asyncio.sleep(0)  # let both tasks park on their events

    summary = await runs.shutdown()

    # The follow-up turn C was registered during the sweep; shutdown must have cancelled it too.
    assert c_task is not None, "the mid-sweep backstop turn was never spawned"
    assert c_task.cancelled(), (
        "a turn registered during shutdown's awaited sweep survived SIGTERM (orphaned run)"
    )
    # And the registry is empty afterwards — no live handle leaks past shutdown.
    assert runs.active_handles() == [], "an in-flight run leaked past shutdown"
    assert "S" in summary["cancelled"] and "B" in summary["cancelled"]


# --- cancel TOOL semantics --------------------------------------------------------------


async def test_cancel_tool_cancels_other_session_and_refuses_self(tmp_path):
    """The cancel_run tool cancels a DIFFERENT session's in-flight run (freeing its slot) and
    REFUSES to cancel the very run it is invoked from (that would deadlock)."""
    gate = asyncio.Event()
    runner = _BlockingRunner({}, gate)
    sem = asyncio.Semaphore(1)
    runs = RunRegistry()

    # A background run on session "victim" holds the slot.
    ctx_victim = _ctx(tmp_path, runner, sem=sem, session_id="victim")
    victim_task = asyncio.create_task(ctx_victim.run_command(MUT))
    runs.register("victim", victim_task)
    await asyncio.sleep(0.05)
    assert runs.is_running("victim")

    # The tool is called from a DIFFERENT session ("caller"); it cancels "victim".
    ctx_caller = _ctx(tmp_path, runner, sem=sem, session_id="caller", runs=runs)
    out = await cancel_run(ctx_caller, session_id="victim")
    assert out["cancelled"] is True and out["slot_released"] is True
    assert victim_task.cancelled()

    # Cancelling itself is refused (anti-deadlock guard).
    with pytest.raises(ToolError):
        await cancel_run(ctx_caller, session_id="caller")

    # Cancelling a now-finished/absent session reports cancelled=False, not an error.
    gone = await cancel_run(ctx_caller, session_id="victim")
    assert gone["cancelled"] is False

    gate.set()


async def test_cancel_tool_requires_registry(tmp_path):
    """Without a wired registry the tool refuses loudly rather than silently no-op'ing."""
    runner = _BlockingRunner({}, asyncio.Event())
    ctx = _ctx(tmp_path, runner, sem=asyncio.Semaphore(1), session_id="x", runs=None)
    with pytest.raises(ToolError):
        await cancel_run(ctx, session_id="y")


# --- (b) graceful shutdown handler cancels in-flight tasks ------------------------------


async def test_shutdown_handler_cancels_all_inflight(tmp_path):
    """The shutdown handler (a plain coroutine — no OS signal) cancels EVERY in-flight run and
    frees their slots, so a SIGTERM stops the server without orphaning. Called DIRECTLY here."""
    gate = asyncio.Event()
    runner = _BlockingRunner({}, gate)
    sem = asyncio.Semaphore(2)
    runs = RunRegistry()

    tasks = {}
    for sid, argv in (("A", MUT), ("B", MUT2)):
        ctx = _ctx(tmp_path, runner, sem=sem, session_id=sid)
        t = asyncio.create_task(ctx.run_command(argv))
        runs.register(sid, t)
        tasks[sid] = t
    await asyncio.sleep(0.05)
    assert runner.entered == 2 and sem.locked(), "both heavy runs hold both slots"

    summary = await runs.shutdown()
    assert set(summary["cancelled"]) == {"A", "B"} and summary["count"] == 2
    assert all(t.cancelled() for t in tasks.values()), "all in-flight runs cancelled"
    # Slots are released: a fresh run can acquire one immediately (no orphaned holders).
    assert not sem.locked(), "shutdown must release every held slot"
    assert runs.active_handles() == [], "no in-flight runs remain after shutdown"

    # Idempotent: a second shutdown finds nothing.
    again = await runs.shutdown()
    assert again["count"] == 0


# --- (b') the app's graceful_shutdown wired on app.state via the real lifespan ----------


@pytest.mark.skipif(not get_settings().bench_repo.is_dir(), reason="repo not present")
def test_app_graceful_shutdown_callable_and_cancels(tmp_path):
    """app.main.graceful_shutdown is a plain coroutine a test invokes DIRECTLY (no signal). With
    a fake in-flight run registered on app.state.runs it cancels it and reports the summary."""
    from app.main import app, graceful_shutdown

    with TestClient(app):
        runs: RunRegistry = app.state.runs
        assert isinstance(runs, RunRegistry)

        async def _never():
            await asyncio.Event().wait()  # parks forever unless cancelled

        async def _drive():
            t = asyncio.create_task(_never())
            runs.register("sid-1", t)
            await asyncio.sleep(0)
            assert runs.is_running("sid-1")
            summary = await graceful_shutdown(app)
            assert summary["count"] == 1 and "sid-1" in summary["cancelled"]
            assert t.cancelled()
            # A second call is a clean no-op.
            assert (await graceful_shutdown(app))["count"] == 0

        asyncio.run(_drive())


@pytest.mark.skipif(not get_settings().bench_repo.is_dir(), reason="repo not present")
def test_graceful_shutdown_cancels_background_pre_probe(tmp_path):
    """``graceful_shutdown`` must cancel the read-only environment pre-probe (``_prewarm_env``,
    BUG-033) that is tracked ONLY in ``app.state.background_tasks`` — never ``runs.register``.

    The pre-probe runs real subprocesses (``kubectl get nodes``, ``kind get clusters``, …) via the
    runner the instant a brand-new chat connects. ``runs.shutdown()`` only sweeps the run REGISTRY,
    so it never reaches this task. If shutdown doesn't cancel ``background_tasks`` too, a SIGTERM
    that lands mid-probe leaves the task running and ORPHANS its child process group — the exact
    leak ``graceful_shutdown`` exists to prevent. We model the probe with a task that, like the
    real runner's ``CancelledError`` path (``app/security/runner.py``), reaps its 'subprocess' only
    when cancelled; the test asserts both the cancel AND the reap, and that the provider close still
    runs (a leftover probe must not abort the rest of teardown).
    """
    from app.main import app, graceful_shutdown

    with TestClient(app):
        reaped = {"value": False}

        async def _fake_pre_probe() -> None:
            # Stands in for `_prewarm_env`: parks while its subprocess runs, and reaps the child's
            # process group on cancellation exactly as the real runner does on CancelledError.
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                reaped["value"] = True  # the runner's killpg(...) on shutdown-cancel
                raise

        provider_closed = {"value": False}

        class _ClosableProvider:
            async def aclose(self) -> None:
                provider_closed["value"] = True

        async def _drive():
            app.state.provider = _ClosableProvider()
            probe = asyncio.create_task(_fake_pre_probe())
            app.state.background_tasks.add(probe)
            probe.add_done_callback(app.state.background_tasks.discard)
            await asyncio.sleep(0)  # let the probe park on its event
            assert not probe.done(), "pre-probe should still be in-flight when SIGTERM lands"

            # No registered runs — only the background pre-probe is live. graceful_shutdown must
            # still cancel it (and reap its subprocess) rather than orphaning it on SIGTERM.
            await graceful_shutdown(app)

            assert probe.cancelled(), (
                "the background environment pre-probe survived shutdown (orphaned subprocess)"
            )
            assert reaped["value"], "the pre-probe's child process group was never reaped on cancel"
            # The provider close must still have run — a leftover probe can't abort later teardown.
            assert provider_closed["value"], "provider.aclose() was skipped after the probe cancel"

        asyncio.run(_drive())


# --- (c) /readyz reports per-component readiness ----------------------------------------


@pytest.mark.skipif(not get_settings().bench_repo.is_dir(), reason="repo not present")
def test_readyz_reports_components_including_runner():
    """/readyz returns STRUCTURED per-component readiness — provider, repos, runner, workspace —
    via the real FastAPI wiring. Phase 16 adds the runner_ok component (the allowlist policy
    loads). Liveness stays minimal on /healthz."""
    from app.main import app

    with TestClient(app) as client:
        resp = client.get("/readyz")
        body = resp.json()
        assert "ready" in body and "self_check" in body
        assert resp.status_code == (200 if body["ready"] else 503)
        names = {c["name"] for c in body["self_check"]["checks"]}
        # The four components the spec names, each surfaced individually.
        assert {"workspace_writable", "provider_coherent", "repos_resolvable", "runner_ok"} <= names
        runner = next(c for c in body["self_check"]["checks"] if c["name"] == "runner_ok")
        assert runner["ok"] is True  # the shipped allowlist policy loads in CI
        assert runner["data"]["executables"] >= 1
        # Liveness is the minimal, dependency-free probe.
        live = client.get("/healthz").json()
        assert live == {"ok": True}


def test_runner_ok_component_detects_a_broken_policy(tmp_path):
    """The runner_ok readiness component FAILS (not crashes) when the allowlist policy is
    malformed — proving it's a real signal, not a constant True."""
    from app.storage.retention import _check_runner_ok

    bad = tmp_path / "allowlist.yaml"
    bad.write_text("this: is not: valid: allowlist\n  - nonsense\n")

    class _S:
        allowlist_path = bad

    out = _check_runner_ok(_S())
    assert out.ok is False and "allowlist" in out.detail.lower()


# --- (d) reattach replays buffered live events; cancel control message stops a run ------


@pytest.mark.skipif(not get_settings().bench_repo.is_dir(), reason="repo not present")
def test_reattach_replays_buffer_then_cancel_message_stops_run():
    """A client disconnects mid-turn (parked at the plan-approval gate). On reattach the missed
    LIVE events replay from the Phase-15 buffer (reattach). Then the `cancel` control message
    stops the still-running turn — the client sees `cancelled` + `done`, and the run leaves the
    registry (its slot, had it held one, would be freed)."""
    from app.main import app

    turns = [
        AssistantTurn(text="Here is the plan.", tool_calls=[ToolCall("c1", "propose_session_plan", {
            "use_case_summary": "tiny chat", "spec": "cicd/kind", "namespace": "llmd-quickstart",
            "harness": "inference-perf", "workload": "sanity_random.yaml", "expected_steps": ["standup"],
        })]),
        AssistantTurn(text="done.", tool_calls=[]),
    ]

    class _FakeProvider:
        def __init__(self, ts):
            self._t = ts
            self.i = 0

        async def chat(self, *, system, messages, tools, cache_key=None):
            t = self._t[min(self.i, len(self._t) - 1)]
            self.i += 1
            return t

    with TestClient(app) as client:
        app.state.provider = _FakeProvider(turns)

        # Connection #1: drive until parked at the approval gate, then drop without answering.
        with client.websocket_connect("/ws") as ws1:
            ready = ws1.receive_json()
            sid = ready["data"]["session_id"]
            ws1.send_json({"type": "user_message", "text": "benchmark a tiny chat model"})
            for _ in range(40):
                if ws1.receive_json()["type"] == "approval_request":
                    break

        # Still running (parked) — registered in the lifecycle registry.
        assert app.state.runs.is_running(sid), "parked turn should be a tracked in-flight run"

        # Connection #2: reattach replays the missed live events (assistant text + tool_call),
        # then re-surfaces the pending approval.
        with client.websocket_connect(f"/ws?session={sid}") as ws2:
            replayed_text = replayed_tool = re_approval = False
            for _ in range(40):
                ev = ws2.receive_json()
                if ev["type"] == "assistant_text" and ev["data"]["text"] == "Here is the plan.":
                    replayed_text = True
                if ev["type"] == "tool_call" and ev["data"]["name"] == "propose_session_plan":
                    replayed_tool = True
                if ev["type"] == "approval_request":
                    re_approval = True
                    break
            assert replayed_text and replayed_tool and re_approval, \
                "reattach must replay the buffered live events + re-surface the pending gate"

            # Now CANCEL the run via the control message instead of answering the approval.
            ws2.send_json({"type": "cancel"})
            saw_cancelled = saw_done = False
            for _ in range(40):
                ev = ws2.receive_json()
                if ev["type"] == "cancelled":
                    saw_cancelled = True
                if ev["type"] == "done":
                    saw_done = True
                    break
            assert saw_cancelled and saw_done, "cancel message must stop the run with cancelled+done"

        # The run is gone from the registry (it was cancelled, slot — if any — freed).
        for _ in range(50):
            if not app.state.runs.is_running(sid):
                break
        assert not app.state.runs.is_running(sid), "cancelled run must leave the in-flight registry"


def test_cancel_is_a_valid_inbound_frame_and_extra_forbidden():
    """The `cancel` control message validates as an inbound frame; an unknown extra field is
    rejected (extra='forbid'), keeping the WS protocol surface tight (Phase 15 hardening)."""
    from app.agent.ws_schemas import CancelIn, ValidationError, parse_inbound

    msg = parse_inbound({"type": "cancel"})
    assert isinstance(msg, CancelIn)
    with pytest.raises(ValidationError):
        parse_inbound({"type": "cancel", "session_id": "nope"})  # extra field forbidden


# --- thin-code/thick-agent: the WHEN-to-cancel judgment lives in knowledge ----------------


def test_cancel_judgment_lives_in_knowledge(tmp_path):
    """The 'when to cancel' guidance is in knowledge/run_lifecycle.md (judgment), NOT in Python
    if/elif, and the system prompt loads it so the agent reasons over it."""
    settings = get_settings()
    kfile = settings.knowledge_dir / "run/run_lifecycle.md"
    assert kfile.is_file(), "knowledge/run_lifecycle.md must exist (the cancel judgment)"
    text = kfile.read_text().lower()
    # It speaks to BOTH directions of the decision (when to and when not to cancel).
    assert "when to cancel" in text and "when not to cancel" in text

    # And the prompt assembler actually folds it into the system prompt the agent sees.
    from app.agent.prompt import build_system_prompt
    from app.security.allowlist import Allowlist
    from app.tools.context import ToolContext

    ctx = ToolContext(
        settings=settings,
        allowlist=Allowlist.from_file(settings.allowlist_path),
        runner=CommandRunner(settings.repo_paths),
        workspace=tmp_path / "ws",
    )
    prompt = build_system_prompt(ctx)
    assert "run_lifecycle.md" in prompt, "the cancel knowledge file must be loaded into the prompt"
