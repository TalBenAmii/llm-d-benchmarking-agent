"""The agent control loop: prompt -> LLM -> validated tool dispatch -> approval-gated
execution -> feed results back, until the model stops calling tools.
"""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from app.agent import events
from app.agent.prompt import build_system_prompt
from app.agent.session import Session
from app.llm.provider import LLMProvider
from app.tools.context import ApprovalRejected, ToolError
from app.tools.registry import dispatch, tool_definitions

MAX_STEPS = 24
_TOOL_RESULT_BUDGET = 20_000  # chars of a tool result fed back to the model

EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]
ApproveFn = Callable[[str, dict[str, Any]], Awaitable[bool]]


class AgentLoop:
    def __init__(self, provider: LLMProvider):
        self._provider = provider

    async def run_turn(
        self,
        session: Session,
        user_text: str,
        *,
        emit: EmitFn,
        request_approval: ApproveFn,
    ) -> None:
        ctx = session.ctx
        ctx.emit = emit
        ctx.request_approval = request_approval
        session.messages.append({"role": "user", "content": user_text})

        system = build_system_prompt(ctx)
        tools = tool_definitions()

        for _ in range(MAX_STEPS):
            try:
                turn = await self._provider.chat(system=system, messages=session.messages, tools=tools)
            except Exception as exc:  # provider/network error
                await emit(events.ERROR, {"message": f"LLM call failed: {exc}"})
                break

            session.messages.append({
                "role": "assistant",
                "content": turn.text or "",
                "tool_calls": [{"id": tc.id, "name": tc.name, "input": tc.input} for tc in turn.tool_calls],
            })
            if turn.text:
                await emit(events.ASSISTANT_TEXT, {"text": turn.text})

            if not turn.tool_calls:
                break  # the model is done for this turn

            tool_result_msgs = []
            for tc in turn.tool_calls:
                await emit(events.TOOL_CALL, {"id": tc.id, "name": tc.name, "input": tc.input})
                result = await self._invoke(ctx, tc.name, tc.input)

                if tc.name == "propose_session_plan" and isinstance(result, dict) and result.get("approved"):
                    session.approved_plan = result.get("plan")

                await emit(events.TOOL_RESULT, {"id": tc.id, "name": tc.name, "result": result})
                tool_result_msgs.append({
                    "tool_call_id": tc.id,
                    "name": tc.name,
                    "content": json.dumps(result)[:_TOOL_RESULT_BUDGET],
                })

            session.messages.append({"role": "tool_results", "results": tool_result_msgs})
        else:
            await emit(events.ERROR, {"message": f"reached the step limit ({MAX_STEPS}); pausing."})

        session.persist()
        await emit(events.DONE, {})

    async def _invoke(self, ctx, name: str, raw_input: dict[str, Any]) -> dict[str, Any]:
        try:
            return await dispatch(ctx, name, raw_input)
        except ApprovalRejected as exc:
            return {"rejected": True, "reason": str(exc),
                    "note": "the user declined this action; ask what they want to do instead"}
        except ToolError as exc:
            return {"error": str(exc)}
        except Exception as exc:  # never let one tool crash the loop
            return {"error": f"tool {name!r} raised: {exc}"}
