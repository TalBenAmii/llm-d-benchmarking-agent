"""Per-connection MCP adapters — three formerly-separate thin modules stitched together:

- the ToolContext approval callback (re-homes the web app's browser approval card onto the client),
- the ToolContext ``emit`` callback (an ``EmitFn`` → MCP-log-notification adapter, best-effort),
- the per-connection ``ToolContext`` factory (one context per stdio connection).
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING, Any

from app.agent.lifecycle import RunRegistry
from app.config import Settings
from app.security.allowlist import Allowlist
from app.security.runner import CommandRunner
from app.tools.context import ApproveFn, EmitFn, ToolContext

if TYPE_CHECKING:
    from mcp.server.lowlevel import Server

log = logging.getLogger("app.mcp")


# --- approval — the ToolContext approval callback for MCP ---------------------------------------
# Re-homes the web app's browser approval card onto the connecting client:
#
# - ``kind == "command"``: the connecting client already prompted the user to allow THIS tool call
#   before the handler ran, so that invocation IS the approval → return ``True``. This is the "works
#   freely like a normal local agent" behaviour. It is NOT a silent auto-approve: the human checkpoint
#   is the client's per-call permission prompt, and one tool call maps to one user permission.
# - ``kind == "session_plan"``: ask explicitly via MCP elicitation where the client supports it; on any
#   failure or an unsupporting client, fall back to a pass-through (the plan is inert — it mutates
#   nothing — and every downstream MUTATING tool call is still independently client-gated).
#
# No auto-approve of a mutation ever happens here: mutations are separate, individually client-gated
# tool calls.
def make_approval_fn(server: Server) -> ApproveFn:
    async def request_approval(kind: str, payload: dict[str, Any]) -> bool:
        if kind != "session_plan":
            return True
        try:
            session = server.request_context.session
        except Exception:  # noqa: BLE001 — no active request context → cannot elicit, pass through
            return True
        try:
            result = await session.elicit_form(
                message=_render_plan(payload),
                requestedSchema={
                    "type": "object",
                    "properties": {
                        "approve": {"type": "boolean", "title": "Approve this benchmark plan?"},
                    },
                    "required": ["approve"],
                },
            )
        except Exception:  # noqa: BLE001 — client lacks elicitation or it errored → sentinel pass-through
            return True
        return result.action == "accept" and bool((result.content or {}).get("approve"))

    return request_approval


def _render_plan(payload: dict[str, Any]) -> str:
    """A short human-readable plan summary for the elicitation prompt. Tolerant of either a flat
    plan dict or a ``{"plan": {...}}`` envelope, since this is a presentation-only string."""
    plan = payload.get("plan") if isinstance(payload, dict) and isinstance(payload.get("plan"), dict) else payload
    plan = plan if isinstance(plan, dict) else {}
    head = "Approve this benchmark plan?"
    summary = plan.get("use_case_summary")
    if summary:
        head += f"\n\n{summary}"
    fields = [("spec", plan.get("spec")), ("deploy_path", plan.get("deploy_path")),
              ("namespace", plan.get("namespace")), ("harness", plan.get("harness")),
              ("workload", plan.get("workload"))]
    line = "  ".join(f"{k}={v}" for k, v in fields if v is not None)
    return head + (f"\n\n{line}" if line else "")


# --- events — the ToolContext ``emit`` callback for MCP -----------------------------------------
# The web UI consumes a rich event stream (streaming output, cards); MCP has no equivalent surface, so
# events are forwarded best-effort to the connecting client as a logging notification and always to the
# local structured logger. ``emit`` must never raise into a tool handler — a dropped progress line is
# not a tool failure.
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


# --- context — build the single per-connection ``ToolContext`` for the MCP server --------------
# A stdio server process serves exactly one client connection, so "one Session per connection"
# collapses to one ``ToolContext`` per process, built lazily and reused. This mirrors the
# shared-dependency construction in ``app/main.py`` startup (allowlist / runner / semaphore /
# RunRegistry, ~``main.py:90-108``); it is built fresh here because the MCP process has no FastAPI
# ``app.state``. Kept deliberately parallel — if that construction changes, change it here too.
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
