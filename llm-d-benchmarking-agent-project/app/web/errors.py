"""Formatting of inbound-frame validation errors for the WS protocol ``error`` event.

Pure: turns a Pydantic ``ValidationError`` into the short, human-readable reason the ``/ws``
handler ships back without leaking internals. No ``app`` dependency.
"""
from __future__ import annotations

from app.agent.ws_schemas import ValidationError


def first_validation_message(exc: ValidationError) -> str:
    """A short, human-readable reason from a Pydantic validation error for the protocol
    `error` event — the field path + message of the first error, without leaking internals."""
    errs = exc.errors()
    if not errs:
        return "invalid frame"
    e = errs[0]
    loc = ".".join(str(p) for p in e.get("loc", ())) or "frame"
    return f"{loc}: {e.get('msg', 'invalid')}"
