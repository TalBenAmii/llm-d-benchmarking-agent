"""In-process MCP server exposing the tool registry to the SDK-native engine.

One SDK MCP tool wrapper per registered ToolSpec. The wrapper is the new home of
everything the old agent loop (``app/agent/loop.py``) did per tool call: emit
``tool_call`` → ``registry.dispatch()`` (the schema gate stays intact) under the
verbatim ApprovalRejected/ToolError ladder → record the wall-clock duration →
name-keyed side effects (approved plan → namespace) → emit ``tool_result`` with
the FULL result → persist card results → emit ``results_card`` → return the full
JSON result to the model. NO clamp/truncation anywhere — results enter the model
context whole; CLI auto-compaction is the only bound (user-decided; design
contract: docs/reference/SDK_NATIVE_ENGINE.md).

The wrapper needs the live per-turn context (session, emit, the pending
``tool_use_id`` handed over by ``can_use_tool``), so the server is built PER TURN
from the engine's ``LiveTurn`` handle rather than once at import.
"""
from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any

from app.agent import events
from app.agent.cards import build_results_card
from app.observability.logging import bind as log_bind
from app.tools.context import ApprovalRejected, ToolError
from app.tools.registry import dispatch, tool_definitions

if TYPE_CHECKING:  # import-time cycle guard: engine.py imports this module
    from app.agent.engine import LiveTurn

log = logging.getLogger("app.tools.mcp_server")

SERVER_NAME = "benchtools"
# The model sees each tool as ``mcp__benchtools__<name>`` on the wire.
TOOL_PREFIX = f"mcp__{SERVER_NAME}__"

# Tools whose FULL result the UI re-renders as a rich card (report summary + clickable charts,
# Pareto/comparison/env/capacity/etc.). Their result is persisted to ``session.card_results``
# so a resumed/reloaded chat replays the card in its transcript position. Keep this in
# lock-step with the dispatch in app/ui/app.js `renderToolResultCards`.
CARD_RESULT_TOOLS = frozenset({
    "locate_and_parse_report", "analyze_results", "compare_reports", "compare_harness_runs",
    "probe_environment", "check_capacity", "check_endpoint_readiness", "advise_accelerators",
    "generate_doe_experiment", "orchestrate_benchmark_run",
    "export_run_bundle",
    # suggest_next_steps carries no metrics card — its result is the {label,prompt} chip list the
    # UI draws as clickable next-step buttons. Persisted here so the buttons replay on
    # resume/reload exactly like the report/analysis cards.
    "suggest_next_steps",
})


def build_benchtools_server(turn: LiveTurn) -> Any:
    """Build the per-turn ``benchtools`` SDK MCP server from the live tool registry.

    Every registered tool is exposed (the lazy-group filter is deliberately absent — the
    SDK-native engine rides all schemas in the cached prefix). The returned config plugs
    straight into ``ClaudeAgentOptions.mcp_servers``."""
    from claude_agent_sdk import create_sdk_mcp_server
    from claude_agent_sdk import tool as sdk_tool

    tools = [
        sdk_tool(t["name"], t["description"], t["input_schema"])(_handler_for(turn, t["name"]))
        for t in tool_definitions()
    ]
    return create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=tools)


def _handler_for(turn: LiveTurn, name: str):
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        result = await execute_tool(turn, name, args or {})
        # Only text/image/resource content survives the SDK's result conversion, so the model
        # receives the result as its full JSON serialization — whole, never clamped.
        return {"content": [
            {"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)},
        ]}
    return handler


async def execute_tool(turn: LiveTurn, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run one registry tool for the SDK-native engine — the per-call pipeline the old loop
    performed inline, relocated verbatim. Serialized under the per-turn lock because
    ``ToolContext`` assumes sequential dispatch (``current_tool_call_id`` and the gate
    bookkeeping are single-slot)."""
    async with turn.tool_lock:
        session = turn.session
        ctx = session.ctx
        # The tool_use id stashed by the engine's can_use_tool gatekeeper just before the SDK
        # dispatched this call — ties events/approvals/durations back to the model's tool call.
        tc_id = turn.take_tool_use_id(name)
        with log_bind(tool=name):
            log.info("tool.call.start", extra={"tool_call_id": tc_id})
            await turn.emit(events.TOOL_CALL, {"id": tc_id, "name": name, "input": args})
            # Tie any approval gate raised inside this dispatch back to its tool call.
            ctx.current_tool_call_id = tc_id
            _t0 = time.monotonic()
            result = await _invoke(ctx, name, args)
            # Persist this tool call's wall-clock run time (keyed to its id) so a resumed/
            # reloaded chat shows the SAME duration badge on the action row a live run does
            # (includes any approval wait, mirroring the live client-side elapsed).
            session.record_tool_duration(tc_id, time.monotonic() - _t0)
            ctx.current_tool_call_id = None

            if (name == "propose_session_plan" and isinstance(result, dict)
                    and result.get("approved")):
                plan = result.get("plan")
                session.approved_plan = plan
                # The approved plan defines this chat's namespace (its sidebar folder). Fill
                # it only if still unset, so a session pre-stamped with a namespace (e.g. the
                # test suite's "test") is never overwritten.
                if plan and not session.namespace:
                    session.namespace = plan.get("namespace")

            # Kept for state parity while both engines coexist on the branch: the persisted
            # ``loaded_groups`` set still gates the OLD loop's exposed tool schemas, so a chat
            # that ran a turn here must not lose groups if the flag flips back. Dies at Phase 5
            # with the lazy-group machinery.
            if name == "load_tools" and isinstance(result, dict):
                session.loaded_groups.update(result.get("loaded") or [])

            log.info("tool.call.result", extra={
                "tool_call_id": tc_id,
                "ok": not (isinstance(result, dict)
                           and ("error" in result or result.get("rejected"))),
            })
            await turn.emit(events.TOOL_RESULT, {"id": tc_id, "name": name, "result": result})
            # Persist the full result of card-rendering tools (keyed to this tool call) so a
            # resumed/reloaded chat can replay the report summary + its clickable charts in place.
            if name in CARD_RESULT_TOOLS:
                session.record_card_result(
                    {"tool_call_id": tc_id, "name": name, "result": result})
            # Deterministic structured results card: right after an analyze_results tool result,
            # emit a consistent card carrying the analyzer's exact SLO/Pareto verdicts. Pure
            # mechanism — build_results_card returns None for anything not renderable.
            card = build_results_card(name, result)
            if card is not None:
                await turn.emit(events.RESULTS_CARD, {"id": tc_id, "card": card})
        # Hand the full result to the engine so the session.messages mirror records it in
        # today's ``tool_results`` shape when the SDK echoes the tool_result block back.
        turn.record_result(tc_id, name, result)
        return result


async def _invoke(ctx: Any, name: str, raw_input: dict[str, Any]) -> dict[str, Any]:
    # The exact except-ladder the old loop used (loop.py:_invoke) — behavior copied verbatim.
    try:
        return await dispatch(ctx, name, raw_input)
    except ApprovalRejected as exc:
        return {"rejected": True, "reason": str(exc),
                "note": "the user declined this action. If a user message follows this "
                        "result, they typed it INSTEAD of approving — treat it as their new "
                        "instruction: adjust accordingly and, if a mutating step is still the "
                        "right next move, propose it again by calling the tool (a fresh "
                        "Approve/Decline card). Otherwise ask what they want to do instead."}
    except ToolError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # never let one tool crash the turn
        return {"error": f"tool {name!r} raised: {exc}"}
