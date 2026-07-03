"""Per-session chain-of-thought debug trace.

When the agent makes a mistake, the most useful artifact for debugging *why* is the model's
own reasoning alongside the decisions it drove. This module appends a newline-delimited JSON
record per event to ``<workspace>/sessions/<id>/cot_trace.jsonl`` — the session's OWN folder,
right next to its ``state.json`` — so each chat carries a readable, greppable record of:

* ``turn_start``  — the user message that kicked off the turn,
* ``step``        — one LLM call: its extended-thinking (chain-of-thought), assistant text,
                    the tool calls it decided to make, and per-call token usage,
* ``tool_result`` — what each tool returned (bounded),
* ``turn_end``    — how many tool calls the turn made.

Pure mechanism: it RECORDS, it never decides. Best-effort and defensive — a write failure is
logged at debug level and swallowed so tracing can never break a live turn. Disabled instances
(``TurnTrace.disabled()``) are cheap no-ops, so the loop can hold one unconditionally.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
from pathlib import Path
from typing import Any

from app.observability.logging import get_corr_id

log = logging.getLogger("app.observability.cot_trace")

# The per-session trace file name, written inside the session's workspace dir.
TRACE_FILENAME = "cot_trace.jsonl"

# Bound the thinking / text / tool-result bodies written per record so a runaway turn can't grow
# the trace without limit. Generous — the point is debugging, not minimal disk — but finite.
_BODY_LIMIT = 200_000


def _clip(value: Any, limit: int = _BODY_LIMIT) -> Any:
    """Truncate every oversized string body with a visible marker; small values pass through.

    Recurses into the list/dict CONTAINERS the loop hands us (notably a step's ``tool_calls``,
    whose ``input`` is a model-emitted dict that can nest a large body like ``write_config``'s
    ``content``). Bounding only a top-level string would let such a nested blob grow one record
    without limit — defeating the per-record cap. Recursion is depth-bounded so a degenerate
    (or cyclic) structure can never blow the stack while tracing a live turn.
    """
    return _clip_at(value, limit, depth=8)


def _clip_at(value: Any, limit: int, depth: int) -> Any:
    if isinstance(value, str):
        if len(value) > limit:
            return value[:limit] + f"\n…[truncated {len(value) - limit} chars]"
        return value
    if depth <= 0:
        return value
    if isinstance(value, dict):
        return {k: _clip_at(v, limit, depth - 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clip_at(v, limit, depth - 1) for v in value]
    return value


class TurnTrace:
    """A per-turn handle that appends events to one session's trace file.

    Construct via :meth:`for_session` (enabled) or :meth:`disabled` (no-op). ``event`` is the
    single entry point; it never raises into the caller.
    """

    def __init__(self, path: Path | None):
        self._path = path

    @classmethod
    def for_session(cls, workspace: Path) -> TurnTrace:
        """A tracer writing to ``<workspace>/cot_trace.jsonl`` (the session's own folder)."""
        return cls(Path(workspace) / TRACE_FILENAME)

    @classmethod
    def disabled(cls) -> TurnTrace:
        """A no-op tracer (every :meth:`event` returns immediately)."""
        return cls(None)

    @property
    def enabled(self) -> bool:
        return self._path is not None

    def event(self, kind: str, **data: Any) -> None:
        """Append one JSON line: ``{ts, corr_id, kind, **data}``. Best-effort; never raises."""
        if self._path is None:
            return
        record: dict[str, Any] = {
            "ts": _dt.datetime.now(tz=_dt.UTC).isoformat(timespec="milliseconds"),
            "corr_id": get_corr_id() or None,
            "kind": kind,
        }
        record.update({k: _clip(v) for k, v in data.items()})
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except OSError as exc:  # disk full / permissions — tracing must never break a turn
            log.debug("cot_trace.write_failed", extra={"error": str(exc)})
