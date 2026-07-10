"""The knowledge playbooks must ground operations in REAL llm-d-skills tasks.

The agent reaches skills through the playbooks; a playbook that names a nonexistent
`*_skill` task would silently break grounding. Assert every `task="…_skill"` reference
across knowledge/*.md points at a real key_docs.yaml skill task, and that the quickstart
playbook grounds in the quickstart runbook first. Hermetic (reads only local knowledge/).
"""
from __future__ import annotations

import re

from app.config import get_settings
from tests.eval._skills import SKILL_TASKS

_SKILL_REF = re.compile(r'task=["\']?(\w+_skill)["\']?')


def _knowledge_md():
    return sorted(get_settings().knowledge_dir.rglob("*.md"), key=lambda p: p.name)


def test_playbook_skill_task_references_are_real():
    """Every task="…_skill" reference in the playbooks names a real skill task."""
    bad = []
    total = 0
    for md in _knowledge_md():
        for m in _SKILL_REF.finditer(md.read_text()):
            total += 1
            if m.group(1) not in SKILL_TASKS:
                bad.append((md.name, m.group(1)))
    assert total > 0, "no *_skill task references found in knowledge playbooks (regex drift?)"
    assert not bad, f"playbooks reference unknown skill tasks: {bad}"


def test_quickstart_playbook_grounds_in_quickstart_first():
    """quickstart_playbook.md tells the agent to fetch the quickstart runbook before planning."""
    text = (get_settings().knowledge_dir / "deploy/quickstart_playbook.md").read_text()
    assert 'fetch_key_docs task="quickstart"' in text
