"""The ToolContext ``emit`` callback for MCP.

The web UI consumes a rich event stream (streaming output, cards); MCP has no equivalent surface, so
events are forwarded best-effort to the connecting client as a logging notification and always to the
local structured logger. ``emit`` must never raise into a tool handler — a dropped progress line is
not a tool failure.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from app.tools.context import EmitFn

if TYPE_CHECKING:
    from mcp.server.lowlevel import Server

log = logging.getLogger("app.mcp")


def make_emit_fn(server: Server) -> EmitFn:
    async def emit(event_type: str, payload: dict[str, Any]) -> None:
        log.info("mcp.emit", extra={"event": event_type})
        try:
            session = server.request_context.session
            await session.send_log_message(level="info", data={"event": event_type, **_safe(payload)})
        except Exception:  # noqa: BLE001 — no request context / client ignores logs / level unset
            pass

    return emit


def _safe(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep the forwarded log small, serializable, and secret-free: scalars only, drop ``env``."""
    out: dict[str, Any] = {}
    for key, value in (payload or {}).items():
        if key == "env":
            continue
        if value is None or isinstance(value, (str, int, float, bool)):
            out[key] = value
    return out
