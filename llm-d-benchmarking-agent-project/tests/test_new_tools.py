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


# ---- read_knowledge (hybrid: core inline + rest on-demand) ----------------

def test_read_knowledge_returns_content_for_valid_topic(tool_ctx):
    out = probe.read_knowledge(tool_ctx, name="capacity")
    assert out["name"] == "capacity.md"
    assert out["topic"] == "capacity"
    # The on-demand guide must come back with its real, full content.
    expected = (tool_ctx.settings.knowledge_dir / "capacity.md").read_text()
    assert out["content"] == expected
    assert "capacity" in out["content"].lower()


def test_read_knowledge_accepts_full_basename(tool_ctx):
    out = probe.read_knowledge(tool_ctx, name="analysis.md")
    assert out["name"] == "analysis.md"
    assert out["content"]


def test_read_knowledge_rejects_unknown_name(tool_ctx):
    out = probe.read_knowledge(tool_ctx, name="does_not_exist")
    assert "error" in out
    # The error must list valid topics so the model can self-correct.
    assert "capacity.md" in out["valid_topics"]


@pytest.mark.parametrize("evil", [
    "../config.py",
    "../../etc/passwd",
    "/etc/passwd",
    "knowledge/capacity.md",
    "..",
])
def test_read_knowledge_rejects_path_traversal(tool_ctx, evil):
    out = probe.read_knowledge(tool_ctx, name=evil)
    assert "error" in out and "content" not in out
    assert "valid_topics" in out


async def test_read_knowledge_in_tool_definitions_and_dispatch(tool_ctx):
    from app.tools.registry import tool_definitions

    names = {d["name"] for d in tool_definitions()}
    assert "read_knowledge" in names
    # End-to-end via dispatch: valid topic returns content.
    result = await dispatch(tool_ctx, "read_knowledge", {"name": "history"})
    assert result["name"] == "history.md" and result["content"]


# ---- build_system_prompt: core inline + on-demand index ------------------

def test_system_prompt_inlines_core_and_indexes_on_demand(tool_ctx):
    from app.agent.prompt import CORE_KNOWLEDGE, build_system_prompt

    prompt = build_system_prompt(tool_ctx)
    kdir = tool_ctx.settings.knowledge_dir

    # (a) Each CORE file's actual body must be inlined verbatim.
    for name in CORE_KNOWLEDGE:
        body = (kdir / name).read_text()
        # A distinctive mid-file slice (skips the heading shared with the index line).
        chunk = body[120:300]
        assert chunk and chunk in prompt, f"core file {name} not inlined"

    # (b) On-demand files: their FULL body is NOT inlined, but their name IS in the index,
    # and the index tells the model to call read_knowledge.
    on_demand = ["multi_harness.md", "capacity.md", "analysis.md", "sweep_playbook.md",
                 "history.md", "observability.md", "packaging.md", "orchestrator.md"]
    assert 'read_knowledge("<topic>")' in prompt
    for name in on_demand:
        body = (kdir / name).read_text()
        chunk = body[300:480]
        assert chunk and chunk not in prompt, f"on-demand file {name} should NOT be inlined"
        assert name in prompt, f"on-demand file {name} missing from the index"


def test_multi_harness_full_body_absent_but_indexed(tool_ctx):
    # Explicit single-file assertion required by the task: the multi_harness body is gone
    # from the prompt, yet its name still appears (so the model can load it on demand).
    from app.agent.prompt import build_system_prompt

    prompt = build_system_prompt(tool_ctx)
    body = (tool_ctx.settings.knowledge_dir / "multi_harness.md").read_text()
    # The bulk of the file (everything past the first heading) is not present.
    assert body[200:] not in prompt
    assert "multi_harness" in prompt
