"""Start-of-chat suggestion chips (DATA, no logic).

The chips themselves live in ``suggestions.yaml`` beside this module — deliberately under
``app/agent/`` rather than ``knowledge/`` so they never leak into the system prompt or
``read_knowledge``. This loader is mechanism only: it reads the YAML and returns the flat
``chips`` list, filtered to well-formed ``{label, prompt}`` entries. Best-effort — a missing
file, a parse error, or a wrong shape yields ``[]`` (the UI then falls back to its plain note).
"""
from __future__ import annotations

from pathlib import Path

import yaml

from app.config import Settings

_SUGGESTIONS_PATH = Path(__file__).with_name("suggestions.yaml")


def load_suggestions(settings: Settings) -> list[dict[str, str]]:
    """Return the start-of-chat chips as a list of ``{"label": ..., "prompt": ...}`` dicts.

    Best-effort: returns ``[]`` if the file is missing, unparseable, or the wrong shape, and
    drops any entry lacking both ``label`` and ``prompt`` (each coerced to ``str``)."""
    try:
        data = yaml.safe_load(_SUGGESTIONS_PATH.read_text())
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(data, dict):
        return []
    chips = data.get("chips")
    if not isinstance(chips, list):
        return []
    out: list[dict[str, str]] = []
    for chip in chips:
        if isinstance(chip, dict) and chip.get("label") and chip.get("prompt"):
            out.append({"label": str(chip["label"]), "prompt": str(chip["prompt"])})
    return out
