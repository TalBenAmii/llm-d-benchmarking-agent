"""Official Codex Python SDK bridge to the existing chat and tool contracts.

The SDK owns the model loop, authentication, streaming, and thread persistence.
Its dynamic tools call the same registry wrapper as Claude, including approvals,
schema validation, results cards, and command policy. The synchronous SDK's reader
thread hands calls to our asyncio loop; it never runs application tools itself.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from concurrent.futures import Future
from typing import Any

from openai_codex import InvalidRequestError
from openai_codex.client import CodexClient

from app.agent import events
from app.agent.engine import (
    LIVE_TURNS,
    MAX_TURNS,
    ContinueFn,
    LiveTurn,
    SdkNativeEngine,
    _mirror_replay_text,
)
from app.agent.prompt import build_system_prompt
from app.agent.session import Session
from app.llm.codex_options import codex_config, isolated_thread_config, require_chatgpt
from app.tools.context import ApproveFn, EmitFn
from app.tools.mcp_server import execute_tool
from app.tools.registry import tool_definitions


def _tool_response(result: dict[str, Any]) -> dict[str, Any]:
    return {"success": not (result.get("error") or result.get("rejected")),
            "contentItems": [{"type": "inputText",
                              "text": json.dumps(result, ensure_ascii=False, default=str)}]}


class CodexEngine:
    def __init__(self, client_factory: Any = CodexClient):
        self.client_factory = client_factory or CodexClient

    async def run_turn(
        self, session: Session, user_text: str, *, emit: EmitFn,
        request_approval: ApproveFn, should_continue: ContinueFn | None = None,
    ) -> None:
        ctx = session.ctx
        ctx.emit, ctx.request_approval = emit, request_approval
        prior = list(session.messages)
        query = SdkNativeEngine._first_query(session, user_text)
        session.persist()
        await emit(events.SESSION_SAVED, {})
        turn = LiveTurn(session=session, emit=emit)
        LIVE_TURNS[session.id] = turn
        loop = asyncio.get_running_loop()
        pending: set[Future] = set()
        pending_lock = threading.Lock()
        closing = False
        allowed = {t["name"] for t in tool_definitions()}

        async def invoke(params: dict[str, Any]) -> dict[str, Any]:
            name, args, tid = params.get("tool"), params.get("arguments"), params.get("callId")
            if (not isinstance(name, str) or name not in allowed or not isinstance(args, dict) or not tid
                    or params.get("threadId") != session.codex_thread_id):
                return _tool_response({"error": "unknown or malformed benchmark tool call"})
            if should_continue and not should_continue():
                return _tool_response({"rejected": True, "reason": "conversation detached"})
            turn.stash_tool_use(name, tid, args)
            result = await execute_tool(turn, name, args)
            turn.take_result(tid)
            session.messages.append({"role": "tool_results", "results": [
                {"tool_call_id": tid, "name": name, "content": result},
            ]})
            session.persist()
            return _tool_response(result)

        def on_request(method: str, params: dict[str, Any] | None) -> dict[str, Any]:
            # Fail closed for Codex's built-in execution/permission requests. Only
            # registry tools can act; their approval decisions come from the UI.
            if method != "item/tool/call":
                if method == "item/permissions/requestApproval":
                    return {"permissions": {}, "scope": "turn"}
                return {"decision": "decline"}
            with pending_lock:
                if closing:
                    return _tool_response({"rejected": True, "reason": "turn stopped"})
                future = asyncio.run_coroutine_threadsafe(invoke(params or {}), loop)
                pending.add(future)
            try:
                return future.result()
            except Exception:
                return _tool_response({"error": "tool call interrupted"})
            finally:
                with pending_lock:
                    pending.discard(future)

        client: Any = self.client_factory(codex_config(ctx.settings), approval_handler=on_request)
        totals = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "calls": 0}
        try:
            await asyncio.to_thread(client.start)
            await asyncio.wait_for(asyncio.to_thread(client.initialize), timeout=30)
            await asyncio.wait_for(asyncio.to_thread(require_chatgpt, client), timeout=20)
            config = await asyncio.to_thread(isolated_thread_config, client)
            params = {
                "model": session.model_override or ctx.settings.codex_model,
                "modelProvider": "openai", "approvalPolicy": "never",
                "sandbox": "read-only", "cwd": str(ctx.workspace),
                "baseInstructions": build_system_prompt(ctx), "config": config,
            }
            if session.codex_thread_id:
                try:
                    await asyncio.to_thread(client.thread_resume, session.codex_thread_id, params)
                except InvalidRequestError as exc:
                    # Only a missing transcript warrants replay. Auth/config errors must
                    # fail visibly, and a partially executed turn must never be repeated.
                    if not any(s in str(exc).lower() for s in ("not found", "no rollout", "does not exist")):
                        raise
                    session.codex_thread_id = None
            if not session.codex_thread_id:
                started = await asyncio.to_thread(client.thread_start, {
                    **params, "environments": [], "selectedCapabilityRoots": [],
                    "dynamicTools": [
                        {"type": "function", "name": t["name"],
                         "description": t["description"], "inputSchema": t["input_schema"]}
                        for t in tool_definitions()
                    ],
                })
                session.codex_thread_id = started.thread.id
                session.codex_token_usage = {}
                query = _mirror_replay_text(prior) + query
                session.persist()
            for _ in range(MAX_TURNS):
                started_turn = await asyncio.to_thread(
                    client.turn_start, session.codex_thread_id, query,
                    {"effort": session.effort_override or ctx.settings.codex_effort},
                )
                ok = await self._consume(client, started_turn.turn.id, turn, totals)
                steers = turn.drain_steers() if ok else []
                if not steers:
                    break
                query = "\n\n".join(steers)
                session.messages.append({"role": "user", "content": query})
            else:
                raise RuntimeError(f"reached the step limit ({MAX_TURNS}); pausing")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await emit(events.ERROR, {"message": f"Codex call failed: {exc}"})
        finally:
            with pending_lock:
                closing = True
                for future in pending:
                    future.cancel()
            # Closing the SDK also kills its app-server and unblocks any SDK reader
            # waiting for a notification. Reap before returning the concurrency slot.
            await asyncio.to_thread(client.close)
            ctx.current_tool_call_id = None
            if LIVE_TURNS.get(session.id) is turn:
                del LIVE_TURNS[session.id]
            ctx.steer_messages = turn._steers + ctx.steer_messages
            session.persist()
        await emit(events.DONE, {})

    async def _consume(self, client: Any, turn_id: str, turn: LiveTurn,
                       totals: dict[str, int]) -> bool:
        calls = 0
        terminal = False
        try:
            while True:
                notification = await self._next_event(client, turn_id, turn)
                method = notification.method
                payload = notification.payload.model_dump(mode="json", by_alias=True)
                payload = payload.get("params", payload)  # SDK's forward-compatible unknown event
                if method == "item/agentMessage/delta" and not terminal:
                    await turn.emit(events.ASSISTANT_DELTA, {"text": payload.get("delta", "")})
                elif method == "item/started" and payload["item"]["type"] == "dynamicToolCall":
                    calls += 1
                    if calls > MAX_TURNS:
                        raise RuntimeError(f"reached the step limit ({MAX_TURNS}); pausing")
                    item = payload["item"]
                    turn.session.messages.append({"role": "assistant", "content": "",
                                                  "tool_calls": [{"id": item["id"],
                                                      "name": item["tool"],
                                                      "input": item["arguments"]}]})
                    turn.names_by_id[item["id"]] = item["tool"]
                    turn.note_mirrored()
                elif (method == "item/completed" and payload["item"]["type"] == "dynamicToolCall"
                      and payload["item"].get("tool") == "suggest_next_steps"
                      and payload["item"].get("success")):
                    terminal = True
                elif (method == "item/completed" and payload["item"]["type"] == "agentMessage"
                      and not terminal):
                    text = payload["item"].get("text", "")
                    turn.session.messages.append({"role": "assistant", "content": text,
                                                  "tool_calls": []})
                    await turn.emit(events.ASSISTANT_TEXT, {"text": text})
                elif method == "thread/tokenUsage/updated":
                    await self._usage(turn, payload["tokenUsage"], totals)
                elif method == "turn/completed":
                    result = payload["turn"]
                    if result["status"] != "completed":
                        error = result.get("error") or {}
                        await turn.emit(events.ERROR, {"message": error.get("message")
                                                       or f"Codex turn {result['status']}"})
                        return False
                    return True
        finally:
            client.unregister_turn_notifications(turn_id)

    @staticmethod
    async def _next_event(client: Any, turn_id: str, turn: LiveTurn) -> Any:
        task = asyncio.create_task(asyncio.to_thread(client.next_turn_notification, turn_id))
        watchdog = turn.session.ctx.settings.agent_stream_watchdog_s
        poll = min(5.0, watchdog) if watchdog > 0 else None
        silent = 0.0
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=poll)
                if done:
                    return task.result()
                silent = 0.0 if turn.tool_depth else silent + (poll or 0)
                if watchdog > 0 and silent >= watchdog:
                    raise RuntimeError(f"agent stream stalled for {watchdog:g}s with no tool running")
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    @staticmethod
    async def _usage(turn: LiveTurn, data: dict[str, Any], totals: dict[str, int]) -> None:
        session = turn.session
        current = data["total"]
        previous = session.codex_token_usage
        if current == previous:
            return
        delta = {key: max(0, int(current.get(key, 0)) - int(previous.get(key, 0)))
                 for key in ("inputTokens", "cachedInputTokens", "outputTokens")}
        fresh = max(0, delta["inputTokens"] - delta["cachedInputTokens"])
        session.total_input_tokens += fresh
        session.total_cache_read_tokens += delta["cachedInputTokens"]
        session.total_output_tokens += delta["outputTokens"]
        totals["input"] += fresh
        totals["cache_read"] += delta["cachedInputTokens"]
        totals["output"] += delta["outputTokens"]
        totals["calls"] += 1
        totals["total"] = totals["input"] + totals["cache_read"] + totals["output"]
        session.codex_token_usage = current
        last = data["last"]
        session.last_context_tokens = int(last.get("inputTokens", 0))
        await turn.emit(events.USAGE, {
            "turn": dict(totals),
            "session": {"input": session.total_input_tokens, "output": session.total_output_tokens,
                        "cache_read": session.total_cache_read_tokens, "total": session.session_total},
            "context_window": {"tokens": session.last_context_tokens,
                               "input": max(0, session.last_context_tokens - last.get("cachedInputTokens", 0)),
                               "cache_read": last.get("cachedInputTokens", 0), "cache_write": 0},
            "context": {"total_tokens": session.last_context_tokens,
                        "max_tokens": data.get("modelContextWindow")},
        })
