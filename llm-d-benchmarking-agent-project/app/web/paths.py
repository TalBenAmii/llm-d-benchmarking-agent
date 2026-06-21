"""Path-traversal hardening for the read-only artifact + provenance-bundle serving routes.

These helpers are PURE: each takes the already-resolved ``sessions_root`` (the route resolves
it via ``get_settings()`` so a test monkeypatching ``app.main.get_settings`` still steers which
dir is served) and raises ``HTTPException`` with the EXACT status/detail the routes are tested
on. They register nothing on the FastAPI ``app`` — the decorated routes in ``app.main`` are thin
callers that wrap the returned ``Path``/``dict`` in a ``FileResponse``/``JSONResponse``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import HTTPException

from app.storage.provenance import BundleStore

# Per-run chart images (e.g. the latency/throughput PNGs inference-perf renders into a
# session's analysis/ dir) live under the gitignored workspace, which the /static mount does
# NOT serve. The artifact route exposes them so the UI can show a run's charts inline next to
# its summary. Hardened: image suffixes only, and the resolved path must stay INSIDE the named
# session dir (defeats ../ traversal). The chart paths come from locate_and_parse_report's
# `charts` field.
_ARTIFACT_SUFFIXES = frozenset({".png", ".svg", ".jpg", ".jpeg", ".webp"})
_ARTIFACT_MEDIA = {
    ".png": "image/png", ".svg": "image/svg+xml", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".webp": "image/webp",
}


def resolve_artifact(sessions_root: Path, sid: str, path: str) -> tuple[Path, str]:
    """Resolve one image artifact under ``<sessions_root>/<sid>/`` (read-only, image-only).

    Returns ``(candidate, media_type)`` for a valid request; raises ``HTTPException(404,
    "artifact not found")`` for anything else. ``sessions_root`` must already be ``.resolve()``-d
    by the caller. Rejects ../ traversal in either ``sid`` or ``path``, non-image suffixes,
    non-files, and degrades over-long components / embedded NUL bytes to a clean 404 (never 500)."""
    try:
        base = (sessions_root / sid).resolve()
        candidate = (base / path).resolve()
        # `base` must be a real session dir directly under sessions_root, and `candidate` must not
        # escape it — together these reject ../ traversal in either `sid` or `path`.
        if base.parent != sessions_root or not base.is_dir() or not candidate.is_relative_to(base):
            raise HTTPException(status_code=404, detail="artifact not found")
        suffix = candidate.suffix.lower()
        if suffix not in _ARTIFACT_SUFFIXES or not candidate.is_file():
            raise HTTPException(status_code=404, detail="artifact not found")
    except (OSError, ValueError):
        # An over-long `sid`/`path` component (ENAMETOOLONG → OSError) or an embedded NUL byte
        # (`%00` → ValueError "embedded null byte") during resolution must read as a clean 404 —
        # never a 500. (HTTPException is neither, so the explicit 404s above propagate untouched.)
        raise HTTPException(status_code=404, detail="artifact not found") from None
    return candidate, _ARTIFACT_MEDIA[suffix]


def resolve_bundle(sessions_root: Path, sid: str, bundle_id: str) -> dict[str, Any]:
    """Locate one provenance bundle JSON under a session's ``bundles/`` dir, reusing the SAME
    path-traversal hardening as ``resolve_artifact`` (``base.parent == sessions_root``,
    ``is_relative_to``) PLUS the BundleStore's own ``_safe_id`` guard on the bundle id. A 404 for
    a bad ``sid`` / ``bundle_id`` / missing bundle (never an info leak). ``sessions_root`` must
    already be ``.resolve()``-d by the caller."""
    try:
        base = (sessions_root / sid).resolve()
        if base.parent != sessions_root or not base.is_dir():
            raise HTTPException(status_code=404, detail="bundle not found")
    except (OSError, ValueError):
        # Over-long `sid` (ENAMETOOLONG → OSError) or an embedded NUL byte (`%00` → ValueError) →
        # clean 404, never a 500.
        raise HTTPException(status_code=404, detail="bundle not found") from None
    bundle = BundleStore(base).read(bundle_id)  # _safe_id inside rejects ../ and a/b ids
    if bundle is None:
        raise HTTPException(status_code=404, detail="bundle not found")
    # Defense in depth: the resolved file must stay inside the session's bundles dir.
    candidate = (base / "bundles" / f"{bundle_id}.json").resolve()
    if not candidate.is_relative_to(base):
        raise HTTPException(status_code=404, detail="bundle not found")
    return bundle
