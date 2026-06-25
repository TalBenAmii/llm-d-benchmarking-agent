"""WebSocket wire-protocol schemas (Phase 15).

The ``/ws`` handler is the browser-facing security/protocol boundary, so every *inbound*
frame is validated against an explicit Pydantic model before the handler acts on it. A
malformed frame is rejected with a structured ``error`` event and the socket is KEPT alive
(an unvalidated ``msg.get(...)`` would let a bad/hostile frame silently no-op, or — for a
non-dict payload — crash the handler). This module is pure schema/mechanism: it encodes no
agent judgment, only the shape of the protocol.

Inbound (client -> server) frames are a tagged union discriminated on ``type``:
  user_message     {type, text}
  approval         {type, request_id, approved}
  cancel           {type}                    — cancel this chat's in-flight run (Phase 16)
  set_auto_approve {type, enabled}           — toggle per-session auto-approve of command gates
  ping             {type}

Outbound (server -> client) frames are uniformly ``{type, data}`` (see :func:`outbound`);
the typed envelope below documents that shape and gives a single place to serialize it.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

__all__ = [
    "UserMessageIn",
    "ApprovalIn",
    "CancelIn",
    "SetAutoApproveIn",
    "PingIn",
    "InboundMessage",
    "INBOUND_ADAPTER",
    "parse_inbound",
    "outbound",
    "ValidationError",
]


class UserMessageIn(BaseModel):
    """A new chat turn from the user."""
    model_config = {"extra": "forbid"}

    type: Literal["user_message"]
    text: str = ""


class ApprovalIn(BaseModel):
    """The user's Approve/Reject decision on a pending gate."""
    model_config = {"extra": "forbid"}

    type: Literal["approval"]
    request_id: str = Field(..., min_length=1)
    approved: bool


class CancelIn(BaseModel):
    """Cancel this chat's in-flight run/turn (Phase 16): the user clicked Stop. Frees the run's
    concurrency slot and reaps its subprocess. Idempotent — a no-op if nothing is running."""
    model_config = {"extra": "forbid"}

    type: Literal["cancel"]


class SetAutoApproveIn(BaseModel):
    """Toggle this chat's per-session auto-approve of COMMAND gates (the UI button). When
    ``enabled`` is True the Channel auto-approves every kind=="command" gate without prompting;
    the session-plan gate is never auto-approved. Server-authoritative + persisted."""
    model_config = {"extra": "forbid"}

    type: Literal["set_auto_approve"]
    enabled: bool


class PingIn(BaseModel):
    """A keep-alive probe; the server answers with a ``pong`` event."""
    model_config = {"extra": "forbid"}

    type: Literal["ping"]


# Tagged union over the ``type`` field: Pydantic picks the right model and reports a clean
# error for an unknown/missing tag or a malformed payload. Keeping the discriminator explicit
# means a new inbound frame is a one-line addition here, not a branch in the handler.
InboundMessage = Annotated[
    UserMessageIn | ApprovalIn | CancelIn | SetAutoApproveIn | PingIn,
    Field(discriminator="type"),
]

INBOUND_ADAPTER: TypeAdapter[InboundMessage] = TypeAdapter(InboundMessage)


def parse_inbound(raw: Any) -> InboundMessage:
    """Validate one decoded inbound frame, or raise ``pydantic.ValidationError``.

    ``raw`` is whatever ``websocket.receive_json()`` decoded (any JSON value). A non-dict
    or unknown ``type`` raises rather than silently no-ops, so the handler can answer with a
    structured ``error`` and keep the connection alive.
    """
    return INBOUND_ADAPTER.validate_python(raw)


def outbound(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Build the canonical outbound frame for one event. Single point of truth for the
    server->client envelope shape (kept trivial so it stays mechanism, not judgment)."""
    return {"type": event_type, "data": payload}
