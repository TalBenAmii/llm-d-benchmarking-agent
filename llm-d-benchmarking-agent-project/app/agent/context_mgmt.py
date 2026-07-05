"""Conversation context management — keep the replayed transcript from growing without bound.

A single `run_turn` replays the WHOLE conversation to the LLM on every step, so a long
benchmarking session (probe -> docs -> plan -> standup -> smoketest -> run -> report) replays
many large blobs forever, even after they are stale. This module compacts OLD, SUPERSEDED
content in place, replacing it with a short reference stub once it is far enough behind the
live edge of the conversation. Two kinds of blob are compacted:

  * OLD large ``tool_results`` content (the long command output, the full analysis card, …).
  * OLD large SYNTHETIC injected user messages — the machine-injected context the *user* never
    typed: the environment pre-probe snapshot and the once-per-session live-catalog snapshot.
    These accumulate at the head of the transcript and were previously never elided, so they
    rode along in every replay forever. Both are re-derivable on demand (probe_environment /
    list_catalog), so once stale they become a short stub telling the model how to refresh.

This is MECHANISM only — a deterministic transform driven by char budgets + a recency window,
not agent judgment. It has these hard correctness constraints:

  * Tool-call / tool-result PAIRING is never broken. We only shrink the ``content`` STRING of
    a ``tool_results`` entry; we never drop the entry, its ``tool_call_id``, or its matching
    assistant ``tool_calls`` block. The message shape is unchanged.
  * REAL user messages and ALL assistant messages are NEVER touched. Only machine-injected
    SYNTHETIC user messages (flagged ``synthetic: True`` or carrying a known injected
    bracket-tag, e.g. "[live catalog snapshot …]") are eligible — a real typed message is
    irreplaceable, a synthetic snapshot is regenerable.
  * Nothing the agent still needs MID-TASK is dropped. Only content OLDER than a recency
    window of recent turns is eligible, and only when the transcript is over a size threshold.
    A stub tells the model the content was elided and how to re-fetch it if it needs it again.

Idempotent: an already-stubbed result/message is left alone, so re-compacting a transcript is
a no-op and the running session total of bytes only shrinks.
"""
from __future__ import annotations

import json
from typing import Any

# A tool-result ``content`` longer than this many chars is a candidate for elision once it is
# behind the recency window. Smaller results (a short verdict, an endpoint count) are cheap to
# keep verbatim and are left untouched regardless of age. Kept low so a few-hundred-char stale
# blob (a probe dump, a short report) is also reclaimed once old.
_ELIDE_OVER_CHARS = 600

# Keep the most-recent N messages verbatim (the live working set the agent is mid-task on).
# Only tool results in messages BEFORE this trailing window are eligible for elision. A
# tool-calling step appends two messages (assistant + tool_results), so 8 ≈ the last ~4 steps
# kept in full — the agent almost always only needs the last 1-3 results to pick its next move,
# and anything older is re-fetchable (the stub says how), so a smaller window trims the replayed
# tail (and its per-step cache-read) without starving the live working set.
_RECENT_MESSAGES_KEPT = 8

# Only compact at all once the transcript's total replayed content exceeds this many chars
# (~ a few thousand tokens). Below it, replay is cheap and we keep everything verbatim.
# A long deploy→smoketest→run→report session replays the whole transcript on EVERY step, so
# compacting at a modest threshold keeps the per-step replay (and the recurring cache-read of it)
# materially smaller without touching the recent working window.
_COMPACT_THRESHOLD_CHARS = 20_000

# Marker prefix on an elided stub, so compaction is IDEMPOTENT (a stub is never re-elided) and
# tests/inspection can recognise a compacted result.
_ELIDED_PREFIX = "[elided to save context]"

# Known bracket-tag prefixes that identify a machine-INJECTED synthetic user message (context the
# human never typed). The env pre-probe snapshot ALSO carries ``synthetic: True``; the live
# catalog snapshot does NOT (it is injected once per session as a plain user message — see
# app/agent/loop.py + catalog_brief_message), so we recognise it by its byte-stable tag. Each
# maps to a short, honest stub telling the model how to re-derive the elided context on demand.
_INJECTED_TAGS: tuple[tuple[str, str], ...] = (
    ("[environment pre-probe",
     f"{_ELIDED_PREFIX} an environment snapshot gathered for you on an earlier turn was removed "
     "from the replayed history to save context — call probe_environment again if you need the "
     "current cluster/tooling state."),
    ("[live catalog snapshot",
     f"{_ELIDED_PREFIX} the live catalog snapshot from an earlier turn was removed from the "
     "replayed history to save context — call list_catalog again if you need the current list of "
     "valid specs/harnesses/workloads."),
)


def _stub(name: str, original_len: int) -> str:
    """The short reference that replaces a superseded tool result's content."""
    return (f"{_ELIDED_PREFIX} the earlier {name} result ({original_len} chars) was removed "
            "from the replayed history to save context. Re-run the tool if you need it again.")


def _injected_stub(content: str) -> str | None:
    """If ``content`` is a recognised machine-injected synthetic user message, the short stub
    that should replace it; else None. Bracket-tag match is byte-stable (the tags are fixed
    literals emitted by the loop), so this is deterministic and safe."""
    for tag, stub in _INJECTED_TAGS:
        if content.startswith(tag):
            return stub
    return None


def _is_synthetic_user(m: dict[str, Any]) -> bool:
    """True for a machine-injected synthetic user message — flagged ``synthetic: True`` OR
    carrying a known injected bracket-tag. NEVER true for a real typed user message (no flag,
    and a real message that happened to start with '[' is not one of the fixed tags) or for any
    assistant / tool_results message."""
    if m.get("role") != "user":
        return False
    if m.get("synthetic"):
        return True
    content = m.get("content")
    return isinstance(content, str) and _injected_stub(content) is not None


def _synthetic_stub_for(m: dict[str, Any]) -> str:
    """The stub to replace a synthetic user message's content with. Prefers the tag-specific
    stub; falls back to a generic one for a ``synthetic``-flagged message with no known tag."""
    content = m.get("content")
    if isinstance(content, str):
        stub = _injected_stub(content)
        if stub is not None:
            return stub
    return (f"{_ELIDED_PREFIX} machine-injected context from an earlier turn was removed from "
            "the replayed history to save context — re-run the relevant read-only tool "
            "(probe_environment / list_catalog) if you need it again.")


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


# ~4 chars per token is the standard cheap estimate (no tokenizer, no network). Labelled an
# ESTIMATE everywhere it surfaces.
_CHARS_PER_TOKEN = 4


def _last_tool_result_chars(messages: list[dict[str, Any]]) -> int:
    """Size (chars) of the most recent ``tool_results`` message — the single blob most likely to
    dominate a turn's growth (a fat command output / report). 0 if there is none yet."""
    for m in reversed(messages):
        if m.get("role") == "tool_results":
            return sum(len(r.get("content") or "") for r in m.get("results", []))
    return 0


def estimate_context_size(system: str, messages: list[dict[str, Any]]) -> dict[str, int]:
    """A cheap ESTIMATE of the CURRENT assembled-context window size + a small breakdown, so the
    user can SEE context growth and what dominates it. char/4 token estimate (NOT a tokenizer);
    every field is an estimate, labelled as such by the consumer. Mechanism only — zero cost.

    Breakdown:
      * ``system`` — the (cached) system prompt prefix,
      * ``history`` — everything replayed in ``messages`` (synthetic + real user + assistant +
        tool results),
      * ``last_tool_result`` — the most recent tool-result blob (the usual per-turn spike).
    """
    system_chars = len(system)
    history_chars = _transcript_chars(messages)
    last_tr = _last_tool_result_chars(messages)
    total_chars = system_chars + history_chars
    return {
        "total_chars": total_chars,
        "total_tokens_est": total_chars // _CHARS_PER_TOKEN,
        "system_chars": system_chars,
        "system_tokens_est": system_chars // _CHARS_PER_TOKEN,
        "history_chars": history_chars,
        "history_tokens_est": history_chars // _CHARS_PER_TOKEN,
        "last_tool_result_chars": last_tr,
        "last_tool_result_tokens_est": last_tr // _CHARS_PER_TOKEN,
    }


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
        role = m.get("role")
        if role == "tool_results":
            for r in m.get("results", []):
                content = r.get("content") or ""
                if len(content) <= _ELIDE_OVER_CHARS or _is_elided(content):
                    continue
                stub = _stub(r.get("name") or "tool", len(content))
                reclaimed += len(content) - len(stub)
                r["content"] = stub
        elif _is_synthetic_user(m):
            # OLD machine-injected synthetic user context (env pre-probe snapshot / live catalog
            # snapshot). Re-derivable on demand, so once stale it becomes a short stub. Real typed
            # user messages and assistant messages are never reached here (_is_synthetic_user is
            # False for them). Keep the ``synthetic`` flag intact so title/history rendering still
            # skips it.
            content = m.get("content") or ""
            if len(content) <= _ELIDE_OVER_CHARS or _is_elided(content):
                continue
            stub = _synthetic_stub_for(m)
            reclaimed += len(content) - len(stub)
            m["content"] = stub
    return reclaimed


# ── tool-result feed-back budget ─────────────────────────────────────────────
# Bound a tool result to a char budget for feed-back to the model — without ever handing the
# model malformed JSON.
#
# The agent loop appends each tool result to the conversation as the ``content`` string of a
# ``tool_result`` block. Results can be large (long command output, a full analysis card), so we
# cap them. A naive ``json.dumps(result)[:budget]`` slices mid-structure and feeds the model
# invalid JSON it must then parse. Instead, when a result overflows we emit a *valid* JSON
# truncation envelope that (a) preserves the small top-level signal fields — ``error`` /
# ``rejected`` / ``quota_exceeded`` and other status flags — verbatim, (b) records the original
# size, and (c) carries a clipped preview of the full payload with an explicit note so the model
# knows it is seeing only the leading portion and should narrow its query.

# The char budget the agent loop applies to every tool result before feeding it back to the
# model (loop.py applies ``DEFAULT_TOOL_RESULT_BUDGET`` directly). Exported here — where the clamp itself
# lives — so tools that want to pre-empt the clamp with a smarter, self-shaped truncation (e.g.
# read_knowledge listing the section headings it dropped) can size against the SAME budget
# without importing the loop.
DEFAULT_TOOL_RESULT_BUDGET = 6_000

_TRUNC_NOTE = (
    "tool result exceeded the feed-back budget and was truncated; the 'preview' field holds "
    "its leading portion. Re-run with a narrower query or request specific fields for the rest."
)

# A short scalar top-level field is kept verbatim in the envelope (it carries the error/status
# signal); anything longer is treated as bulk payload and appears only (clipped) in the preview.
_SIGNAL_STR_MAX = 500


def _is_signal_scalar(value: Any) -> bool:
    """True for small scalars worth preserving intact (bools, numbers, short strings, None)."""
    if value is None or isinstance(value, (bool, int, float)):
        return True
    return isinstance(value, str) and len(value) <= _SIGNAL_STR_MAX


def clamp_tool_result_content(result: Any, budget: int) -> str:
    """Serialize ``result`` to JSON of at most ``budget`` chars that is always valid JSON.

    Fast path: when the full serialization fits, it is returned unchanged (byte-identical to
    ``json.dumps(result)``). Otherwise a valid JSON truncation envelope is returned, sized to
    the budget, preserving small top-level signal fields and a clipped preview of the payload.
    """
    full = json.dumps(result)
    if len(full) <= budget:
        return full

    envelope: dict[str, Any] = {"_truncated": True, "_original_chars": len(full)}
    # Preserve small top-level signal fields so error / rejected / quota markers survive intact.
    if isinstance(result, dict):
        for key, value in result.items():
            if isinstance(key, str) and key not in envelope and _is_signal_scalar(value):
                envelope[key] = value
    envelope["_note"] = _TRUNC_NOTE

    # Budget left for the preview after the envelope's own JSON overhead (keys, braces, the
    # "preview" key and its quoting). Reserve it by measuring the envelope with an empty preview.
    skeleton = dict(envelope)
    skeleton["preview"] = ""
    remaining = budget - len(json.dumps(skeleton))
    if remaining <= 0:
        # Even the signal-only envelope overflows the budget; fall back to the minimal one.
        minimal = {"_truncated": True, "_original_chars": len(full), "_note": _TRUNC_NOTE}
        return json.dumps(minimal)

    # JSON-escaping can expand the preview (a " becomes \", a newline becomes \n), so clip the
    # raw source first, then shrink until the *encoded* envelope fits the budget.
    preview = full[:remaining]
    while preview:
        envelope["preview"] = preview
        encoded = json.dumps(envelope)
        if len(encoded) <= budget:
            return encoded
        preview = preview[: -max(1, len(encoded) - budget)]

    envelope["preview"] = ""
    return json.dumps(envelope)
