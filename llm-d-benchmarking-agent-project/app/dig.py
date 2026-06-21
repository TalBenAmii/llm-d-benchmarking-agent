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
