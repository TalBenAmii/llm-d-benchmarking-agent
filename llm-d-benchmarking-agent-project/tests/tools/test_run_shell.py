"""Tests for the agent's always-on ad-hoc `run_shell` tool (arbitrary `bash -lc`).

Covers the read/write classifier (which decides auto-run vs approval), that the tool is
registered on the default tool surface, and the preserved human-approval flow (mutating
commands prompt; read-only commands do not). `run_shell` does NOT consult the command
policy — the classifier + approval gate are its guardrail.
"""
from __future__ import annotations

import pytest

from app.security.policy import MUTATING, READ_ONLY
from app.tools.context import ApprovalRejected
from app.tools.registry import build_registry, dispatch, tool_definitions
from app.tools.run.shell import classify_shell_command, run_shell
from tests._helpers import _capture_ctx

# ---- the read/write classifier -------------------------------------------

@pytest.mark.parametrize("command", [
    "ls -la",
    "kubectl get pods",
    "cat foo | grep bar",
    "git log",
    "kubectl get pods -o json | jq '.items'",
    "docker ps",
    "sed -n '1,5p' foo.txt",
    "find . -name '*.py'",                    # plain find SEARCHES → read-only
    "find /tmp -type f | head",
    "sort foo.txt",                            # plain sort just prints → read-only
    "sort -u foo.txt | uniq",
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
    "find . -name '*.tmp' -delete",          # find -delete WRITES → mutating
    "find . -type f -exec rm {} ;",          # find -exec runs a command → mutating
    "sort -o out.txt foo.txt",               # sort -o FILE writes → mutating
    "sort foo.txt --output=foo.txt",         # long-form output write → mutating
])
def test_classifier_mutating(command):
    assert classify_shell_command(command) == MUTATING


# ---- registered on the default tool surface ------------------------------

def test_run_shell_registered_by_default():
    reg = build_registry()
    assert "run_shell" in reg
    # ...and it is exported to the LLM in the default tool_definitions().
    assert "run_shell" in {d["name"] for d in tool_definitions()}


# ---- the preserved approval flow -----------------------------------------

async def test_read_only_command_auto_runs_without_approval(tmp_path):
    seen: list[str] = []

    async def approve(kind, payload):
        seen.append("approval")
        return True

    ctx, runner = _capture_ctx(tmp_path, approve=approve)
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

    ctx, runner = _capture_ctx(tmp_path, approve=approve)
    res = await run_shell(ctx, command="rm -rf build")

    assert seen == [("command", "rm -rf build")]   # mutating → prompted
    assert res["mode"] == MUTATING and res["auto_run"] is False
    assert len(runner.calls) == 1


async def test_rejected_mutating_command_raises_and_does_not_run(tmp_path):
    async def reject(kind, payload):
        return False

    ctx, runner = _capture_ctx(tmp_path, approve=reject)
    with pytest.raises(ApprovalRejected):
        await run_shell(ctx, command="kubectl apply -f x.yaml")
    assert runner.calls == []               # declined → never executed


async def test_emits_command_event_with_mode_and_auto_run(tmp_path):
    events: list[tuple[str, dict]] = []

    async def emit(t, p):
        events.append((t, p))

    ctx, _ = _capture_ctx(tmp_path, emit=emit)
    await run_shell(ctx, command="ls -la")

    cmds = [p for (t, p) in events if t == "command"]
    assert len(cmds) == 1
    assert cmds[0]["argv"] == ["bash", "-lc", "ls -la"]
    assert cmds[0]["mode"] == READ_ONLY and cmds[0]["auto_run"] is True


async def test_dispatch_routes_to_run_shell_by_default(tmp_path):
    ctx, runner = _capture_ctx(tmp_path)
    out = await dispatch(ctx, "run_shell", {"command": "echo hi"})
    # `echo` is read-only, so it auto-runs (no approver wired) and returns a result dict.
    assert out["mode"] == READ_ONLY and out["exit_code"] == 0
    assert len(runner.calls) == 1
