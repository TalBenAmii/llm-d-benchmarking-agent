"""Structured logging setup (Phase 11): stdlib ``logging`` configured with a hand-rolled
JSON formatter (one JSON object per line) and a context filter that stamps the per-turn
correlation ids onto every record.

NO new runtime dependency: the JSON formatter is implemented here over stdlib only (no
structlog / python-json-logger). Pure mechanism — *what* to log is decided at the call
sites (the loop, tools, the runner); this module only decides *how* a record is rendered
and that it carries the correlation context.

Call :func:`setup_logging` once at startup (the FastAPI lifespan). The
:class:`ContextFilter` reads :mod:`app.observability.logctx`, so a corr_id bound at the WS
handshake propagates to records emitted anywhere downstream in the same turn.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
from typing import Any

from app.observability.logctx import LOG_CONTEXT_FIELDS, get_log_context

# The fixed, standard keys every JSON line carries (correlation fields are added when bound).
_STANDARD_KEYS = ("timestamp", "level", "logger", "message")

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
                record.created, tz=_dt.timezone.utc
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
