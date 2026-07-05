"""Integrity of the whole key_docs.yaml wiring the agent reads at request time.

Beyond the *_skill tasks: EVERY key doc the agent can fetch must be well-formed
(task/path/why) and must resolve to a real file under the read-only repos, so
fetch_key_docs never dangles. The resolution check is sibling-dependent (uses the
skills_ctx skip-guard); the structural checks are hermetic.
"""
from __future__ import annotations

from collections import Counter

import yaml

from app.config import get_settings
from app.tools import knowledge_access


def _docs():
    path = get_settings().knowledge_dir / "key_docs.yaml"
    return yaml.safe_load(path.read_text())["docs"]


def test_every_key_doc_entry_has_required_fields():
    """Each docs[] entry carries task + path + why (no partial wiring)."""
    bad = [e for e in _docs() if not ({"task", "path", "why"} <= set(e))]
    assert not bad, f"key_docs.yaml entries missing task/path/why: {[e.get('path') for e in bad]}"


def test_key_doc_paths_are_repo_relative_and_unique_per_task():
    """No absolute paths; no duplicate path within a single task."""
    docs = _docs()
    for e in docs:
        assert not e["path"].startswith("/"), f"absolute path in key_docs: {e['path']}"
    per_task: dict[str, list[str]] = {}
    for e in docs:
        per_task.setdefault(e["task"], []).append(e["path"])
    dupes = {t: [p for p, n in Counter(paths).items() if n > 1] for t, paths in per_task.items()}
    dupes = {t: p for t, p in dupes.items() if p}
    assert not dupes, f"duplicate paths within a task: {dupes}"


def test_all_key_docs_resolve_to_real_files(skills_ctx):
    """fetch_key_docs (unfiltered) resolves EVERY key doc — none dangle."""
    res = knowledge_access.fetch_key_docs(skills_ctx, max_bytes_each=200)
    assert res["docs"], "no key docs fetched"
    unresolved = [d["path"] for d in res["docs"] if not d.get("found")]
    assert not unresolved, f"key docs that did not resolve to a real file: {unresolved}"
    assert res["found_count"] == len(res["docs"])
