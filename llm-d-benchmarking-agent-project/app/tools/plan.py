"""propose_session_plan — the agent surfaces a structured SessionPlan; the user reviews
and approves it before any mutating step. Reuses the same approval channel as commands.
"""
from __future__ import annotations

from typing import Any

from app.tools.context import ToolContext, ToolError
from app.validation.session_plan import SessionPlan, validate_plan


async def propose_session_plan(ctx: ToolContext, **fields: Any) -> dict[str, Any]:
    try:
        plan = SessionPlan(**fields)
    except Exception as exc:  # pydantic validation error
        raise ToolError(f"invalid session plan: {exc}") from exc

    errors = validate_plan(plan, ctx.catalog())
    if errors:
        return {"approved": False, "valid": False, "errors": errors,
                "note": "fix these against the live catalog and propose again"}

    if ctx.request_approval is None:
        raise ToolError("approval channel not wired")

    plan_dict = plan.model_dump()
    approved = await ctx.request_approval("session_plan", plan_dict)
    if approved:
        return {"approved": True, "valid": True, "plan": plan_dict}
    return {"approved": False, "valid": True, "plan": plan_dict,
            "note": "user declined the plan; ask what they'd like to change"}
