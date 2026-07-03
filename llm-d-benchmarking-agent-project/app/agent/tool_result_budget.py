"""Bound a tool result to a char budget for feed-back to the model — without ever handing the
model malformed JSON.

The agent loop appends each tool result to the conversation as the ``content`` string of a
``tool_result`` block. Results can be large (long command output, a full analysis card), so we
cap them. A naive ``json.dumps(result)[:budget]`` slices mid-structure and feeds the model
invalid JSON it must then parse. Instead, when a result overflows we emit a *valid* JSON
truncation envelope that (a) preserves the small top-level signal fields — ``error`` /
``rejected`` / ``quota_exceeded`` and other status flags — verbatim, (b) records the original
size, and (c) carries a clipped preview of the full payload with an explicit note so the model
knows it is seeing only the leading portion and should narrow its query.
"""
from __future__ import annotations

import json
from typing import Any

# The char budget the agent loop applies to every tool result before feeding it back to the
# model (see loop.py). Exported here — where the clamp itself lives — so tools that want to
# pre-empt the clamp with a smarter, self-shaped truncation (e.g. read_knowledge listing the
# section headings it dropped) can size against the SAME budget without importing the loop.
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
