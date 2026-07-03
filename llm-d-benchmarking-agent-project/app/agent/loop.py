"""The agent control loop: prompt -> LLM -> validated tool dispatch -> approval-gated
execution -> feed results back, until the model stops calling tools.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.agent import events
from app.agent.cards import build_results_card
from app.agent.context_mgmt import (
    DEFAULT_TOOL_RESULT_BUDGET,
    clamp_tool_result_content,
    compact_messages,
    estimate_context_size,
)
from app.agent.prompt import build_system_prompt, catalog_brief_message
from app.agent.session import Session
from app.llm.provider import LLMProvider, Usage, open_provider_turn
from app.observability.cot_trace import TurnTrace
from app.observability.logging import bind as log_bind
from app.tools.context import ApprovalRejected, QuotaError, ToolError
from app.tools.registry import dispatch, tool_definitions

log = logging.getLogger("app.agent.loop")

MAX_STEPS = 24
_TOOL_RESULT_BUDGET = DEFAULT_TOOL_RESULT_BUDGET  # chars of a tool result fed back to the model

# Tools whose FULL result the UI re-renders as a rich card (report summary + clickable charts,
# Pareto/comparison/env/capacity/etc.). Their un-clamped result is persisted to
# ``session.card_results`` so a resumed/reloaded chat replays the card in its transcript
# position — the LLM-facing copy in ``messages`` is budget-clamped and unusable for rendering.
# Keep this in lock-step with the dispatch in ui/app.js `renderToolResultCards`.
CARD_RESULT_TOOLS = frozenset({
    "locate_and_parse_report", "analyze_results", "compare_reports", "compare_harness_runs",
    "probe_environment", "check_capacity", "check_endpoint_readiness", "advise_accelerators",
    "generate_doe_experiment", "orchestrate_benchmark_run",
    "export_run_bundle",
    # suggest_next_steps carries no metrics card — its result is the {label,prompt} chip list the
    # UI draws as clickable next-step buttons. Persisted here so the buttons replay on resume/reload
    # exactly like the report/analysis cards (the chip payload is tiny, so the messages copy would
    # also survive the feed-back clamp — but persisting keeps it on the same replay path).
    "suggest_next_steps",
})

EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]
ApproveFn = Callable[[str, dict[str, Any]], Awaitable[bool]]
# Optional caller-supplied predicate the loop polls between steps to decide whether the
# in-flight turn still has a reason to keep running (e.g. a recipient is still attached and
# the run was not abandoned). Returning False makes the loop STOP cleanly before the next
# LLM call / tool dispatch instead of burning tokens on an unreachable turn. ``None`` (the
# default) means "always continue" — fully backward compatible with every existing caller.
ContinueFn = Callable[[], bool]


@dataclass
class StepResult:
    """What one step of the turn loop reports back to ``run_turn``.

    ``should_break`` ends the step loop WITHOUT triggering the ``for...else`` step-limit branch
    (the abandoned-turn guard, a provider error, and the normal end-of-turn stop all set it).
    The counters accumulate ACROSS steps, so each step returns their new running values and the
    outer loop carries them into the next step (and into the final trace/log)."""
    tool_calls_made: int
    turn_usage: Usage
    calls: int
    should_break: bool


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
        # Chain-of-thought debug trace: append the model's reasoning + every decision this turn
        # to the session's OWN folder (<workspace>/sessions/<id>/cot_trace.jsonl), so a mistake
        # can be debugged after the fact. Always on — best-effort and never raises into the turn.
        trace = TurnTrace.for_session(ctx.workspace)
        trace.event("turn_start", session_id=session.id, user_text=user_text)
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
        def _compact() -> None:
            reclaimed = compact_messages(session.messages)
            if reclaimed:
                log.info("turn.compacted",
                         extra={"session_id": session.id, "chars_reclaimed": reclaimed})

        _compact()

        log.info("turn.start", extra={"session_id": session.id, "user_chars": len(user_text)})

        system = build_system_prompt(ctx)
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
        #
        # The exposed tool set can change mid-turn: when the model calls load_tools the loop folds
        # the requested group(s) into session.loaded_groups, and we re-open the provider turn with
        # the now-larger set so the group's tools are callable on the very next step (no dead turn).
        # The SDK binds tools at connect, so a changed set needs a fresh turn; this re-open happens
        # once per distinct load_tools call (typically 2-3 across a deploy→run→analyze session). The
        # step budget (MAX_STEPS) and the running counters span all re-opens.
        done = False
        while not done:
            exposed_groups = frozenset(session.loaded_groups)
            tools = tool_definitions(loaded=exposed_groups)
            async with open_provider_turn(
                self._provider, system=system, tools=tools, cache_key=session.id
            ) as agent_turn:
                while True:
                    if calls >= MAX_STEPS:
                        await emit(events.ERROR,
                                   {"message": f"reached the step limit ({MAX_STEPS}); pausing."})
                        log.warning("turn.step_limit", extra={"max_steps": MAX_STEPS})
                        done = True
                        break
                    step = await self._run_step(
                        session=session,
                        ctx=ctx,
                        agent_turn=agent_turn,
                        emit=emit,
                        trace=trace,
                        system=system,
                        on_text=on_text,
                        compact=_compact,
                        should_continue=should_continue,
                        tool_calls_made=tool_calls_made,
                        turn_usage=turn_usage,
                        calls=calls,
                    )
                    tool_calls_made = step.tool_calls_made
                    turn_usage = step.turn_usage
                    calls = step.calls
                    if step.should_break:
                        done = True
                        break
                    # Model just loaded a tool group: leave the inner loop so the outer one
                    # re-opens the provider turn with the expanded set (same user turn).
                    if frozenset(session.loaded_groups) != exposed_groups:
                        break

        trace.event("turn_end", tool_calls=tool_calls_made, llm_calls=calls)
        log.info("turn.end", extra={"session_id": session.id, "tool_calls": tool_calls_made})
        session.persist()
        await emit(events.DONE, {})

    async def _run_step(
        self,
        *,
        session: Session,
        ctx,
        agent_turn,
        emit: EmitFn,
        trace: TurnTrace,
        system: str,
        on_text: Callable[[str], Awaitable[None]],
        compact: Callable[[], None],
        should_continue: ContinueFn | None,
        tool_calls_made: int,
        turn_usage: Usage,
        calls: int,
    ) -> StepResult:
        """One step of the turn loop: continue-check → compact → LLM call → usage accounting →
        append assistant message + trace → tool-call dispatch → tool_results append → steer drain
        → loop-break decision. Pure code motion out of ``run_turn`` — the across-step counters
        (``tool_calls_made``, ``turn_usage``, ``calls``) arrive as their current running values
        and the returned StepResult carries the updated values back. ``should_break`` ending the
        loop never trips the ``for...else`` step-limit branch (same as the original ``break``)."""
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
            return StepResult(tool_calls_made, turn_usage, calls, should_break=True)
        # Re-compact BEFORE every step, not only once at turn start. A single user turn
        # replays the WHOLE transcript on every step and a long multi-step turn appends
        # many large tool results — so the replayed context can blow past the threshold
        # WITHIN the turn. A start-of-turn-only compaction can never fire while the turn
        # that overflows it is still running, leaving the growing history re-sent in full
        # to the provider every step (eventually a context-overflow error). compact_messages
        # is idempotent and a cheap no-op below the threshold, so re-checking each step only
        # acts once the transcript actually crosses it — and never breaks tool-call/result
        # pairing or touches the recent window.
        compact()
        try:
            turn = await agent_turn.chat(session.messages, on_text=on_text)
        except Exception as exc:  # provider/network error
            await emit(events.ERROR, {"message": f"LLM call failed: {exc}"})
            return StepResult(tool_calls_made, turn_usage, calls, should_break=True)

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
        # Record this LLM call to the debug trace: the model's chain-of-thought, the text
        # it produced, the tool calls it decided on, and this call's token usage. The
        # reasoning is NEVER added to session.messages (it would bloat context) — only here.
        trace.event(
            "step",
            step=calls,
            thinking=turn.thinking,
            text=turn.text or None,
            tool_calls=[{"name": tc.name, "input": tc.input} for tc in turn.tool_calls],
            usage={"input": turn.usage.input_tokens, "output": turn.usage.output_tokens,
                   "cache_read": turn.usage.cache_read_tokens},
        )
        if turn.text:
            await emit(events.ASSISTANT_TEXT, {"text": turn.text})

        tool_result_msgs = []
        for tc in turn.tool_calls:  # empty when the model is done -> no tool_results block
            tool_calls_made += 1
            # Bind the tool name into the log context so every record emitted while this
            # tool runs (incl. the command runner's exec line) carries `tool` alongside
            # the turn's corr_id + session_id.
            with log_bind(tool=tc.name):
                log.info("tool.call.start", extra={"tool_call_id": tc.id})
                await emit(events.TOOL_CALL, {"id": tc.id, "name": tc.name, "input": tc.input})
                # Tie any approval gate raised inside this dispatch back to its tool call.
                ctx.current_tool_call_id = tc.id
                _t0 = time.monotonic()
                result = await self._invoke(ctx, tc.name, tc.input)
                # Persist this tool call's wall-clock run time (keyed to its id) so a
                # resumed/reloaded chat shows the SAME duration badge on the action row a
                # live run does — not just the read-only/mutating badge. (Includes any
                # approval wait, mirroring the live client-side d._t0 elapsed.)
                session.record_tool_duration(tc.id, time.monotonic() - _t0)
                ctx.current_tool_call_id = None

                if tc.name == "propose_session_plan" and isinstance(result, dict) and result.get("approved"):
                    plan = result.get("plan")
                    session.approved_plan = plan
                    # The approved plan defines this chat's namespace (its sidebar folder). Fill
                    # it only if still unset, so a session pre-stamped with a namespace (e.g. the
                    # test suite's "test") is never overwritten.
                    if plan and not session.namespace:
                        session.namespace = plan.get("namespace")

                # The model asked to load tool group(s). Fold them into the (persisted) session
                # set; run_turn detects the change between steps and re-opens the provider turn with
                # the expanded tool list, so the group's tools are callable on the very next step of
                # THIS turn (registry.tool_definitions(loaded=...)). The handler validated the group
                # names against LoadToolsInput and echoes them back in result["loaded"].
                if tc.name == "load_tools" and isinstance(result, dict):
                    session.loaded_groups.update(result.get("loaded") or [])

                log.info("tool.call.result", extra={
                    "tool_call_id": tc.id,
                    "ok": not (isinstance(result, dict) and ("error" in result or result.get("rejected"))),
                })
                await emit(events.TOOL_RESULT, {"id": tc.id, "name": tc.name, "result": result})
                # Persist the full result of card-rendering tools (keyed to this tool
                # call) so a resumed/reloaded chat can replay the report summary + its
                # clickable charts in place — the budget-clamped LLM copy below can't
                # drive the renderer. _history_items interleaves these on resume.
                if tc.name in CARD_RESULT_TOOLS:
                    session.record_card_result(
                        {"tool_call_id": tc.id, "name": tc.name, "result": result})
                # Deterministic structured results card (B2): right after an analyze_results
                # tool result, emit a consistent card carrying the analyzer's exact SLO/Pareto
                # verdicts (not free-form prose). The single-run report's metrics + charts are
                # already shown by the frontend report-summary card (driven from the same
                # locate_and_parse_report result), so we do NOT build a second card from it.
                # Pure mechanism — build_results_card returns None for anything not renderable.
                card = build_results_card(tc.name, result)
                if card is not None:
                    await emit(events.RESULTS_CARD, {"id": tc.id, "card": card})
            clamped = clamp_tool_result_content(result, _TOOL_RESULT_BUDGET)
            tool_result_msgs.append({
                "tool_call_id": tc.id,
                "name": tc.name,
                # Bound the result to the feed-back budget WITHOUT slicing mid-JSON: an
                # overflow becomes a valid truncation envelope, never malformed JSON.
                "content": clamped,
            })
            # Trace the result the model will actually see (the same budget-clamped text),
            # so the debug record shows the exact evidence each decision was based on.
            trace.event("tool_result", tool=tc.name, id=tc.id, result=clamped)

        # A tool_results block is appended ONLY when the model actually called tools, so a
        # final text-only step never leaves a dangling empty block before any drained steer.
        if turn.tool_calls:
            session.messages.append({"role": "tool_results", "results": tool_result_msgs})

        # suggest_next_steps is TERMINAL: offering the "what next?" buttons IS the end of the
        # turn. The chips were already emitted + persisted above, so STOP here instead of
        # giving the model another LLM step — left to continue, it reliably appends a
        # redundant closer ("Use the buttons below to choose your next step"), which is
        # exactly the prose the buttons are meant to REPLACE. Mechanism, not judgment: WHAT
        # to offer stays the model's call; this only enforces that the offer ends the turn.
        offered_next_steps = any(tc.name == "suggest_next_steps" for tc in turn.tool_calls)

        # Mid-turn user STEER (Claude-Code style). The WS handler drops any message the user
        # types while this turn runs into ctx.steer_messages — whether they typed mid-thinking
        # (no gate open) or INSTEAD of approving an open gate (the handler also declines the
        # gate, so the tool result above is a rejection). Surface each captured message as a
        # real user turn now, so this SAME turn picks it up at the next step and the model
        # responds to the steer — adjusting and, if appropriate, re-proposing a fresh approval
        # card — instead of it being dropped. Appended AFTER any tool_results block above so
        # tool-call/result pairing is never broken. Persisted with the turn.
        steered = False
        if ctx.steer_messages:
            for steer in ctx.steer_messages:
                session.messages.append({"role": "user", "content": steer})
            ctx.steer_messages = []
            steered = True

        # End the turn when the model made NO tool calls, OR it just offered next-step
        # buttons (terminal — see above) — UNLESS a steer is waiting. A queued steer keeps
        # the SAME turn alive (loop again) so the agent answers it rather than the message
        # hanging until the user's next turn (and outranks the terminal-offer stop: the user
        # typed something, so respond to it instead of parking on the buttons).
        should_break = (not turn.tool_calls or offered_next_steps) and not steered
        return StepResult(tool_calls_made, turn_usage, calls, should_break=should_break)

    async def _invoke(self, ctx, name: str, raw_input: dict[str, Any]) -> dict[str, Any]:
        try:
            return await dispatch(ctx, name, raw_input)
        except ApprovalRejected as exc:
            return {"rejected": True, "reason": str(exc),
                    "note": "the user declined this action. If a user message follows this "
                            "result, they typed it INSTEAD of approving — treat it as their new "
                            "instruction: adjust accordingly and, if a mutating step is still the "
                            "right next move, propose it again by calling the tool (a fresh "
                            "Approve/Decline card). Otherwise ask what they want to do instead."}
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
