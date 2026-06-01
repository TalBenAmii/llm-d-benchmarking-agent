"""The agent control loop: prompt -> LLM -> validated tool dispatch -> approval-gated
execution -> feed results back, until the model stops calling tools.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable

from app.agent import events
from app.agent.prompt import build_system_prompt
from app.agent.session import Session
from app.llm.provider import LLMProvider
from app.observability.logctx import bind as log_bind
from app.tools.context import ApprovalRejected, QuotaError, ToolError
from app.tools.registry import dispatch, tool_definitions

log = logging.getLogger("app.agent.loop")

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

        log.info("turn.start", extra={"session_id": session.id, "user_chars": len(user_text)})

        system = build_system_prompt(ctx)
        tools = tool_definitions()
        tool_calls_made = 0

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
                tool_calls_made += 1
                # Bind the tool name into the log context so every record emitted while this
                # tool runs (incl. the command runner's exec line) carries `tool` alongside
                # the turn's corr_id + session_id.
                with log_bind(tool=tc.name):
                    log.info("tool.call.start", extra={"tool_call_id": tc.id})
                    await emit(events.TOOL_CALL, {"id": tc.id, "name": tc.name, "input": tc.input})
                    # Tie any approval gate raised inside this dispatch back to its tool call.
                    ctx.current_tool_call_id = tc.id
                    result = await self._invoke(ctx, tc.name, tc.input)
                    ctx.current_tool_call_id = None

                    if tc.name == "propose_session_plan" and isinstance(result, dict) and result.get("approved"):
                        session.approved_plan = result.get("plan")

                    log.info("tool.call.result", extra={
                        "tool_call_id": tc.id,
                        "ok": not (isinstance(result, dict) and ("error" in result or result.get("rejected"))),
                    })
                    await emit(events.TOOL_RESULT, {"id": tc.id, "name": tc.name, "result": result})
                tool_result_msgs.append({
                    "tool_call_id": tc.id,
                    "name": tc.name,
                    "content": json.dumps(result)[:_TOOL_RESULT_BUDGET],
                })

            session.messages.append({"role": "tool_results", "results": tool_result_msgs})
        else:
            await emit(events.ERROR, {"message": f"reached the step limit ({MAX_STEPS}); pausing."})
            log.warning("turn.step_limit", extra={"max_steps": MAX_STEPS})

        log.info("turn.end", extra={"session_id": session.id, "tool_calls": tool_calls_made})
        session.persist()
        await emit(events.DONE, {})

    async def _invoke(self, ctx, name: str, raw_input: dict[str, Any]) -> dict[str, Any]:
        try:
            return await dispatch(ctx, name, raw_input)
        except ApprovalRejected as exc:
            return {"rejected": True, "reason": str(exc),
                    "note": "the user declined this action; ask what they want to do instead"}
        except QuotaError as exc:
            # Over an allowlist-declared usage quota — refused pre-execution. Surface the
            # caps so the agent can explain the limit instead of silently failing.
            return {"quota_exceeded": True, "reason": str(exc),
                    "key": exc.key, "window": exc.window, "cap": exc.cap, "used": exc.used,
                    "note": "this command hit its configured usage quota; tell the user the "
                            "limit was reached and ask whether to wait or adjust the plan"}
        except ToolError as exc:
            return {"error": str(exc)}
        except Exception as exc:  # never let one tool crash the loop
            return {"error": f"tool {name!r} raised: {exc}"}
