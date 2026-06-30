"""Build the single per-connection ``ToolContext`` for the MCP server.

A stdio server process serves exactly one client connection, so "one Session per connection"
collapses to one ``ToolContext`` per process, built lazily and reused. This mirrors the
shared-dependency construction in ``app/main.py`` startup (allowlist / runner / semaphore /
RunRegistry, ~``main.py:90-108``); it is built fresh here because the MCP process has no FastAPI
``app.state``. Kept deliberately parallel — if that construction changes, change it here too.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

from app.agent.lifecycle import RunRegistry
from app.config import Settings
from app.mcp.approval import make_approval_fn
from app.mcp.events import make_emit_fn
from app.security.allowlist import Allowlist
from app.security.runner import CommandRunner
from app.tools.context import ToolContext

if TYPE_CHECKING:
    from mcp.server.lowlevel import Server

log = logging.getLogger("app.mcp")


def build_connection_context(settings: Settings, *, server: Server) -> ToolContext:
    """Stand up one ToolContext for this stdio connection and wire the MCP approval/emit adapters."""
    session_id = "mcp-" + uuid.uuid4().hex[:12]
    run_semaphore = (
        asyncio.Semaphore(settings.max_concurrent_runs) if settings.max_concurrent_runs > 0 else None
    )
    ctx = ToolContext(
        settings=settings,
        allowlist=Allowlist.from_file(settings.allowlist_path),
        runner=CommandRunner(settings.repo_paths, extra_env=settings.extra_subprocess_env),
        workspace=settings.resolved_workspace_dir / "mcp" / session_id,
        run_semaphore=run_semaphore,
        runs=RunRegistry(),
        session_id=session_id,
    )
    ctx.workspace.mkdir(parents=True, exist_ok=True)
    ctx.request_approval = make_approval_fn(server)
    ctx.emit = make_emit_fn(server)
    # Pre-warm the live catalog so the first tool call is not paying for it. Best-effort: the repos
    # may be absent (e.g. a fresh checkout) — a tool that needs the catalog will surface that itself.
    try:
        ctx.catalog()
    except Exception:  # noqa: BLE001 — prewarm only; never block server startup
        log.warning("mcp.catalog_prewarm_failed", exc_info=True)
    return ctx
