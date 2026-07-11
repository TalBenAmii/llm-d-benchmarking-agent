"""Self-contained, offline HTML export of a shared conversation.

``render_shared_chat(snapshot, *, ui_dir) -> str`` builds ONE ``.html`` with **zero external
assets**: the live SPA's ``styles.css`` and ``app.js`` inlined, the snapshot embedded as
``window.__LLMD_SHARED__``, and the Google-fonts ``<link>``s stripped (the CSS already falls back
to system fonts). ``app.js``'s boot detects the embedded snapshot and renders it read-only with
**no network at all** — so a shared chat can live as one file on any static host, or be opened
straight from disk, with the agent never involved.

This is the "share a chat without exposing the agent" foundation: the agent only ever *produces*
this static file; sending it to someone or dropping it on a static host is the user's separate,
outward step. Reusing the real SPA means the export is pixel-identical to the live viewer with
zero renderer drift — the same trick the in-app viewer already uses (it reuses ``renderHistory``).

Pure mechanism: it RENDERS an already-frozen snapshot (the same dict ``/api/share/<token>``
returns); it adds no judgment, computes nothing, and fetches nothing. If the UI shell ever loses
the asset refs this inliner rewrites, it raises instead of silently shipping a broken file.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# Only the fields the viewer reads — deliberately WITHOUT ``source_session_id`` (an exported file
# must never carry the owning session id), mirroring the ``/api/share/<token>`` response shape.
_PUBLIC_FIELDS = ("title", "created_at", "shared_at", "items", "usage")

# The two asset refs in app/ui/index.html this inliner rewrites, and the boot marker app.js must carry.
_CSS_REF = '<link rel="stylesheet" href="/static/styles.css" />'
_JS_REF = '<script src="/static/app.js"></script>'
_BOOT_MARKER = "__LLMD_SHARED__"

# The only external assets the shell pulls (Google Fonts). Stripped -> system-font fallback, so the
# file is fully offline and leaks no request to a third party when a recipient opens it.
_FONT_LINK_RE = re.compile(r"[ \t]*<link\b[^>]*fonts\.(?:googleapis|gstatic)\.com[^>]*>\s*\n?")

# JS string/HTML-breakout characters -> \uXXXX. The U+2028/U+2029 line separators are legal in JSON
# strings but illegal raw in JS string literals, so they must be escaped too.
_JSON_SCRIPT_ESCAPES = {
    "<": "\\u003c",
    ">": "\\u003e",
    "&": "\\u0026",
    chr(0x2028): "\\u2028",
    chr(0x2029): "\\u2029",
}


def _embed_json(obj: Any) -> str:
    """Serialize ``obj`` for safe embedding inside an inline ``<script>``. Structural JSON never
    contains ``<``, ``>``, ``&`` or the U+2028/U+2029 line separators outside string values;
    escaping them as ``\\uXXXX`` keeps the text valid JS while making a ``</script>`` or HTML-comment
    breakout impossible."""
    raw = json.dumps(obj, ensure_ascii=False, default=str)
    for ch, esc in _JSON_SCRIPT_ESCAPES.items():
        raw = raw.replace(ch, esc)
    return raw


def render_shared_chat(snapshot: dict[str, Any], *, ui_dir: Path) -> str:
    """Render the SPA shell with ``styles.css`` + ``app.js`` inlined and ``snapshot`` embedded,
    producing a dependency-free, read-only, offline viewer. ``ui_dir`` is ``get_settings().ui_dir``
    (the served ``app/ui/`` directory)."""
    snapshot = snapshot or {}
    public = {k: snapshot.get(k) for k in _PUBLIC_FIELDS}

    html = (ui_dir / "index.html").read_text(encoding="utf-8")
    css = (ui_dir / "styles.css").read_text(encoding="utf-8")
    js = (ui_dir / "app.js").read_text(encoding="utf-8")

    # Fail loudly if the shell drifted out from under us — better than shipping a file that pulls
    # /static/* off a server (defeating "self-contained") or can't render the embedded snapshot.
    if _CSS_REF not in html or _JS_REF not in html:
        raise RuntimeError(
            "shared-chat export: app/ui/index.html no longer links /static/styles.css + /static/app.js "
            "the way render_shared_chat expects — update the inliner."
        )
    if _BOOT_MARKER not in js:
        raise RuntimeError(
            "shared-chat export: app/ui/app.js lacks the window.__LLMD_SHARED__ static-boot path."
        )

    html = _FONT_LINK_RE.sub("", html)                       # drop the only external assets
    html = html.replace(_CSS_REF, f"<style>\n{css}\n</style>")
    embed = f"<script>window.{_BOOT_MARKER} = {_embed_json(public)};</script>"
    html = html.replace(_JS_REF, f"{embed}\n  <script>\n{js}\n  </script>")
    return html


# ---------------------------------------------------------------------------
# CLI entry: ``python -m app.packaging.shared_chat <token> [-o out.html]``. Reads the frozen
# snapshot from the share store and writes the self-contained file — a terminal-side export that
# needn't call the running server.
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    from app.config import get_settings
    from app.storage.share import ShareStore

    parser = argparse.ArgumentParser(
        prog="python -m app.packaging.shared_chat",
        description="Render a shared conversation to a single self-contained .html file.",
    )
    parser.add_argument("token", help="the share token (from POST /api/sessions/<id>/share)")
    parser.add_argument("-o", "--out", help="output file path (default: write to stdout)")
    args = parser.parse_args(argv)

    settings = get_settings()
    snapshot = ShareStore(settings.resolved_workspace_dir).read(args.token)
    if snapshot is None:
        print(f"no shared conversation found for token {args.token!r}", file=sys.stderr)
        return 1

    html_doc = render_shared_chat(snapshot, ui_dir=settings.ui_dir)
    if args.out:
        Path(args.out).write_text(html_doc, encoding="utf-8")
        print(args.out)
    else:
        sys.stdout.write(html_doc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
