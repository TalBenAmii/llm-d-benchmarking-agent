"""Event types streamed to the UI over the WebSocket.

Server -> client:
  assistant_text   {text}                 — a chat message from the agent
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
TOOL_CALL = "tool_call"
COMMAND = "command"
OUTPUT = "output"
APPROVAL_REQUEST = "approval_request"
TOOL_RESULT = "tool_result"
SESSION_PLAN = "session_plan"
ERROR = "error"
DONE = "done"
