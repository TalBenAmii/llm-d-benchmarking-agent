"""The standalone MCP server: builds the low-level ``Server``, registers the tool / resource /
prompt handlers, and runs the stdio loop.

Tools are the existing registry: ``list_tools`` mirrors ``tool_definitions()`` and ``call_tool``
routes through ``dispatch()`` — the same validation and handlers the web app uses. The only tool
adaptation is dropping ``load_tools`` (a web-prompt-budget optimization; MCP clients page their own
tool list). Pure mechanism; the judgment ships as resources/prompts/instructions.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import anyio
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from app.config import Settings
from app.mcp.adapters import build_connection_context
from app.mcp.content import INSTRUCTIONS, register_prompts, register_resources
from app.tools.context import ApprovalRejected, ToolContext, ToolError
from app.tools.registry import dispatch, tool_definitions

_SERVER_NAME = "llm-d-bench"
# Web-loop-only meta-tool: its sole job is lazy reveal of tool groups to protect the agent's cached
# prompt prefix. An MCP client manages its own tool list, so it is not exposed here.
_HIDDEN_TOOLS = frozenset({"load_tools"})


def exposed_definitions() -> list[dict[str, Any]]:
    """The tool defs this server advertises: the full registry minus the hidden meta-tools."""
    return [d for d in tool_definitions() if d["name"] not in _HIDDEN_TOOLS]


async def run_tool(ctx: ToolContext, name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    """Dispatch one tool call, mapping the agent loop's exceptions to result dicts (mirrors
    ``app/agent/loop.py::_invoke``). A tool error must never kill the stdio loop."""
    ctx.current_tool_call_id = "mcp-" + uuid.uuid4().hex[:8]
    try:
        return await dispatch(ctx, name, arguments or {})
    except ApprovalRejected as exc:
        return {"rejected": True, "reason": str(exc)}
    except ToolError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — surface as a clean tool error, never crash the server
        return {"error": f"{type(exc).__name__}: {exc}"}


def build_server(settings: Settings | None = None) -> Server:
    settings = settings or Settings()
    server = Server(_SERVER_NAME, instructions=INSTRUCTIONS)

    # One ToolContext for this process (stdio == one client connection). Lazy so the server
    # constructs even when the cluster/repos are absent; the first tool call wires it up.
    state: dict[str, ToolContext | None] = {"ctx": None}

    def _ctx() -> ToolContext:
        ctx = state["ctx"]
        if ctx is None:
            ctx = state["ctx"] = build_connection_context(settings, server=server)
        return ctx

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(name=d["name"], description=d["description"], inputSchema=d["input_schema"])
            for d in exposed_definitions()
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any] | None = None) -> list[types.ContentBlock]:
        result = await run_tool(_ctx(), name, arguments)
        return [types.TextContent(type="text", text=json.dumps(result, default=str))]

    register_resources(server, settings.knowledge_dir)
    register_prompts(server, settings.knowledge_dir)
    return server


def main() -> None:
    server = build_server()

    async def _serve() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    anyio.run(_serve)
