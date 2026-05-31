"""Tests for the generic run_command tool, the fetch_key_docs context tool, and the
vetted install_prereqs.sh prerequisite installer (allowlist + runner wiring)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.tools import command, probe
from app.tools.context import ApprovalRejected, ToolError
from app.tools.registry import dispatch


# ---- run_command (the generic allowlisted-command tool) -------------------

async def test_run_command_denies_non_allowlisted(tool_ctx):
    with pytest.raises(ToolError):
        await command.run_command(tool_ctx, argv=["rm", "-rf", "/"])


async def test_run_command_denies_bad_cluster_name(tool_ctx):
    with pytest.raises(ToolError):
        await command.run_command(tool_ctx, argv=["kind", "create", "cluster", "--name", "Bad_Name"])


async def test_run_command_mutating_requires_approval(tool_ctx):
    # A valid mutating command must hit the approval gate; rejecting it raises (no exec).
    async def reject(kind, payload):
        assert kind == "command"
        return False

    tool_ctx.request_approval = reject
    with pytest.raises(ApprovalRejected):
        await command.run_command(tool_ctx, argv=["kind", "create", "cluster", "--name", "llmd-quickstart"])


async def test_run_command_install_prereqs_requires_approval(tool_ctx):
    # Installing prerequisites is mutating — it must route through the approval gate too.
    async def reject(kind, payload):
        return False

    tool_ctx.request_approval = reject
    with pytest.raises(ApprovalRejected):
        await command.run_command(tool_ctx, argv=["install_prereqs.sh", "--all"])


async def test_run_command_schema_requires_argv(tool_ctx):
    result = await dispatch(tool_ctx, "run_command", {})
    assert result.get("error") == "invalid arguments"


def test_install_prereqs_resolves_to_executable_project_script(tool_ctx):
    # The `project-script` runner invoke type must resolve install_prereqs.sh to the real
    # file shipped with the agent project — present and executable.
    entry = tool_ctx.allowlist.executable("install_prereqs.sh")
    real, cwd = tool_ctx.runner.resolve(["install_prereqs.sh", "--all"], entry)
    script = Path(real[0])
    assert script.name == "install_prereqs.sh"
    assert script.is_file() and os.access(script, os.X_OK)
    assert real[1:] == ["--all"]


# ---- fetch_key_docs (hard-coded pointers, live content) -------------------

def test_fetch_key_docs_lists_available_tasks(tool_ctx):
    out = probe.fetch_key_docs(tool_ctx, task="__none__")
    assert "quickstart" in out["available_tasks"]
    assert out["docs"] == []  # no doc has that task


def test_fetch_key_docs_quickstart(tool_ctx):
    if not tool_ctx.settings.bench_repo.is_dir():
        pytest.skip("bench repo not present")
    out = probe.fetch_key_docs(tool_ctx, task="quickstart")
    assert out["task"] == "quickstart"
    assert all(d["task"] == "quickstart" for d in out["docs"])
    # The quickstart doc must resolve and carry real content.
    qs = next((d for d in out["docs"] if d["path"].endswith("docs/quickstart.md")), None)
    assert qs is not None and qs["found"] and "kind" in qs["content"].lower()
