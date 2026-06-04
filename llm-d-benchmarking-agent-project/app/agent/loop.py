"""The agent control loop: prompt -> LLM -> validated tool dispatch -> approval-gated
execution -> feed results back, until the model stops calling tools.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.agent import events
from app.agent.context_mgmt import compact_messages
from app.agent.prompt import build_system_prompt, catalog_brief_message
from app.agent.session import Session
from app.llm.provider import LLMProvider, Usage
from app.observability.logctx import bind as log_bind
from app.tools.context import ApprovalRejected, QuotaError, ToolError
from app.tools.registry import dispatch, tool_definitions

log = logging.getLogger("app.agent.loop")

MAX_STEPS = 24
_TOOL_RESULT_BUDGET = 6_000  # chars of a tool result fed back to the model

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
        # If a read-only environment pre-probe ran in the background while the user typed their
        # first message, hand its snapshot to the model as a synthetic user turn BEFORE the real
        # message — so the agent starts environment-aware without spending an extra LLM turn (or
        # re-running probe_environment this turn). One-shot: flip prewarmed so we never re-inject
        # it on later turns. Mechanism only — what to DO with the snapshot is the model's judgment
        # (guided by knowledge/conversation_style.md).
        if session.env_snapshot is not None and not session.prewarmed:
            session.messages.append({
                "role": "user",
                "content": ("[environment pre-probe — read-only snapshot, already gathered for "
                            "you so you don't need to call probe_environment again this turn]\n"
                            + json.dumps(session.env_snapshot)[:4000]),
            })
            session.prewarmed = True
        # Inject the LIVE catalog ONCE per session as a synthetic conversation message instead of
        # baking it into the (now byte-stable) cached system prefix — so the large prefix reliably
        # cache-hits every turn. One-shot: flip catalog_injected so it is not re-injected (the
        # agent re-enumerates on demand with list_catalog if it suspects it has gone stale).
        if not session.catalog_injected:
            session.messages.append({"role": "user", "content": catalog_brief_message(ctx)})
            session.catalog_injected = True
        session.messages.append({"role": "user", "content": user_text})

        # Context management: compact OLD, superseded tool-result blobs in place once the
        # replayed transcript grows past the threshold (mechanism in app/agent/context_mgmt.py).
        # Never breaks tool-call/result pairing and never touches the recent window.
        reclaimed = compact_messages(session.messages)
        if reclaimed:
            log.info("turn.compacted", extra={"session_id": session.id, "chars_reclaimed": reclaimed})

        log.info("turn.start", extra={"session_id": session.id, "user_chars": len(user_text)})

        system = build_system_prompt(ctx)
        tools = tool_definitions()
        tool_calls_made = 0
        # One user "press enter" runs this loop = several LLM calls; per-turn tokens are the SUM
        # of usage across all of them. Track the running turn total + a call counter so the live
        # UI line ticks up on every step and the per-turn footer is exact.
        turn_usage = Usage()
        calls = 0

        for _ in range(MAX_STEPS):
            try:
                turn = await self._provider.chat(
                    system=system, messages=session.messages, tools=tools, cache_key=session.id,
                )
            except Exception as exc:  # provider/network error
                await emit(events.ERROR, {"message": f"LLM call failed: {exc}"})
                break

            # Accumulate REAL usage: into the running turn total and the persisted session tally.
            # (calls counts only SUCCESSFUL chats — the error path above breaks before here, so
            # it is NOT the loop index; keep the explicit counter.)
            turn_usage += turn.usage
            calls += 1  # noqa: SIM113
            session.total_input_tokens += turn.usage.input_tokens
            session.total_output_tokens += turn.usage.output_tokens
            session.total_cache_read_tokens += turn.usage.cache_read_tokens
            session.total_cache_write_tokens += turn.usage.cache_write_tokens
            await emit(events.USAGE, {
                "turn": {
                    "input": turn_usage.input_tokens,
                    "output": turn_usage.output_tokens,
                    "cache_read": turn_usage.cache_read_tokens,
                    "cache_write": turn_usage.cache_write_tokens,
                    "calls": calls,
                    "total": turn_usage.total_input + turn_usage.output_tokens,
                },
                "session": {
                    "input": session.total_input_tokens,
                    "output": session.total_output_tokens,
                    "cache_read": session.total_cache_read_tokens,
                    "total": session.session_total,
                },
            })

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
                        plan = result.get("plan")
                        session.approved_plan = plan
                        # The approved plan defines this chat's namespace (its sidebar folder). Fill
                        # it only if still unset, so a session pre-stamped with a namespace (e.g. the
                        # test suite's "test") is never overwritten.
                        if plan and not session.namespace:
                            session.namespace = plan.get("namespace")

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
