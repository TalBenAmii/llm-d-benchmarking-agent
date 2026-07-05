"""Shared data + helpers for the hermetic llm-d-skills eval tests under tests/eval/.

Not a test module (no test_ prefix) — importable as tests.eval._skills.
"""
from __future__ import annotations

from app.tools import knowledge_access

# operation -> skill dir prefix under the read-only llm-d-skills repo (mirrors key_docs.yaml)
SKILL_TASKS = {
    "deploy_skill": "llm-d-skills/skills/deploy-llm-d/",
    "teardown_skill": "llm-d-skills/skills/teardown-llm-d/",
    "benchmark_skill": "llm-d-skills/skills/run-llm-d-benchmark/",
    "compare_skill": "llm-d-skills/skills/compare-llm-d-configurations/",
    "wva_skill": "llm-d-skills/skills/configure-wva-autoscaling-llm-d/",
}


def skills_populated(ctx) -> bool:
    """True when the read-only llm-d-skills repo is materialized (not a bare worktree).

    Probes fetch_key_docs, then resets ctx.fetched_docs so the probe doesn't poison
    the caller's dedup set.
    """
    res = knowledge_access.fetch_key_docs(ctx, task="deploy_skill")
    populated = res.get("found_count", 0) > 0
    ctx.fetched_docs.clear()
    return populated
