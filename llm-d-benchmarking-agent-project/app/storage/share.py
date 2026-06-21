"""Shareable, read-only conversation snapshots — the "share a chat via link" feature.

A *share* is an IMMUTABLE snapshot of a chat's rendered transcript, taken at the moment the
user clicks Share and written to ``<workspace>/shares/<token>.json``. The ``token`` (a uuid4
hex string — 128 bits, unguessable) is the ONLY credential needed to view it, so the public
viewer route can be reached without the app's optional Bearer auth — that is the whole point
of a public link (the token *is* the bearer secret).

Why a snapshot rather than a live reference to the session:

* **Stability** — continuing the chat (or deleting it) never changes or breaks the shared copy;
  the recipient sees exactly what existed when the link was created (ChatGPT semantics:
  "messages you send after creating the link won't be shared").
* **Safety** — the snapshot carries only the render-friendly transcript items the owner already
  sees, never the live session object. Still-pending approval gates (live, clickable state) are
  filtered out by the caller before snapshotting, so a chat parked at an approval never leaks a
  live gate into a public page; the owning session id is kept server-side and never returned to
  the public viewer.

This module is pure **mechanism**, mirroring :class:`~app.storage.history.HistoryStore` and
:class:`~app.storage.provenance.BundleStore`: token minting, a filesystem-safe id guard, and
write / read / delete over per-token JSON files. No judgment, no LLM. ``shares/`` lives under the
same workspace the session + history stores use and is GC-eligible exactly like them: it is a
managed retention area (see ``app.storage.retention.MANAGED_AREAS``), and the GC prunes a share's
snapshot together with its optional ``<token>.gist`` mapping so a pruned snapshot never leaves a
dangling mapping behind.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

# A share token is a uuid4 hex string. We build a filesystem path from it AND it arrives from
# the browser (the ``/share/<token>`` path + the public JSON route), so validate the SHAPE
# before touching disk — exactly like SessionManager._is_valid_id guards a session id. The
# 32-hex-char form can never contain a path separator or ``..``, so traversal is impossible.
_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")


def _is_valid_token(token: str | None) -> bool:
    return isinstance(token, str) and bool(_TOKEN_RE.match(token))


def is_valid_token(token: str | None) -> bool:
    """Public shape-check for a share token (32 lowercase hex chars). HTTP routes that touch the
    filesystem or a ``gh`` subprocess for a token (publish/revoke) call this to reject a malformed
    token BEFORE it reaches disk/argv — the read/delete paths already guard internally."""
    return _is_valid_token(token)


class ShareStore:
    """Disk-backed store of read-only conversation snapshots, rooted at ``<workspace>/shares``.

    One JSON file per share, named by its token. The store never mutates a snapshot once
    written — a share is created once, read many times, and finally revoked (deleted)."""

    def __init__(self, workspace_dir: Path) -> None:
        self._root = workspace_dir / "shares"

    def create(
        self,
        *,
        items: list[dict[str, Any]],
        title: str,
        created_at: float,
        source_session_id: str,
        usage: dict[str, Any] | None = None,
    ) -> str:
        """Snapshot a chat's rendered transcript and return its unguessable share token.

        ``items`` is the same render-friendly transcript the live UI replays on resume
        (``app.main._history_items``), with any still-pending approval gates already filtered
        out by the caller. Best-effort durable write; mints a fresh token each call so re-sharing
        a chat yields a new, independent link."""
        token = uuid.uuid4().hex
        self._root.mkdir(parents=True, exist_ok=True)
        payload = {
            "token": token,
            "title": title,
            "created_at": created_at,
            "shared_at": time.time(),
            # Kept server-side for revocation/ownership; deliberately NOT echoed to the public
            # viewer route (see app.main.read_share).
            "source_session_id": source_session_id,
            "items": items,
            "usage": usage,
        }
        # Atomic write (temp + replace), matching HistoryStore / BundleStore / autotune: a
        # concurrent reader of /share/<token> never observes a half-written file, and a crash
        # mid-write can't leave a corrupt snapshot — it honors the docstring's "durable write".
        path = self._root / f"{token}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(path)
        return token

    def read(self, token: str | None) -> dict[str, Any] | None:
        """The stored snapshot for ``token``, or None if the token is malformed/unknown."""
        if not _is_valid_token(token):
            return None
        try:
            return json.loads((self._root / f"{token}.json").read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def delete(self, token: str | None) -> bool:
        """Revoke a share (delete its snapshot). True if it existed, False otherwise."""
        if not _is_valid_token(token):
            return False
        path = self._root / f"{token}.json"
        try:
            path.unlink()
            return True
        except OSError:
            return False
