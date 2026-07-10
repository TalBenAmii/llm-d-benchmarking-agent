"""knowledge/ is now organized into topic subfolders and every enumeration site walks it
RECURSIVELY (rglob). Recursion makes a future stem/basename collision silent: read_knowledge /
fetch_key_docs / the MCP resource index all resolve a guide by its BASENAME or STEM, so two files
sharing either (in different folders) would shadow each other unpredictably. This guard fails
loudly the moment such a collision is introduced."""
from __future__ import annotations

from collections import Counter

from app.config import get_settings
from app.tools.knowledge_access import EXCLUDED_KNOWLEDGE_FILES


def _knowledge_paths():
    kdir = get_settings().knowledge_dir
    files = list(kdir.rglob("*.md")) + list(kdir.rglob("*.yaml")) + list(kdir.rglob("*.yml"))
    return [f for f in files if f.name not in EXCLUDED_KNOWLEDGE_FILES]


def test_basenames_are_globally_unique():
    dupes = [n for n, c in Counter(f.name for f in _knowledge_paths()).items() if c > 1]
    assert not dupes, f"knowledge basenames collide across folders (rglob would shadow): {dupes}"


def test_stems_are_globally_unique():
    dupes = [s for s, c in Counter(f.stem for f in _knowledge_paths()).items() if c > 1]
    assert not dupes, f"knowledge stems collide across folders (read_knowledge would shadow): {dupes}"
