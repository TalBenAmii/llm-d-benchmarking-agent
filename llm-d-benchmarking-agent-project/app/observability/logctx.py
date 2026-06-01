"""Per-turn log correlation context (Phase 11).

A single ``contextvars`` carrier holds the correlation fields that should ride along on
*every* log record emitted while handling one WebSocket turn — without threading an
argument through the agent loop, tool dispatch, and the command runner. The
:class:`~app.observability.logging.ContextFilter` reads these and stamps them onto each
record, so a corr_id set at the WS handshake automatically appears on records from the
loop, a tool, and the runner.

Pure mechanism: this only *carries* identifiers. It makes no decisions — what to do with a
correlated trail is the operator's / agent's judgment, never code here. ``contextvars`` is
asyncio-aware: each task sees the value bound on its own logical call stack, so concurrent
turns (parallel sessions) don't bleed correlation ids into each other.
"""
from __future__ import annotations

import contextvars
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

# The correlation fields. Empty string = "unset" (rendered as absent, never a bogus value).
_corr_id: contextvars.ContextVar[str] = contextvars.ContextVar("corr_id", default="")
_session_id: contextvars.ContextVar[str] = contextvars.ContextVar("session_id", default="")
_run_id: contextvars.ContextVar[str] = contextvars.ContextVar("run_id", default="")
_tool: contextvars.ContextVar[str] = contextvars.ContextVar("tool", default="")

# The field names a log record carries, in a stable order. The JSON formatter and the
# ContextFilter both read this so the two never drift.
LOG_CONTEXT_FIELDS = ("corr_id", "session_id", "run_id", "tool")


def new_corr_id() -> str:
    """A fresh correlation id (one per WS connection/turn). Short hex — enough to grep a run."""
    return uuid.uuid4().hex[:16]


def get_corr_id() -> str:
    return _corr_id.get()


def get_log_context() -> dict[str, str]:
    """The currently-bound correlation fields, omitting any that are unset (empty)."""
    values = {
        "corr_id": _corr_id.get(),
        "session_id": _session_id.get(),
        "run_id": _run_id.get(),
        "tool": _tool.get(),
    }
    return {k: v for k, v in values.items() if v}


@dataclass
class _Tokens:
    """Reset tokens so :func:`bind` can restore the prior values exactly on exit."""

    corr_id: contextvars.Token
    session_id: contextvars.Token
    run_id: contextvars.Token
    tool: contextvars.Token


def _set(var: contextvars.ContextVar[str], value: str | None) -> contextvars.Token:
    return var.set(value if value is not None else var.get())


@contextmanager
def bind(
    *,
    corr_id: str | None = None,
    session_id: str | None = None,
    run_id: str | None = None,
    tool: str | None = None,
) -> Iterator[None]:
    """Bind correlation fields for the duration of the ``with`` block, then restore.

    Only the fields passed are changed; the rest keep their current value (so binding a
    ``tool`` inside a turn doesn't clear the turn's corr_id). Nesting is safe — exiting an
    inner block restores exactly the outer block's values, by token.
    """
    tokens = _Tokens(
        corr_id=_set(_corr_id, corr_id),
        session_id=_set(_session_id, session_id),
        run_id=_set(_run_id, run_id),
        tool=_set(_tool, tool),
    )
    try:
        yield
    finally:
        _corr_id.reset(tokens.corr_id)
        _session_id.reset(tokens.session_id)
        _run_id.reset(tokens.run_id)
        _tool.reset(tokens.tool)
