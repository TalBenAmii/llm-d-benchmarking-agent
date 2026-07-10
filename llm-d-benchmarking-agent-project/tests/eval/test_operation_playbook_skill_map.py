"""Each operation's knowledge playbook must ground in that operation's llm-d-skill.

The agent adapts a skill through its operation playbook (e.g. deploy via
deploy_path_playbook); this pins the operation -> playbook -> skill wiring so a renamed
skill task or a dropped reference is caught. Hermetic (reads only local knowledge/).
"""
from __future__ import annotations

import re

import pytest

from app.config import get_settings
from tests.eval._skills import SKILL_TASKS

# operation playbook -> the skill task it must ground in
PLAYBOOK_SKILL = {
    "deploy_path_playbook.md": "deploy_skill",
    "teardown.md": "teardown_skill",
    "author_spec_workload.md": "benchmark_skill",
    "sweep_playbook.md": "compare_skill",
    "autoscaling.md": "wva_skill",
}


def _refs(md_name: str) -> set[str]:
    # Playbooks are keyed by basename; resolve through the topic-folder layout (rglob).
    text = next(get_settings().knowledge_dir.rglob(md_name)).read_text()
    return set(re.findall(r'task=["\']?(\w+_skill)["\']?', text))


@pytest.mark.parametrize("playbook,skill", sorted(PLAYBOOK_SKILL.items()))
def test_operation_playbook_grounds_in_its_skill(playbook, skill):
    """The operation playbook references a fetch of its own skill task."""
    path = next(get_settings().knowledge_dir.rglob(playbook), None)
    assert path is not None, f"missing operation playbook {playbook}"
    assert skill in _refs(playbook), f"{playbook} does not ground in {skill}"


def test_mapping_covers_every_skill_operation():
    """Every one of the five operations has a playbook that grounds in its skill."""
    assert set(PLAYBOOK_SKILL.values()) == set(SKILL_TASKS)
