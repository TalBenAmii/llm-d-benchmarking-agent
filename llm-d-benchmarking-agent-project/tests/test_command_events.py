"""The `command` event (Phase 1 — full command transparency + debug view).

Every command the agent actually executes is announced to the UI via a `command` event,
*including* auto-run read-only probes — not just the approval-gated mutating ones. The
event fires immediately before the process runs, and (for mutating commands) only after
approval, so it records what truly executed. Sessions persist this trail so a resumed chat
can replay the debug view.
"""
from __future__ import annotations

from app.agent.session import _COMMANDS_MAX, Session
from app.config import Settings
from app.security.allowlist import Allowlist
from app.tools.context import ApprovalRejected, ToolContext
from tests.flows.catalog_snapshot import frozen_catalog
from tests.flows.harness import CaptureRunner

RO = ["kind", "get", "clusters"]                                  # read-only, no catalog needed
MUT = ["kind", "create", "cluster", "--name", "test-cluster"]     # mutating, approval-gated


def _ctx(tmp_path, *, emit=None, approve=None):
    settings = Settings(_env_file=None, repos_dir=tmp_path / "repos", workspace_dir=tmp_path / "ws")
    runner = CaptureRunner(settings.repo_paths)
    ctx = ToolContext(
        settings=settings,
        allowlist=Allowlist.from_file(settings.allowlist_path),
        runner=runner,
        workspace=tmp_path / "ws",
        emit=emit,
        request_approval=approve,
    )
    # Pin the catalog so validate()'s ref checks don't scan the empty fake repo.
    frozen = frozen_catalog()
    ctx._catalog = frozen
    ctx.catalog = lambda *, refresh=False: frozen
    return ctx, runner


def _collector():
    events: list[tuple[str, dict]] = []

    async def emit(t, p):
        events.append((t, p))

    return events, emit


def _commands(events):
    return [p for (t, p) in events if t == "command"]


async def test_run_readonly_emits_command_auto_run(tmp_path):
    events, emit = _collector()
    ctx, runner = _ctx(tmp_path, emit=emit)

    await ctx.run_readonly(RO)

    cmds = _commands(events)
    assert len(cmds) == 1
    assert cmds[0]["argv"] == RO
    assert cmds[0]["text"] == "kind get clusters"
    assert cmds[0]["mode"] == "read_only"
    assert cmds[0]["auto_run"] is True
    assert len(runner.calls) == 1  # and it actually ran


async def test_run_command_readonly_emits_command_auto_run(tmp_path):
    events, emit = _collector()
    ctx, _ = _ctx(tmp_path, emit=emit)

    await ctx.run_command(RO)

    cmds = _commands(events)
    assert len(cmds) == 1 and cmds[0]["auto_run"] is True and cmds[0]["mode"] == "read_only"


async def test_run_command_mutating_emits_after_approval(tmp_path):
    events, emit = _collector()
    seen: list[str] = []

    async def approve(kind, payload):
        seen.append("approval")
        return True

    ctx, runner = _ctx(tmp_path, emit=emit, approve=approve)
    await ctx.run_command(MUT)

    cmds = _commands(events)
    assert len(cmds) == 1
    assert cmds[0]["argv"] == MUT
    assert cmds[0]["mode"] == "mutating"
    assert cmds[0]["auto_run"] is False
    assert seen == ["approval"]  # approval happened
    assert len(runner.calls) == 1


async def test_rejected_mutating_emits_no_command(tmp_path):
    events, emit = _collector()

    async def reject(kind, payload):
        return False

    ctx, runner = _ctx(tmp_path, emit=emit, approve=reject)
    try:
        await ctx.run_command(MUT)
        assert False, "expected ApprovalRejected"
    except ApprovalRejected:
        pass

    assert _commands(events) == []   # never announced — it never ran
    assert runner.calls == []


async def test_no_emit_wired_is_safe(tmp_path):
    # With no emit callback, execution still works (no crash).
    ctx, runner = _ctx(tmp_path, emit=None)
    await ctx.run_readonly(RO)
    assert len(runner.calls) == 1


def test_session_records_and_caps_commands():
    s = Session(id="x", ctx=None)  # ctx not needed for record_command
    for i in range(_COMMANDS_MAX + 25):
        s.record_command({"text": f"cmd {i}", "argv": ["x", str(i)], "mode": "read_only", "auto_run": True})
    assert len(s.commands) == _COMMANDS_MAX
    # Oldest dropped, newest kept.
    assert s.commands[-1]["text"] == f"cmd {_COMMANDS_MAX + 24}"


def test_session_persists_and_reloads_commands(tmp_path):
    from app.security.runner import CommandRunner
    from app.tools.context import ToolContext

    settings = Settings(_env_file=None, repos_dir=tmp_path / "repos", workspace_dir=tmp_path / "ws")
    ctx = ToolContext(
        settings=settings,
        allowlist=Allowlist.from_file(settings.allowlist_path),
        runner=CommandRunner(settings.repo_paths),
        workspace=tmp_path / "ws" / "sessions" / "s1",
    )
    s = Session(id="s1", ctx=ctx)
    s.messages.append({"role": "user", "content": "hi"})
    s.record_command({"text": "kind get clusters", "argv": RO, "mode": "read_only", "auto_run": True})
    s.persist()

    from app.agent.session import SessionManager

    mgr = SessionManager(settings, ctx.allowlist, ctx.runner)
    reloaded = mgr.load("s1")
    assert reloaded is not None
    assert reloaded.commands and reloaded.commands[0]["argv"] == RO
