"""Redaction for the read-only public chat-share snapshot.

A share is an IMMUTABLE, UNAUTHENTICATED snapshot of a chat's transcript. Before freezing it,
the route strips server-internal absolute filesystem paths from card tool-results so the public
link never discloses the host path layout or the owning session id those paths embed. This is a
pure list transform (no ``app``/``app.state``); the route in ``app.main`` calls it before handing
the items to the ``ShareStore``.
"""
from __future__ import annotations

from typing import Any

# Fields on a card tool's ``result`` that carry server-internal absolute filesystem paths — the
# located report's path (``<sessions_root>/<session_id>/.../benchmark_report*.json``), and the
# directories a not-found probe searched. They drive nothing in the read-only viewer (the client
# renders ``summary``/``charts`` only; charts are already session-relative), yet a public share is
# UNAUTHENTICATED, so shipping them would disclose the host path layout AND the owning session id —
# the very id the snapshot deliberately withholds (see read_share / shared_chat._PUBLIC_FIELDS).
_SHARE_REDACT_RESULT_KEYS = ("report_path", "searched")


def redact_share_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip server-internal absolute paths from a PUBLIC share snapshot's tool_result rows.

    The transcript replayed to the owner on resume legitimately carries the located report's
    absolute path; a public share must not. Returns NEW item dicts (the live session is never
    mutated) with the path-bearing keys removed from any ``tool_result`` result, leaving every
    render-relevant field (summary, charts, metrics) intact."""
    out: list[dict[str, Any]] = []
    for it in items:
        result = it.get("result") if it.get("role") == "tool_result" else None
        if isinstance(result, dict) and any(k in result for k in _SHARE_REDACT_RESULT_KEYS):
            scrubbed = {k: v for k, v in result.items() if k not in _SHARE_REDACT_RESULT_KEYS}
            out.append({**it, "result": scrubbed})
        else:
            out.append(it)
    return out
