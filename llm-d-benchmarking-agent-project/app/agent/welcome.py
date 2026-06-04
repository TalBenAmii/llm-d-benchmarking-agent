"""Deterministic start-of-chat welcome (B2 / TODO #3).

A FRESH chat opens with a code-emitted welcome card that concisely offers the assistant's
capabilities — consistent every time, with NO LLM turn spent. The judgment text (heading,
capability bullets, closing nudge) lives in ``knowledge/welcome.md`` so it stays editable and
in the agent's own voice; THIS module is mechanism only: it parses that markdown into the flat
``{heading, bullets, nudge}`` shape the ``welcome`` event carries.

Best-effort: a missing/garbled file, or a file lacking the expected sections, yields ``None``
(the UI then falls back to its suggestion chips / plain note). Pure parsing — no judgment, no
per-field knowledge baked in here.
"""
from __future__ import annotations

from typing import Any

from app.tools.context import ToolContext

_WELCOME_FILE = "welcome.md"


def build_welcome(ctx: ToolContext) -> dict[str, Any] | None:
    """Return the deterministic welcome payload ``{heading, bullets, nudge}`` parsed from
    ``knowledge/welcome.md``, or ``None`` when the file is missing/unreadable or carries no
    capability bullets (the one part the card cannot be useful without)."""
    path = ctx.settings.knowledge_dir / _WELCOME_FILE
    try:
        text = path.read_text()
    except OSError:
        return None
    return parse_welcome(text)


def parse_welcome(text: str) -> dict[str, Any] | None:
    """Parse the welcome markdown into ``{heading, bullets, nudge}``.

    Deterministic, section-driven (mechanism only): the LAST ``## `` heading is the card
    heading (the first ``## `` is the doc's own title), the ``- `` bullets under the
    ``### Capabilities`` section are the capabilities, and the first non-empty line under the
    ``### Nudge`` section is the closing nudge. Returns ``None`` when no capability bullets are
    found — the card is pointless without them. Heading/nudge degrade to ``""`` independently.
    """
    heading = ""
    bullets: list[str] = []
    nudge = ""
    section: str | None = None  # "capabilities" | "nudge" | None

    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith("### "):
            label = stripped[4:].strip().lower()
            section = "capabilities" if label == "capabilities" else "nudge" if label == "nudge" else None
            continue
        if stripped.startswith("## "):
            # A card heading (the FRESH-chat greeting), not the doc's own H1 title. The last
            # such heading wins so the leading explanatory ``## Welcome message …`` title that
            # documents the file is never shown to the user.
            heading = stripped[3:].strip()
            section = None
            continue
        if section == "capabilities" and stripped.startswith("- "):
            bullets.append(stripped[2:].strip())
        elif section == "nudge" and stripped and not nudge:
            nudge = stripped

    if not bullets:
        return None
    return {"heading": heading, "bullets": bullets, "nudge": nudge}
