"""Plain-language glossary, parsed from ``knowledge/glossary.md`` (frontend metric explainers).

Non-experts hit two walls (per the proposal): translating a use case into a benchmark, and
*reading* the multidimensional result (TTFT, TPOT, throughput, goodput…). This module feeds the
second: the chat UI fetches the parsed glossary and shows a definitions dialog plus a hover "?"
on each results-card metric. The judgment text lives in ``knowledge/glossary.md`` so it stays
editable and in the agent's own voice; THIS module is mechanism only — it parses that markdown
into a flat ``[{term, definition}]`` list.

Best-effort: a missing/garbled file yields ``[]`` (the UI then simply shows no glossary). Pure
parsing — no per-term knowledge baked in here.
"""
from __future__ import annotations

import re
from pathlib import Path

_GLOSSARY_FILE = "glossary.md"

# A term entry is ``**term** — definition``. The em-dash (—), en-dash (–) or a hyphen all separate
# the term from its gloss. A single markdown bullet may pack SEVERAL such entries (e.g. the
# TTFT/TPOT/throughput/goodput line), so we scan for every ``**term** <dash>`` marker across the
# whole document and take each definition as the text up to the NEXT marker (or end of text).
_TERM_MARKER = re.compile(r"\*\*\s*(?P<term>[^*]+?)\s*\*\*\s*[—–-]\s+")


def build_glossary(knowledge_dir: Path) -> list[dict[str, str]]:
    """Return the parsed glossary ``[{term, definition}]`` from ``knowledge/glossary.md``,
    or ``[]`` when the file is missing/unreadable."""
    try:
        text = (knowledge_dir / _GLOSSARY_FILE).read_text()
    except OSError:
        return []
    return parse_glossary(text)


def _clean(text: str) -> str:
    """Flatten a markdown definition span to a single plain-text line: drop code-span backticks
    and bold markers, collapse the wrapped whitespace, and trim a trailing bullet dash."""
    text = text.replace("`", "").replace("**", "")
    text = re.sub(r"\s+", " ", text).strip()
    # A definition that ran to the end of a bullet may have swallowed the next list marker.
    return text.rstrip().removesuffix("-").strip()


def parse_glossary(text: str) -> list[dict[str, str]]:
    """Parse the glossary markdown into an ordered ``[{term, definition}]`` list.

    Deterministic and mechanism-only: find every ``**term** — `` marker and take its definition as
    everything up to the next marker (or the end). Handles both one-term-per-bullet entries and a
    bullet that packs several inline ``**term** — def`` segments. Entries with an empty term or
    definition are skipped; order is preserved.
    """
    markers = list(_TERM_MARKER.finditer(text))
    entries: list[dict[str, str]] = []
    for i, m in enumerate(markers):
        term = m.group("term").strip()
        end = markers[i + 1].start() if i + 1 < len(markers) else len(text)
        definition = _clean(text[m.end():end])
        if term and definition:
            entries.append({"term": term, "definition": definition})
    return entries
