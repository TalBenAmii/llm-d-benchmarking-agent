"""Tolerant JSON-tail parsing.

A bridge/tool subprocess is expected to print exactly one JSON value on stdout, but may emit
leading log noise. ``find_last_json`` pulls that single value off the stream. Shared by the
capacity, aggregation, and stack-discovery bridges (each wraps it with its own
type-check + missing-output error policy).
"""

import json
from typing import Any


def find_last_json(text: str, opener: str) -> Any | None:
    """Return the last balanced JSON value beginning with ``opener`` (``"{"`` or ``"["``) on a
    possibly-noisy stream, or ``None`` if none parses.

    Tries the whole (stripped) stream first, then scans backward from each ``opener`` for the
    last substring that parses cleanly.
    """
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    start = text.rfind(opener)
    while start != -1:
        try:
            return json.loads(text[start:])
        except ValueError:
            start = text.rfind(opener, 0, start)
    return None
