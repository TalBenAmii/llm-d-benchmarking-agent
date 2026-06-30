"""Tests for the standalone MCP server (``app/mcp``).

Hermetic: no live cluster, no LLM, no network. The tool tests reuse the ``tool_ctx`` fixture and
exercise only paths that don't need a populated catalog (so they pass with empty sibling repos in a
worktree). The approval tests drive ``make_approval_fn`` with a fake server/session.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import get_settings
from app.mcp import prompts as mcp_prompts
from app.mcp import resources as mcp_resources
from app.mcp import server as mcp_server
from app.mcp.approval import make_approval_fn
from app.mcp.instructions import INSTRUCTIONS
from app.tools.context import ApprovalRejected
from app.tools.registry import tool_definitions


def _knowledge_dir() -> Path:
    return get_settings().knowledge_dir


# --- tools -------------------------------------------------------------------------------------

def test_exposed_tools_mirror_registry():
    defs = mcp_server.exposed_definitions()
    names = {d["name"] for d in defs}
    all_names = {d["name"] for d in tool_definitions()}
    assert "load_tools" not in names
    assert names == all_names - {"load_tools"}
    # each def carries exactly the keys list_tools maps to types.Tool, schema mirrored 1:1
    by_name = {d["name"]: d for d in tool_definitions()}
    for d in defs:
        assert set(d) == {"name", "description", "input_schema"}
        assert d["input_schema"] == by_name[d["name"]]["input_schema"]


async def test_run_tool_unknown_tool(tool_ctx):
    out = await mcp_server.run_tool(tool_ctx, "no_such_tool", {})
    assert "error" in out and "unknown tool" in out["error"]


async def test_run_tool_invalid_args(tool_ctx):
    # read_knowledge requires 'name'; empty args -> dispatch returns an 'invalid arguments' error dict
    out = await mcp_server.run_tool(tool_ctx, "read_knowledge", {})
    assert out.get("error") == "invalid arguments"


async def test_run_tool_maps_approval_rejected(tool_ctx, monkeypatch):
    async def _raise(ctx, name, args):
        raise ApprovalRejected(["kubectl", "apply", "-f", "x.yaml"])

    monkeypatch.setattr(mcp_server, "dispatch", _raise)
    out = await mcp_server.run_tool(tool_ctx, "execute_llmdbenchmark", {"subcommand": "standup"})
    assert out["rejected"] is True
    assert "kubectl apply" in out["reason"]


# --- approval re-homing ------------------------------------------------------------------------

async def test_command_approval_is_client_gated():
    # The 'command' kind never touches the server: the client already prompted for the tool call.
    fn = make_approval_fn(SimpleNamespace())
    assert await fn("command", {"argv": ["kubectl", "apply"]}) is True


def _server_with_elicit(elicit_form):
    session = SimpleNamespace(elicit_form=elicit_form)
    return SimpleNamespace(request_context=SimpleNamespace(session=session))


async def test_session_plan_elicit_accept():
    async def elicit_form(message, requestedSchema):
        return SimpleNamespace(action="accept", content={"approve": True})

    fn = make_approval_fn(_server_with_elicit(elicit_form))
    assert await fn("session_plan", {"spec": "cicd/kind", "namespace": "demo"}) is True


async def test_session_plan_elicit_decline():
    async def elicit_form(message, requestedSchema):
        return SimpleNamespace(action="decline", content=None)

    fn = make_approval_fn(_server_with_elicit(elicit_form))
    assert await fn("session_plan", {"spec": "cicd/kind"}) is False


async def test_session_plan_sentinel_when_no_request_context():
    class _NoCtx:
        @property
        def request_context(self):
            raise LookupError("no active request")

    fn = make_approval_fn(_NoCtx())
    # plan is inert; downstream mutating tool calls stay client-gated -> pass-through is safe
    assert await fn("session_plan", {"spec": "cicd/kind"}) is True


async def test_session_plan_sentinel_when_elicitation_unsupported():
    async def elicit_form(message, requestedSchema):
        raise RuntimeError("client advertises no elicitation capability")

    fn = make_approval_fn(_server_with_elicit(elicit_form))
    assert await fn("session_plan", {"spec": "cicd/kind"}) is True


# --- resources ---------------------------------------------------------------------------------

def test_resources_match_knowledge_glob():
    kd = _knowledge_dir()
    res = mcp_resources.list_resource_objects(kd)
    names = {r.name for r in res}
    expected = {p.stem for p in mcp_resources._knowledge_files(kd)}
    assert names == expected
    assert all(str(r.uri).startswith("doc://knowledge/") for r in res)
    # the excluded agent-context files never leak as resources
    assert "CLAUDE" not in names and "CONTEXT" not in names


def test_read_resource_returns_contents():
    kd = _knowledge_dir()
    res = mcp_resources.list_resource_objects(kd)
    contents = mcp_resources.read_resource_contents(kd, res[0].uri)
    assert contents and len(contents[0].content) > 0


def test_read_resource_rejects_unknown_uri():
    with pytest.raises(ValueError):
        mcp_resources.read_resource_contents(_knowledge_dir(), "doc://knowledge/../../etc/passwd")


# --- prompts -----------------------------------------------------------------------------------

def test_list_prompts():
    prompts = mcp_prompts.list_prompt_objects()
    names = {p.name for p in prompts}
    assert names == {
        "benchmark_this_model", "pick_deploy_path", "interpret_this_report",
        "design_a_sweep", "autotune_to_slo",
    }
    for p in prompts:
        assert isinstance(p.arguments, list)


def test_get_prompt_embeds_playbook():
    res = mcp_prompts.build_prompt_result(_knowledge_dir(), "benchmark_this_model", {"model": "llama-3-8b"})
    text = res.messages[0].content.text
    assert "llama-3-8b" in text
    assert "## quickstart_playbook.md" in text


def test_get_prompt_unknown_raises():
    with pytest.raises(ValueError):
        mcp_prompts.build_prompt_result(_knowledge_dir(), "does_not_exist", {})


# --- instructions / wiring ---------------------------------------------------------------------

def test_instructions_free_of_web_ui_terms():
    assert INSTRUCTIONS.strip()
    low = INSTRUCTIONS.lower()
    for term in ("card", "sidebar", "websocket", "/ws"):
        assert term not in low


def test_build_server_wires_instructions():
    srv = mcp_server.build_server(get_settings())
    init = srv.create_initialization_options()
    assert init.instructions == INSTRUCTIONS
