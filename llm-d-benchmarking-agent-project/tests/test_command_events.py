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
    ctx, runner = _ctx(tmp_path, emit=emit)

    await ctx.run_command(RO)

    cmds = _commands(events)
    assert len(cmds) == 1
    assert cmds[0]["argv"] == RO and cmds[0]["text"] == "kind get clusters"
    assert cmds[0]["auto_run"] is True and cmds[0]["mode"] == "read_only"
    assert len(runner.calls) == 1  # the read-only command really ran (no approval skip)


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


async def test_probe_environment_emits_command_per_probe(tmp_path):
    """The headline Phase-1 behavior: read-only probes are now visible. With all tools
    present and a namespace, probe_environment runs exactly 7 read-only commands (the 6
    original probes plus the Phase-61 `kubectl get nodes` node-capacity probe) and each is
    announced (auto_run) — proving probe-emit/exec parity."""
    from unittest.mock import patch

    from app.tools.probe import probe_environment

    events, emit = _collector()
    ctx, runner = _ctx(tmp_path, emit=emit)

    with patch("app.tools.probe.shutil.which", side_effect=lambda n, *a, **k: f"/usr/bin/{n}"):
        await probe_environment(ctx, namespace="llmd-quickstart")

    cmds = _commands(events)
    assert len(cmds) == 7, [c["text"] for c in cmds]
    assert all(c["mode"] == "read_only" and c["auto_run"] is True for c in cmds)
    assert len(runner.calls) == 7  # one announcement per real execution
    exes = {c["argv"][0] for c in cmds}
    assert exes == {"docker", "kind", "kubectl"}
    # The node-capacity probe (Phase 61) is among them.
    assert ["kubectl", "get", "nodes", "-o", "json"] in [c["argv"] for c in cmds]


async def test_deploy_flow_surfaces_every_command(tmp_path):
    """Driven through the REAL agent loop: a full quickstart deploy surfaces every executed
    command as a `command` event, including each llmdbenchmark subcommand (standup/smoketest/run)."""
    from tests.flows.flows import FLOWS_BY_NAME
    from tests.flows.harness import run_flow

    run = await run_flow(FLOWS_BY_NAME["kind-quickstart"], tmp_path=tmp_path)
    cmd_argvs = [p["argv"] for (t, p) in run.events if t == "command"]

    # Every significant command (llmdbenchmark/install.sh/git/helm) is announced.
    for c in run.significant:
        assert c.argv in cmd_argvs, f"significant command never announced: {c.argv}"

    # The three llmdbenchmark lifecycle subcommands each appear in a command event.
    llmd_subs = {
        s
        for a in cmd_argvs if a and a[0] == "llmdbenchmark"
        for s in ("standup", "smoketest", "run") if s in a
    }
    assert {"standup", "smoketest", "run"} <= llmd_subs, f"missing lifecycle subcommands: saw {llmd_subs}"


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
