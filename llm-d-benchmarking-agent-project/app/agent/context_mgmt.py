"""Conversation context management — keep the replayed transcript from growing without bound.

A single `run_turn` replays the WHOLE conversation to the LLM on every step, so a long
benchmarking session (probe -> docs -> plan -> standup -> smoketest -> run -> report) replays
many large tool-result blobs forever, even after they are stale. This module compacts OLD,
SUPERSEDED tool results in place: it replaces the big JSON content of a tool result with a
short reference stub once it is far enough behind the live edge of the conversation.

This is MECHANISM only — a deterministic transform driven by char budgets + a recency window,
not agent judgment. It has two hard correctness constraints:

  * Tool-call / tool-result PAIRING is never broken. We only shrink the ``content`` STRING of
    a ``tool_results`` entry; we never drop the entry, its ``tool_call_id``, or its matching
    assistant ``tool_calls`` block. The message shape is unchanged.
  * Nothing the agent still needs MID-TASK is dropped. Only tool results OLDER than a recency
    window of recent turns are eligible, and only when the transcript is over a size threshold.
    A stub tells the model the content was elided and to re-run the tool if it needs it again.

Idempotent: an already-stubbed result is left alone, so re-compacting a transcript is a no-op
and the running session total of bytes only shrinks.
"""
from __future__ import annotations

from typing import Any

# A tool-result ``content`` longer than this many chars is a candidate for elision once it is
# behind the recency window. Smaller results (a short verdict, an endpoint count) are cheap to
# keep verbatim and are left untouched regardless of age.
_ELIDE_OVER_CHARS = 800

# Keep the most-recent N messages verbatim (the live working set the agent is mid-task on).
# Only tool results in messages BEFORE this trailing window are eligible for elision.
_RECENT_MESSAGES_KEPT = 12

# Only compact at all once the transcript's total replayed content exceeds this many chars
# (~ tens of thousands of tokens). Below it, replay is cheap and we keep everything verbatim.
_COMPACT_THRESHOLD_CHARS = 48_000

# Marker prefix on an elided stub, so compaction is IDEMPOTENT (a stub is never re-elided) and
# tests/inspection can recognise a compacted result.
_ELIDED_PREFIX = "[elided to save context]"


def _stub(name: str, original_len: int) -> str:
    """The short reference that replaces a superseded tool result's content."""
    return (f"{_ELIDED_PREFIX} the earlier {name} result ({original_len} chars) was removed "
            "from the replayed history to save context. Re-run the tool if you need it again.")


def _is_elided(content: str) -> bool:
    return content.startswith(_ELIDED_PREFIX)


def _transcript_chars(messages: list[dict[str, Any]]) -> int:
    """Approximate total replayed content size — the sum of user/assistant text plus every
    tool-result content string. Used only to decide WHETHER to compact (a cheap heuristic, not
    a token count)."""
    total = 0
    for m in messages:
        role = m.get("role")
        if role == "tool_results":
            for r in m.get("results", []):
                total += len(r.get("content") or "")
        else:
            content = m.get("content")
            if isinstance(content, str):
                total += len(content)
    return total


def compact_messages(messages: list[dict[str, Any]]) -> int:
    """Compact OLD, large, superseded tool results in ``messages`` IN PLACE.

    Returns the number of chars reclaimed (0 when nothing was compacted). Safe to call every
    turn: it is a no-op below the size threshold and idempotent above it.

    Correctness: only the ``content`` string of ``tool_results`` entries OLDER than the recent
    window is touched, and only when it is large and not already a stub — so tool-call/result
    pairing and recent context are always preserved.
    """
    if _transcript_chars(messages) <= _COMPACT_THRESHOLD_CHARS:
        return 0
    # Messages from this index onward are the recent window — kept verbatim.
    keep_from = max(0, len(messages) - _RECENT_MESSAGES_KEPT)
    reclaimed = 0
    for m in messages[:keep_from]:
        if m.get("role") != "tool_results":
            continue
        for r in m.get("results", []):
            content = r.get("content") or ""
            if len(content) <= _ELIDE_OVER_CHARS or _is_elided(content):
                continue
            stub = _stub(r.get("name") or "tool", len(content))
            reclaimed += len(content) - len(stub)
            r["content"] = stub
    return reclaimed
