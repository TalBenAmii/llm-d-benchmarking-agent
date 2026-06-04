"""Tolerant JSON-tail parsing.

A bridge/tool subprocess is expected to print exactly one JSON value on stdout, but may emit
leading log noise. ``find_last_json`` pulls that single value off the stream. Shared by the
capacity, aggregation, and stack-discovery bridges (each wraps it with its own
type-check + missing-output error policy).
"""

import json
from typing import Any

_DECODER = json.JSONDecoder()


def find_last_json(text: str, opener: str) -> Any | None:
    """Return the last balanced JSON value beginning with ``opener`` (``"{"`` or ``"["``) on a
    possibly-noisy stream, or ``None`` if none parses.

    Tries the whole (stripped) stream first, then scans backward from each ``opener`` for the
    last substring that is exactly one JSON value (modulo trailing whitespace).

    Each candidate is decoded with ``raw_decode`` — which parses a single value and stops at its
    end — so a non-matching ``opener`` (e.g. a brace nested inside the real payload, or a
    JSON-like fragment in a log line) costs only that fragment's size, not a re-parse of the
    whole suffix. That keeps a large blob preceded by log noise from degrading to O(braces ×
    length). Result is identical to attempting ``json.loads(text[start:])`` at each opener.
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
            obj, end = _DECODER.raw_decode(text, start)
        except ValueError:
            start = text.rfind(opener, 0, start)
            continue
        # Accept only when the candidate spans to the end of the stream (nothing but
        # whitespace trails it) — i.e. text[start:] is a single value, as json.loads requires.
        if not text[end:].strip():
            return obj
        start = text.rfind(opener, 0, start)
    return None
