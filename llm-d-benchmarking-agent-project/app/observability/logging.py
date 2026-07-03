"""Structured logging setup (Phase 11): stdlib ``logging`` configured with a hand-rolled
JSON formatter (one JSON object per line) and a context filter that stamps the per-turn
correlation ids onto every record.

NO new runtime dependency: the JSON formatter is implemented here over stdlib only (no
structlog / python-json-logger). Pure mechanism — *what* to log is decided at the call
sites (the loop, tools, the runner); this module only decides *how* a record is rendered
and that it carries the correlation context.

Call :func:`setup_logging` once at startup (the FastAPI lifespan). The
:class:`ContextFilter` reads the per-turn correlation context (defined in the section
below), so a corr_id bound at the WS handshake propagates to records emitted anywhere
downstream in the same turn.

Per-turn log correlation context
--------------------------------
A single ``contextvars`` carrier holds the correlation fields that should ride along on
*every* log record emitted while handling one WebSocket turn — without threading an
argument through the agent loop, tool dispatch, and the command runner. The
:class:`ContextFilter` reads these and stamps them onto each record, so a corr_id set at
the WS handshake automatically appears on records from the loop, a tool, and the runner.

Pure mechanism: this only *carries* identifiers. It makes no decisions — what to do with a
correlated trail is the operator's / agent's judgment, never code here. ``contextvars`` is
asyncio-aware: each task sees the value bound on its own logical call stack, so concurrent
turns (parallel sessions) don't bleed correlation ids into each other.
"""
from __future__ import annotations

import contextvars
import datetime as _dt
import json
import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

# --- per-turn correlation context --------------------------------------------

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


# --- structured logging setup ------------------------------------------------

# LogRecord attributes that are NOT user "extra" fields — excluded when we fold extras in.
_RESERVED_RECORD_ATTRS = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module",
        "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created", "msecs",
        "relativeCreated", "thread", "threadName", "processName", "process", "taskName",
        "message", "asctime",
    }
    | set(LOG_CONTEXT_FIELDS)
)


class ContextFilter(logging.Filter):
    """Stamp the current correlation context (corr_id/session_id/run_id/tool) onto each
    record. A :class:`logging.Filter` is the clean injection point: it runs for every record
    just before formatting, so the formatter can read the fields straight off the record.

    Returns ``True`` always (it filters nothing out — it only enriches)."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - stdlib API name
        ctx = get_log_context()
        for field in LOG_CONTEXT_FIELDS:
            # Always set the attribute (default "") so the text formatter never KeyErrors,
            # and the JSON formatter can decide to omit empties.
            setattr(record, field, ctx.get(field, ""))
        return True


class JsonFormatter(logging.Formatter):
    """Render a LogRecord as a single-line JSON object with the standard fields, the bound
    correlation fields (omitted when unset), any structured ``extra=`` keys, and — when
    present — a formatted exception. One JSON object per line (newline-delimited JSON)."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": _dt.datetime.fromtimestamp(
                record.created, tz=_dt.UTC
            ).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Correlation fields (set by ContextFilter); include only the non-empty ones.
        for field in LOG_CONTEXT_FIELDS:
            value = getattr(record, field, "")
            if value:
                payload[field] = value
        # Structured extras passed via logger.x(..., extra={...}) — anything not a reserved
        # LogRecord attribute. Lets call sites attach exe/duration/exit_code/etc.
        for key, value in record.__dict__.items():
            if key not in _RESERVED_RECORD_ATTRS and key not in payload:
                payload[key] = _coerce(value)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)
        return json.dumps(payload, default=str)


def _coerce(value: Any) -> Any:
    """Keep JSON-native types as-is; stringify everything else so a stray object in an
    ``extra`` can never make a log line invalid JSON (``json.dumps(default=str)`` backstops)."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple, dict)):
        return value
    return str(value)


# A compact text format for dev: timestamp level logger [corr_id] message.
_TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(corr_id)s] %(message)s"


def _build_handler(log_format: str) -> logging.Handler:
    handler = logging.StreamHandler()
    handler.addFilter(ContextFilter())
    if log_format == "text":
        handler.setFormatter(logging.Formatter(_TEXT_FORMAT))
    else:
        handler.setFormatter(JsonFormatter())
    return handler


def setup_logging(*, level: str = "INFO", log_format: str = "json") -> None:
    """Configure the root logger once: clear pre-existing handlers, install a single stream
    handler with the chosen formatter + the context filter. Idempotent (safe to call again —
    e.g. across test runs / a reload): it replaces handlers rather than stacking them.

    ``log_format`` is ``"json"`` (default; one JSON object per line) or ``"text"`` (dev)."""
    root = logging.getLogger()
    root.setLevel(_normalize_level(level))
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(_build_handler("text" if log_format == "text" else "json"))


def _normalize_level(level: str) -> int:
    """Map a level name to its numeric value; default to INFO for an unknown string."""
    resolved = logging.getLevelName(str(level).upper())
    return resolved if isinstance(resolved, int) else logging.INFO
