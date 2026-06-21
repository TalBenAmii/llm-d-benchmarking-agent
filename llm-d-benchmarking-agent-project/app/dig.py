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

Deliberately stdlib-only and imports nothing from ``app`` so every layer (validation,
storage, packaging) can use it without risking a circular import.
"""

from __future__ import annotations

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
