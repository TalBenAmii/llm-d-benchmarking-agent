"""Coupling guard: the live skill-usage eval's SCENARIOS must not drift from the
operation->skill wiring in knowledge/key_docs.yaml.

Every `*_skill` task defined in key_docs.yaml must have exactly one live-eval
scenario (the scenarios are a SUPERSET by exactly the `quickstart` route), and each
*_skill scenario's read_prefix must point at that task's real SKILL.md path. Fully
hermetic (reads only the local knowledge yaml + the eval's SCENARIOS); needs no sibling repos.
"""
from __future__ import annotations

import yaml

from app.config import get_settings
from tests.eval._skills import SKILL_TASKS
from tests.eval.simulate.test_skill_usage_live import SCENARIOS


def _key_docs() -> list[dict]:
    path = get_settings().knowledge_dir / "key_docs.yaml"
    spec = yaml.safe_load(path.read_text())
    return spec["docs"]


def test_scenarios_cover_exactly_the_skill_tasks():
    """Every key_docs.yaml *_skill task has a scenario; the only extra scenario is quickstart."""
    yaml_skill_tasks = {e["task"] for e in _key_docs() if e["task"].endswith("_skill")}
    scenario_keys = {s.key for s in SCENARIOS}
    assert yaml_skill_tasks <= scenario_keys, (
        f"key_docs *_skill tasks without a scenario: {yaml_skill_tasks - scenario_keys}"
    )
    assert scenario_keys - yaml_skill_tasks == {"quickstart"}, (
        f"unexpected non-skill scenarios: {scenario_keys - yaml_skill_tasks - {'quickstart'}}"
    )


def test_scenario_keys_match_shared_skill_tasks_table():
    """Every SKILL_TASKS entry has a scenario; the scenarios add exactly quickstart."""
    scenario_keys = {s.key for s in SCENARIOS}
    assert set(SKILL_TASKS) <= scenario_keys
    assert scenario_keys - set(SKILL_TASKS) == {"quickstart"}


def test_each_scenario_skill_dir_has_matching_skill_md_entry():
    """Each *_skill scenario's read_prefix matches a key_docs.yaml SKILL.md path for that task."""
    docs = _key_docs()
    for s in SCENARIOS:
        if not s.key.endswith("_skill"):
            continue  # quickstart grounds in a knowledge/docs runbook, not a SKILL.md
        entries = [e for e in docs if e["task"] == s.key]
        assert entries, f"{s.key} has no key_docs.yaml entry"
        skill_md = [e for e in entries if e["path"].endswith("SKILL.md")]
        assert skill_md, f"{s.key} has no SKILL.md entry in key_docs.yaml"
        assert all(e["path"].startswith(s.read_prefix) for e in skill_md), (
            f"{s.key} SKILL.md path(s) {[e['path'] for e in skill_md]} not under {s.read_prefix}"
        )


def test_shared_table_dirs_match_scenario_dirs():
    """SKILL_TASKS and the eval scenarios agree on each operation's skill dir."""
    by_key = {s.key: s.read_prefix for s in SCENARIOS}
    for key, skill_dir in SKILL_TASKS.items():
        assert by_key.get(key) == skill_dir, f"{key} dir mismatch: {by_key.get(key)} vs {skill_dir}"
