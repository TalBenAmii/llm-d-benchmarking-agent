"""The ToolContext approval callback for MCP.

Re-homes the web app's browser approval card onto the connecting client:

- ``kind == "command"``: the connecting client already prompted the user to allow THIS tool call
  before the handler ran, so that invocation IS the approval → return ``True``. This is the "works
  freely like a normal local agent" behaviour. It is NOT a silent auto-approve: the human checkpoint
  is the client's per-call permission prompt, and one tool call maps to one user permission.
- ``kind == "session_plan"``: ask explicitly via MCP elicitation where the client supports it; on any
  failure or an unsupporting client, fall back to a pass-through (the plan is inert — it mutates
  nothing — and every downstream MUTATING tool call is still independently client-gated).

No auto-approve of a mutation ever happens here: mutations are separate, individually client-gated
tool calls.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.tools.context import ApproveFn

if TYPE_CHECKING:
    from mcp.server.lowlevel import Server


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
