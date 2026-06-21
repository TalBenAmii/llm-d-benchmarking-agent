"""Pure response-shaping for the HTTP routes in ``app.main``.

These turn a storage record into the plain JSON-serializable dict a route returns. No
``app``/``app.state`` and no ``get_settings()`` — the route resolves the store and passes the
record in; this just shapes it.
"""
from __future__ import annotations

from typing import Any


def history_record_view(rec) -> dict[str, Any]:
    """The results-browser summary view of one stored history record (no heavy body)."""
    return {
        "id": rec.id, "stored_at": rec.stored_at, "label": rec.label, "tags": rec.tags,
        "model": rec.model, "run_uid": rec.run_uid, "spec": rec.spec,
        "harness": rec.harness, "workload": rec.workload, "namespace": rec.namespace,
        # Reproducibility: when this record has a provenance bundle, surface its id (+ the
        # owning session id) so the sidebar can offer Reproduce / Export report-card.
        "bundle_id": getattr(rec, "bundle_id", None),
        "session_id": getattr(rec, "session_id", None),
    }
