"""Safe traversal of nested, untrusted dict structures (one shared helper).

A benchmark report / summary / provenance bundle is a deeply-nested mapping read from
disk or a harness, so any child along a path may be missing or a non-dict. Both the
validation/analysis math and the report-card / history layers need the same defensive
"walk a path, return None the moment it leaves dict-land" traversal — previously each
re-implemented it as a private ``_dig`` (four near-identical copies). This is the single
mechanism: no judgment, never fabricates, never raises on a malformed shape.

Two entry points over one traversal:
- ``dig(obj, *parts)``  — segment form: ``dig(s, "latency", "ttft")``
- ``dig_dotted(obj, "latency.ttft")`` — dotted-string form (splits on ``.``)

Two more single-level coercers (not traversals) live here as siblings:
- ``dict_or_empty(v)`` — ``v`` if it is a mapping, else ``{}`` — so a caller can ``.get``
  the children of an untrusted, on-disk value without re-asserting the type at every lookup.
  Several layers had each copied this one-liner as a private ``_d``/``_dict``; this is the
  single mechanism. Named ``dict_or_empty`` (not ``as_dict``) so it never reads like the
  dataclass ``.as_dict()`` SERIALIZER methods elsewhere in app/.
- ``num_or_zero(v)`` — ``v`` if it is a real number (``bool`` excluded), else ``0.0`` — a
  crash-proof sort key for records reconstructed from on-disk JSON, so a non-numeric field
  can't make ``sorted(...)`` raise ``TypeError`` and break listing/trending. Several storage/
  agent layers had each copied this one-liner as a private ``_as_num``; this is the single
  mechanism.

Deliberately stdlib-only and imports nothing from ``app`` so every layer (validation,
storage, packaging) can use it without risking a circular import.
"""

from __future__ import annotations

import json
import os
from typing import Any


def dig(obj: Any, *parts: str) -> Any:
    """Walk ``parts`` into a nested mapping, returning ``None`` if the path leaves dict-land.

    At each step the current value must be a ``dict`` or traversal stops with ``None`` —
    so a missing key, or a key whose value is a scalar/list/None, never raises.
    """
    cur: Any = obj
    for part in parts:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def dig_dotted(obj: Any, dotted: str) -> Any:
    """``dig`` for a single dotted path, e.g. ``dig_dotted(summary, "latency.ttft")``."""
    return dig(obj, *dotted.split("."))


def dict_or_empty(value: Any) -> dict[str, Any]:
    """``value`` if it is a mapping, else an empty ``dict``.

    Lets callers read a possibly-missing-or-oddly-shaped child of an untrusted, disk-loaded
    mapping with ``.get(...)`` defensively without re-asserting the type at every lookup —
    the same "never crash on a malformed shape" contract as ``dig`` (which walks a path; this
    coerces a single level). Several layers previously re-implemented this as a private
    ``_d`` / ``_dict``; this is the single mechanism. Named ``dict_or_empty`` (not ``as_dict``)
    so it never reads like the dataclass ``.as_dict()`` SERIALIZER methods elsewhere in app/.
    """
    return value if isinstance(value, dict) else {}


def num_or_zero(value: Any) -> float:
    """``value`` if it is a real number, else ``0.0`` — a crash-proof sort key.

    Records reconstructed from on-disk JSON carry NO per-field type-check, so a corrupt /
    hand-edited / forward-incompatible record with a non-numeric field (a null or a truthy
    string) would otherwise make ``sorted(...)`` raise ``TypeError`` and break listing/trending
    for EVERY record, not just the corrupt one. Coercing keeps the record visible (sorted as
    oldest). ``bool`` is excluded — an ``int`` subclass, never a valid timestamp/index.
    """
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def scrub_strings(obj: Any, replacements: list[tuple[str, str]]) -> Any:
    """Rewrite every string leaf of a nested dict/list/str structure by an ORDERED list of
    ``(find, replace)`` substitutions, returning a NEW structure (the input is never mutated).

    Both the ``/readyz`` self-check body and the public-share snapshot must mask host-internal
    substrings (home dir, workspace root, owning session id) in EVERY string leaf of a nested
    result before it crosses an unauthenticated boundary — this is the single mechanism for that
    walk (each layer previously re-implemented it). Substitutions apply in order to each leaf, so a
    caller can mask a workspace prefix before the ``home`` prefix it sits under. A ``find`` that is
    empty OR ``os.sep`` (``"/"``) is SKIPPED: a degenerate root (``HOME="/"``, an empty workspace)
    would otherwise rewrite the separator in every path leaf into garbage.
    """
    if isinstance(obj, str):
        for find, replace in replacements:
            if find and find != os.sep:
                obj = obj.replace(find, replace)
        return obj
    if isinstance(obj, dict):
        return {k: scrub_strings(v, replacements) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub_strings(v, replacements) for v in obj]
    return obj


# ── tolerant JSON-tail parsing (merged from app/tools/json_tail.py) ───────────
# Tolerant JSON-tail parsing.
#
# A bridge/tool subprocess is expected to print exactly one JSON value on stdout, but may emit
# leading log noise. ``find_last_json`` pulls that single value off the stream. Shared by the
# capacity, aggregation, and stack-discovery bridges (each wraps it with its own
# type-check + missing-output error policy).

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


def parse_bridge_dict(output: str, label: str) -> dict[str, Any]:
    """Parse the single JSON OBJECT a bridge subprocess prints on stdout into a result dict.

    The capacity and aggregation bridges share this contract: print exactly one JSON object,
    possibly after some leading log noise. This wrapper applies the shared, never-raise error
    policy used by both tools — on empty output or no parseable trailing object it returns a
    ``{"ok": False, "error": ...}`` dict (rather than raising) so the calling tool can fold the
    failure into its own structured result. ``label`` names the bridge in the error text
    (e.g. ``"capacity"`` -> ``"capacity bridge produced no output"``).

    Note: a non-object JSON tail (e.g. a bare list) is treated as "not JSON" here — the
    contract is a single object, and that is the safer behavior for a result expected to carry
    ``ok``/``error`` keys. (Use ``find_last_json`` directly when a list is the expected shape.)
    """
    text = (output or "").strip()
    if not text:
        return {"ok": False, "error": f"{label} bridge produced no output"}
    result = find_last_json(text, "{")
    if isinstance(result, dict):
        return result
    return {"ok": False, "error": f"{label} bridge output was not JSON: {text[-500:]}"}
