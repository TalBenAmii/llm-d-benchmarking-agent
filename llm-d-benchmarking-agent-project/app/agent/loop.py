"""The agent control loop: prompt -> LLM -> validated tool dispatch -> approval-gated
execution -> feed results back, until the model stops calling tools.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.agent import events
from app.agent.context_mgmt import compact_messages, estimate_context_size
from app.agent.prompt import build_system_prompt, catalog_brief_message
from app.agent.results_card import build_results_card
from app.agent.session import Session
from app.agent.tool_result_budget import clamp_tool_result_content
from app.llm.provider import LLMProvider, Usage, open_provider_turn
from app.observability.logctx import bind as log_bind
from app.tools.context import ApprovalRejected, QuotaError, ToolError
from app.tools.registry import dispatch, tool_definitions

log = logging.getLogger("app.agent.loop")

MAX_STEPS = 24
_TOOL_RESULT_BUDGET = 6_000  # chars of a tool result fed back to the model

EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]
ApproveFn = Callable[[str, dict[str, Any]], Awaitable[bool]]
# Optional caller-supplied predicate the loop polls between steps to decide whether the
# in-flight turn still has a reason to keep running (e.g. a recipient is still attached and
# the run was not abandoned). Returning False makes the loop STOP cleanly before the next
# LLM call / tool dispatch instead of burning tokens on an unreachable turn. ``None`` (the
# default) means "always continue" — fully backward compatible with every existing caller.
ContinueFn = Callable[[], bool]


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
        should_continue: ContinueFn | None = None,
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
            # ``synthetic: True`` marks this as agent-only context the *user* never typed: the
            # provider still sends it (the wire formatters read only role/content and ignore the
            # flag — see app/llm/*_provider.py), but the UI history renderer and derive_title()
            # both skip synthetic messages so it never shows as a user bubble or leaks into the
            # sidebar chat title.
            session.messages.append({
                "role": "user",
                "synthetic": True,
                # Same budget bound as tool results: a big snapshot (a real cluster's
                # cluster_info + namespaces) gets a VALID truncation envelope, not a JSON object
                # sliced mid-structure. Small snapshots serialize byte-identically (fast path).
                "content": ("[environment pre-probe — read-only snapshot, already gathered for "
                            "you so you don't need to call probe_environment again this turn]\n"
                            + clamp_tool_result_content(session.env_snapshot, 4000)),
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

        # Surface a brand-new chat in the sidebar NOW. The chat only becomes "real" (has a user
        # message → passes SessionManager.list()'s no-messages filter) at this line, but list()
        # reads state.json from disk and the turn's own persist() is the FINAL line below — so
        # without this early write the chat first appears in the sidebar only at end-of-turn
        # `done`, tens of seconds later for a long benchmark turn. Persist once here and ping the
        # UI to refetch its sidebar; the end-of-turn persist() still records the full transcript.
        # SESSION_SAVED is a NON_TURN_EVENT (not buffered/seq-stamped — a mid-turn reconnect
        # already finds the chat on disk), so it is cheap and never pollutes the replay buffer.
        session.persist()
        await emit(events.SESSION_SAVED, {})

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

        # Stream the model's text to the UI as it generates (perceived-latency win): the provider
        # calls this with each delta; the UI appends it to the live assistant bubble, then the
        # per-step ASSISTANT_TEXT below finalizes that bubble with the authoritative text. Emitted
        # as a NON_TURN_EVENT (unbuffered) — see events.py. Providers that don't stream never call
        # it, so only the final ASSISTANT_TEXT shows (unchanged behavior).
        async def on_text(delta: str) -> None:
            await emit(events.ASSISTANT_DELTA, {"text": delta})

        # One provider "turn" spans all of this user turn's steps. For the Claude Agent SDK this
        # keeps ONE warm CLI subprocess for the whole turn instead of spawning a fresh one per step
        # (~3s init each) — see app/llm/agent_sdk_provider.py. Other providers (and the test fakes)
        # transparently get a stateless per-step chat() with identical behavior.
        async with open_provider_turn(
            self._provider, system=system, tools=tools, cache_key=session.id
        ) as agent_turn:
            for _ in range(MAX_STEPS):
                # Abandoned-turn guard (sim-1 00:40): a turn whose recipient is gone and which
                # was never approved into a background run has no reason to keep spending API
                # tokens. The caller (the WS handler) supplies should_continue(); when it reports
                # the turn is abandoned we STOP cleanly here — between steps, never mid-tool — so
                # the next LLM call and tool dispatch don't fire. This is ALSO the natural
                # mid-workflow yield checkpoint (AGENT_FINDINGS 01:36): the predicate is polled
                # before every step, so a disconnect partway through a long approve+start workflow
                # is honored at the next step boundary instead of running to completion unheard.
                if should_continue is not None and not should_continue():
                    log.info("turn.abandoned", extra={
                        "session_id": session.id, "tool_calls": tool_calls_made})
                    break
                try:
                    turn = await agent_turn.chat(session.messages, on_text=on_text)
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
                # The CURRENT context window = total_input of THIS one call (fresh + cache_read +
                # cache_write) — NOT the per-turn sum, which double-counts the cached prefix re-sent
                # each step. Persisted so the meter is right on reload; this is the "context used"
                # number Claude Code shows, and it shrinks when compaction trims the transcript.
                session.last_context_tokens = turn.usage.total_input
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
                    # REAL current context-window occupancy from the provider: total_input of THIS
                    # call (fresh + cache_read + cache_write) — the "context used" number Claude Code
                    # shows. No model limit / percentage: the active model can change (and may be a
                    # remote API), so a hard-coded denominator would be unreliable — show the count.
                    "context_window": {
                        "tokens": turn.usage.total_input,
                        "input": turn.usage.input_tokens,
                        "cache_read": turn.usage.cache_read_tokens,
                        "cache_write": turn.usage.cache_write_tokens,
                    },
                    # DEBUGGING TOKEN USAGE: a cheap (char/4) ESTIMATE of the CURRENT assembled-context
                    # window size + a breakdown (system vs replayed history vs the last tool result),
                    # so the user can SEE context growth and what dominates it — not just cumulative
                    # billed usage. Estimated from the exact system + messages just sent this call.
                    "context_est": estimate_context_size(system, session.messages),
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
                        # Deterministic structured results card (B2): right after an analyze_results
                        # tool result, emit a consistent card carrying the analyzer's exact SLO/Pareto
                        # verdicts (not free-form prose). The single-run report's metrics + charts are
                        # already shown by the frontend report-summary card (driven from the same
                        # locate_and_parse_report result), so we do NOT build a second card from it.
                        # Pure mechanism — build_results_card returns None for anything not renderable.
                        card = build_results_card(tc.name, result)
                        if card is not None:
                            await emit(events.RESULTS_CARD, {"id": tc.id, "card": card})
                    tool_result_msgs.append({
                        "tool_call_id": tc.id,
                        "name": tc.name,
                        # Bound the result to the feed-back budget WITHOUT slicing mid-JSON: an
                        # overflow becomes a valid truncation envelope, never malformed JSON.
                        "content": clamp_tool_result_content(result, _TOOL_RESULT_BUDGET),
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
