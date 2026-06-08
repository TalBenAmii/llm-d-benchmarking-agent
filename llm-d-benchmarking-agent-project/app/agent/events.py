"""Event types streamed to the UI over the WebSocket.

Server -> client:
  assistant_text   {text}                 — a chat message from the agent (the FINAL, authoritative
                                            text for one step; buffered + replayed). When a step
                                            streamed `assistant_delta`s, this finalizes that bubble.
  assistant_delta  {text}                 — a token-by-token text fragment streamed live as the
                                            model generates, so the UI fills the assistant bubble
                                            in real time instead of waiting for the whole step.
                                            Live-only (a NON_TURN_EVENT: unbuffered/seqless); the
                                            step's `assistant_text` carries the complete text.
  tool_call        {id, name, input}       — the agent invoked a tool
  command          {argv, text, mode, auto_run}  — EVERY command actually executed,
                                            including auto-run read-only probes. Lets the UI
                                            show the full executed-command trail and power a
                                            debug view. Emitted just before the process runs
                                            (after approval, for mutating commands).
  output           {line}                  — a streamed line of command stdout/stderr
  approval_request {request_id, kind, payload}  — needs Approve/Reject
  tool_result      {id, name, result}      — a tool finished
  session_plan     {plan}                  — a proposed plan (also an approval_request)
  error            {message[, kind]}        — a turn/agent error; kind="protocol_error" for a
                                            rejected malformed inbound frame (Phase 15)
  cancelled        {message}               — the in-flight run/turn was cancelled (Phase 16);
                                            its concurrency slot is freed and subprocess reaped
  usage            {turn:{input,output,cache_read,cache_write,calls,total},
                    session:{input,output,cache_read,total},
                    context_est:{total_chars,total_tokens_est,system_*,history_*,
                                 last_tool_result_*}}
                                           — REAL token usage from the provider API. A PER-TURN
                                            event emitted on every LLM call (the live UI line
                                            ticks up): turn.* are the RUNNING totals for the
                                            in-progress turn, session.* the running session totals.
                                            context_est is a cheap (char/4) ESTIMATE of the CURRENT
                                            assembled-context window size + a breakdown (system vs
                                            replayed history vs the last tool result) so the user
                                            can see context GROWTH and what dominates it — NOT a
                                            tokenizer count (see app/agent/context_mgmt.py
                                            estimate_context_size).
  welcome          {heading, bullets:[str], nudge}
                                           — DETERMINISTIC start-of-chat greeting, emitted by the
                                            backend (NOT the LLM, no token cost) on a brand-new
                                            connection ONLY (never on resume) right before
                                            `suggestions`. Consistent every time; its judgment text
                                            comes from knowledge/welcome.md. A connection-lifecycle
                                            frame, not a turn event.
  suggestions      {chips:[{label,prompt}]}— start-of-chat suggestion chips, emitted ONCE on a
                                            brand-new connection (never on resume) right after
                                            `welcome`. A connection-lifecycle frame, not a turn event.
  session_saved    {}                       — the session was just persisted at the START of a turn
                                            (right after the user message is recorded), so a
                                            brand-new chat lands on disk and the client can refresh
                                            its recent-chats sidebar IMMEDIATELY instead of waiting
                                            for end-of-turn `done`. A NON_TURN_EVENT (not buffered):
                                            a mid-turn reconnect already finds the chat via /api/sessions.
  results_card     {model, harness, ...}    — DETERMINISTIC structured results summary, emitted by
                                            the backend right after a locate_and_parse_report /
                                            analyze_results tool result that carried a validated
                                            Benchmark Report v0.2 summary. Built from the validated
                                            summary + analyzer verdicts (not free-form prose), so the
                                            results card is identical every run. A turn event
                                            (buffered + replayed like tool_call/tool_result).
  resource_stats   {available, namespace?, rows?, note?}
                                           — LIVE cluster CPU/memory for the running benchmark's
                                            pods, streamed by the backend resource poller during a
                                            run at a fixed interval. ZERO LLM cost (it never enters
                                            the message stream). Frequent — see NON_TURN_EVENTS.
  done             {}                      — the agent finished this turn

Client -> server (validated against app.agent.ws_schemas; a malformed frame is rejected with
an error event of kind "protocol_error" and the connection is kept alive):
  user_message     {text}
  approval         {request_id, approved}
  ping             {}                      — keep-alive; answered with a `pong` event

On reconnect mid-turn the server replays the in-flight turn's buffered live events (Phase 15)
so a client that dropped catches up to the LIVE stream, then continues live.
"""
from __future__ import annotations

ASSISTANT_TEXT = "assistant_text"
ASSISTANT_DELTA = "assistant_delta"
TOOL_CALL = "tool_call"
COMMAND = "command"
OUTPUT = "output"
APPROVAL_REQUEST = "approval_request"
TOOL_RESULT = "tool_result"
SESSION_PLAN = "session_plan"
ERROR = "error"
CANCELLED = "cancelled"
USAGE = "usage"
SUGGESTIONS = "suggestions"
WELCOME = "welcome"
SESSION_SAVED = "session_saved"
RESULTS_CARD = "results_card"
RESOURCE_STATS = "resource_stats"
DONE = "done"

# Connection-lifecycle frames: emitted by the /ws handler on (re)connect, NOT part of any
# turn's live stream. They must be excluded from the per-turn live buffer (Phase 15) so a
# second mid-turn reconnect doesn't replay a stale `ready`/`history`/`pong` interleaved before
# the real missed turn events.
READY = "ready"
HISTORY = "history"
PONG = "pong"

# Event types that are NOT buffered into the per-turn live ring. Two kinds:
#   * lifecycle frames (ready/history/pong/welcome/suggestions) — emitted by the /ws handler on
#     (re)connect, not part of any turn's live stream; buffering them would replay a stale
#     handshake on a second mid-turn reconnect;
#   * resource_stats — the live resource poller streams these every few seconds during a run.
#     They are frequent and disposable; buffering them would evict the REAL turn events (tool
#     calls, command lines, assistant text) from the bounded ring, so a mid-turn reconnect would
#     replay a wall of stat samples instead of the progress it actually missed.
#   * session_saved — a one-shot "the chat is now on disk; refresh your sidebar" ping emitted at
#     the start of a turn. A mid-turn reconnect already finds the chat via /api/sessions, so
#     buffering/replaying it would be pure noise.
#   * assistant_delta — token-by-token text streamed live as the model generates (perceived-
#     latency win). High-frequency and transient: buffering deltas would evict the REAL turn
#     events from the bounded ring. They are unbuffered/seqless and live-only — a mid-turn
#     reconnect simply doesn't see the in-flight step's partial text, then the step's FINAL
#     `assistant_text` (which IS buffered + seq-stamped) replays the complete block.
# results_card is DELIBERATELY NOT here: it is a TURN event (emitted during the turn, right after
# the report/analysis tool result), so it must be buffered + seq-stamped + replayed like a
# tool_call/tool_result so a mid-turn reconnect still catches it.
# The buffer therefore holds only the in-flight TURN's meaningful events, exactly as replay_live
# promises.
NON_TURN_EVENTS = frozenset(
    {READY, HISTORY, PONG, WELCOME, SUGGESTIONS, SESSION_SAVED, RESOURCE_STATS, ASSISTANT_DELTA}
)
