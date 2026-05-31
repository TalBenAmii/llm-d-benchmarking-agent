"""Event types streamed to the UI over the WebSocket.

Server -> client:
  assistant_text   {text}                 — a chat message from the agent
  tool_call        {id, name, input}       — the agent invoked a tool
  output           {line}                  — a streamed line of command stdout/stderr
  approval_request {request_id, kind, payload}  — needs Approve/Reject
  tool_result      {id, name, result}      — a tool finished
  session_plan     {plan}                  — a proposed plan (also an approval_request)
  error            {message}
  done             {}                      — the agent finished this turn

Client -> server:
  user_message     {text}
  approval         {request_id, approved}
"""
from __future__ import annotations

ASSISTANT_TEXT = "assistant_text"
TOOL_CALL = "tool_call"
OUTPUT = "output"
APPROVAL_REQUEST = "approval_request"
TOOL_RESULT = "tool_result"
SESSION_PLAN = "session_plan"
ERROR = "error"
DONE = "done"
