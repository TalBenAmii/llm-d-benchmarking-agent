"""Publish a frozen shared-chat snapshot as a SECRET GitHub gist — a public link, agent NOT exposed.

``render_shared_chat`` (see :mod:`app.packaging.shared_chat`) turns a snapshot into ONE
self-contained ``.html``; this module uploads that file as a **secret** (unlisted) gist via the
GitHub CLI (``gh``). The chat then has a read-only public URL on GitHub's CDN while the running
agent is never reachable. The gist id is recorded per-token under ``<workspace>/shares/<token>.gist``
so the link can be cleanly revoked later.

That mapping file + the gist description are a **shared contract** with the terminal path
(``scripts/publish_shared_chat.sh``): both write ``<token>.gist`` containing just the gist id and use
the same ``"llm-d shared chat <token>"`` description, so a gist published from the in-app dialog can be
revoked from the script and vice-versa. ``tests/test_gist_publish.py`` pins that contract.

Trust model: ``gh`` runs as a fixed argv with ``shell=False``; the GitHub token lives in the user's
own ``gh`` config and never touches an argv or this process's environment. Publishing is an OUTWARD
action (it creates content on the user's GitHub account), so it is owner-gated wherever it's exposed
(the ``POST`` route is auth-gated; the script is user-run) and is NEVER something the agent does
autonomously — this is mechanism the user explicitly triggers, not an agent tool.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.packaging.shared_chat import render_shared_chat

# The gist description — MUST match scripts/publish_shared_chat.sh so either path can revoke the
# other's gists (the description is how a human spots which gist belongs to which chat).
_GIST_DESC = "llm-d shared chat {token}"

# GitHub serves raw gist files as text/plain, so the rendered HTML is viewed through a static render
# proxy. The canonical raw host is swapped to gist.githack.com (primary); htmlpreview.github.io is a
# fallback that renders the very same raw URL.
_RAW_HOST = "gist.githubusercontent.com"
_GITHACK_HOST = "gist.githack.com"


class GistPublishError(RuntimeError):
    """A publish/revoke step failed. ``reason`` is a stable machine code the caller branches on:

    - ``"gh-missing"`` — the GitHub CLI isn't installed / couldn't be executed.
    - ``"gh-failed"``  — ``gh`` ran but errored (usually: not authenticated, or offline).
    - ``"not-shared"`` — no snapshot exists for the token (nothing to render).
    - ``"not-published"`` — revoke asked for, but no gist was recorded for the token.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class PublishResult:
    """The outcome of a successful publish."""

    token: str
    gist_id: str
    public_url: str    # gist.githack.com render URL — the link to share (primary)
    fallback_url: str  # htmlpreview.github.io render URL of the same file (if the proxy hiccups)
    reused: bool       # True when an existing gist for this token was reused (no new upload)


def mapping_path(workspace: Path | str, token: str) -> Path:
    """Where the token→gist-id mapping for ``token`` lives (the shared-contract location)."""
    return Path(workspace) / "shares" / f"{token}.gist"


def have_gh() -> bool:
    """Whether the GitHub CLI is on PATH (cheap precheck so callers can branch before a publish)."""
    return shutil.which("gh") is not None


def _gh(args: list[str]) -> str:
    """Run ``gh <args>`` (``shell=False``) and return stripped stdout. Raises :class:`GistPublishError`
    on a missing binary or a non-zero exit — never leaking the token (it's in ``gh``'s config, not
    here)."""
    if not have_gh():
        raise GistPublishError(
            "the GitHub CLI 'gh' is not installed (https://cli.github.com), so a public link can't "
            "be published from here.",
            reason="gh-missing",
        )
    try:
        proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    except OSError as exc:  # pragma: no cover - defensive (which() said it exists)
        raise GistPublishError(f"could not run 'gh': {exc}", reason="gh-missing") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        detail = tail[-1] if tail else f"gh exited {proc.returncode}"
        raise GistPublishError(
            f"the GitHub CLI failed (try 'gh auth status'): {detail}", reason="gh-failed"
        )
    return proc.stdout.strip()


def _render_urls(raw_url: str) -> tuple[str, str]:
    """(githack primary, htmlpreview fallback) render URLs for a canonical raw gist URL."""
    githack = raw_url.replace(_RAW_HOST, _GITHACK_HOST, 1)
    htmlpreview = f"https://htmlpreview.github.io/?{raw_url}"
    return githack, htmlpreview


def _raw_url_for(gist_id: str) -> str:
    """The canonical raw URL GitHub serves the gist's first file at (also proves the gist exists)."""
    return _gh(["api", f"gists/{gist_id}", "--jq", "[.files[].raw_url][0]"])


def publish(
    token: str,
    *,
    workspace: Path | str,
    ui_dir: Path,
    snapshot: dict[str, Any] | None = None,
) -> PublishResult:
    """Render ``token``'s snapshot and upload it as a secret gist; return its public render URLs.

    Idempotent per token: if a gist was already recorded for ``token`` and is still reachable, it is
    reused (no second upload). If the recorded gist is gone (revoked out-of-band), a fresh one is
    created and the mapping rewritten. Pass ``snapshot`` to avoid a re-read when the caller already
    loaded it; otherwise it is read from the share store under ``workspace``."""
    workspace = Path(workspace)
    if snapshot is None:
        from app.storage.share import ShareStore

        snapshot = ShareStore(workspace).read(token)
    if snapshot is None:
        raise GistPublishError(
            f"no shared conversation exists for token {token}", reason="not-shared"
        )

    mapping = mapping_path(workspace, token)
    recorded = mapping.read_text(encoding="utf-8").strip() if mapping.exists() else ""

    # 1) Reuse an already-published gist if its raw URL still resolves (no duplicate uploads).
    if recorded:
        try:
            raw = _raw_url_for(recorded)
            if raw:
                githack, htmlpreview = _render_urls(raw)
                return PublishResult(token, recorded, githack, htmlpreview, reused=True)
        except GistPublishError:
            pass  # stale/unreachable mapping → fall through and publish a fresh gist

    # 2) Render + upload a fresh SECRET gist (gh defaults to secret; --public would list it).
    html_doc = render_shared_chat(snapshot, ui_dir=ui_dir)
    with tempfile.TemporaryDirectory(prefix="llmd-share-") as tmp:
        html_file = Path(tmp) / "chat.html"  # tidy gist filename + URL
        html_file.write_text(html_doc, encoding="utf-8")
        gist_url = _gh(["gist", "create", "--desc", _GIST_DESC.format(token=token), str(html_file)])
    gist_id = gist_url.rsplit("/", 1)[-1]
    githack, htmlpreview = _render_urls(_raw_url_for(gist_id))

    mapping.parent.mkdir(parents=True, exist_ok=True)
    mapping.write_text(gist_id + "\n", encoding="utf-8")
    return PublishResult(token, gist_id, githack, htmlpreview, reused=False)


def revoke(token: str, *, workspace: Path | str) -> str:
    """Delete the gist published for ``token`` and forget the mapping; return the deleted gist id.

    Raises ``GistPublishError(reason="not-published")`` when no gist was recorded (nothing to do)."""
    mapping = mapping_path(Path(workspace), token)
    if not mapping.exists():
        raise GistPublishError(
            f"no published gist recorded for token {token}", reason="not-published"
        )
    gist_id = mapping.read_text(encoding="utf-8").strip()
    _gh(["gist", "delete", gist_id])
    mapping.unlink(missing_ok=True)
    return gist_id
