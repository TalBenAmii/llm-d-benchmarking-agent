"""Tests for the opt-in, allowlist-BYPASSING `run_shell` tool (UNRESTRICTED_TOOLS=1).

Covers the read/write classifier (which decides auto-run vs approval), the conditional
registry exposure (the tool is invisible unless the flag is on), and the preserved
human-approval flow (mutating commands prompt; read-only commands do not).
"""
from __future__ import annotations

import pytest

from app.config import Settings
from app.security.allowlist import MUTATING, READ_ONLY, Allowlist
from app.tools.context import ApprovalRejected, ToolContext, ToolError
from app.tools.registry import build_registry, dispatch, tool_definitions
from app.tools.shell import classify_shell_command, run_shell
from tests.flows.catalog_snapshot import frozen_catalog
from tests.flows.harness import CaptureRunner

# ---- the read/write classifier -------------------------------------------

@pytest.mark.parametrize("command", [
    "ls -la",
    "kubectl get pods",
    "cat foo | grep bar",
    "git log",
    "kubectl get pods -o json | jq '.items'",
    "docker ps",
    "sed -n '1,5p' foo.txt",
])
def test_classifier_read_only(command):
    assert classify_shell_command(command) == READ_ONLY


@pytest.mark.parametrize("command", [
    "rm -rf x",
    "echo hi > f",
    "kubectl apply -f x.yaml",
    "pip install foo",
    "curl -X POST https://example.com",
    "curl -o out.bin https://example.com/file",
    "frobnicate --do-thing",                 # unknown binary → fail-safe MUTATING
    "git push origin main",
    "helm install rel chart",
    "cat foo | tee out.txt",                 # a write verb (tee) anywhere in the pipeline
    "ls && rm -rf x",                         # one mutating segment taints the whole command
    "sed -i 's/a/b/' foo.txt",               # in-place sed is NOT read-only
])
def test_classifier_mutating(command):
    assert classify_shell_command(command) == MUTATING


# ---- conditional registry exposure ---------------------------------------

def test_run_shell_not_registered_by_default():
    reg = build_registry()
    assert "run_shell" not in reg
    # ...and the default tool_definitions() export (no ctx) does not expose it either.
    assert "run_shell" not in {d["name"] for d in tool_definitions()}


def test_run_shell_registered_when_unrestricted():
    reg = build_registry(unrestricted=True)
    assert "run_shell" in reg


def test_tool_definitions_exposes_run_shell_only_with_flag(tmp_path):
    off = _ctx(tmp_path, unrestricted=False)[0]
    on = _ctx(tmp_path, unrestricted=True)[0]
    assert "run_shell" not in {d["name"] for d in tool_definitions(off)}
    assert "run_shell" in {d["name"] for d in tool_definitions(on)}


# ---- the preserved approval flow -----------------------------------------

def _ctx(tmp_path, *, unrestricted: bool, emit=None, approve=None):
    settings = Settings(
        _env_file=None, unrestricted_tools=unrestricted,
        repos_dir=tmp_path / "repos", workspace_dir=tmp_path / "ws",
    )
    runner = CaptureRunner(settings.repo_paths)
    ctx = ToolContext(
        settings=settings,
        allowlist=Allowlist.from_file(settings.allowlist_path),
        runner=runner,
        workspace=tmp_path / "ws",
        emit=emit,
        request_approval=approve,
    )
    frozen = frozen_catalog()
    ctx._catalog = frozen
    ctx.catalog = lambda *, refresh=False: frozen
    return ctx, runner


async def test_run_shell_refuses_when_flag_off(tmp_path):
    # Defense in depth: even if the handler is reached, it refuses unless the flag is on.
    ctx, _ = _ctx(tmp_path, unrestricted=False)
    with pytest.raises(ToolError):
        await run_shell(ctx, command="ls -la")


async def test_read_only_command_auto_runs_without_approval(tmp_path):
    seen: list[str] = []

    async def approve(kind, payload):
        seen.append("approval")
        return True

    ctx, runner = _ctx(tmp_path, unrestricted=True, approve=approve)
    res = await run_shell(ctx, command="ls -la")

    assert seen == []                       # read-only → no approval prompt
    assert res["mode"] == READ_ONLY and res["auto_run"] is True
    assert len(runner.calls) == 1
    assert runner.calls[0]["argv"] == ["bash", "-lc", "ls -la"]


async def test_mutating_command_requires_approval(tmp_path):
    seen: list[tuple[str, str]] = []

    async def approve(kind, payload):
        seen.append((kind, payload["command"]))
        return True

    ctx, runner = _ctx(tmp_path, unrestricted=True, approve=approve)
    res = await run_shell(ctx, command="rm -rf build")

    assert seen == [("command", "rm -rf build")]   # mutating → prompted
    assert res["mode"] == MUTATING and res["auto_run"] is False
    assert len(runner.calls) == 1


async def test_rejected_mutating_command_raises_and_does_not_run(tmp_path):
    async def reject(kind, payload):
        return False

    ctx, runner = _ctx(tmp_path, unrestricted=True, approve=reject)
    with pytest.raises(ApprovalRejected):
        await run_shell(ctx, command="kubectl apply -f x.yaml")
    assert runner.calls == []               # declined → never executed


async def test_emits_command_event_with_mode_and_auto_run(tmp_path):
    events: list[tuple[str, dict]] = []

    async def emit(t, p):
        events.append((t, p))

    ctx, _ = _ctx(tmp_path, unrestricted=True, emit=emit)
    await run_shell(ctx, command="ls -la")

    cmds = [p for (t, p) in events if t == "command"]
    assert len(cmds) == 1
    assert cmds[0]["argv"] == ["bash", "-lc", "ls -la"]
    assert cmds[0]["mode"] == READ_ONLY and cmds[0]["auto_run"] is True


async def test_dispatch_routes_to_run_shell_when_enabled(tmp_path):
    ctx, runner = _ctx(tmp_path, unrestricted=True)
    out = await dispatch(ctx, "run_shell", {"command": "echo hi"})
    # `echo` is read-only, so it auto-runs (no approver wired) and returns a result dict.
    assert out["mode"] == READ_ONLY and out["exit_code"] == 0
    assert len(runner.calls) == 1


async def test_dispatch_unknown_tool_when_flag_off(tmp_path):
    ctx, _ = _ctx(tmp_path, unrestricted=False)
    out = await dispatch(ctx, "run_shell", {"command": "echo hi"})
    assert "error" in out and "run_shell" not in out.get("valid_tools", [])
