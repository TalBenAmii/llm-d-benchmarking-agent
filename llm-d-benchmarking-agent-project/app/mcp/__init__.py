"""Standalone MCP server (stdio) re-exposing the agent's tools, knowledge, and workflow to
external MCP clients (Claude Desktop, Claude Code, Cursor, ...).

Pure mechanism: it reuses ``app/tools`` (registry / dispatch / ToolContext) and ships the
judgment as MCP resources + prompts + server ``instructions`` sourced from ``knowledge/`` —
never duplicated here. See ``DESIGN.md`` in this folder for the full design and
``docs/history/proposals/05-mcp-server.md`` for the locked decisions.
"""
from __future__ import annotations

from app.mcp.server import build_server, main

__all__ = ["build_server", "main"]
