"""Shared pytest fixtures."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.config import get_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST_PATH = PROJECT_ROOT / "security" / "allowlist.yaml"
# Hermetic baseline: neutralize the developer's .env SIMULATE toggle before the first settings
# read. A dev .env with SIMULATE=1 otherwise makes approval-dependent tests deadlock — simulate
# mode skips the per-command approval those tests wait for. Env vars take precedence over the
# .env file in pydantic-settings; clearing the lru_cache covers any earlier read.
os.environ["SIMULATE"] = "0"
# Tag every session the suite creates with namespace "test" so the test chats cluster under a
# single foldable "test" folder in the sidebar instead of bloating the real chat list (and so
# the namespace-folder feature is exercised end-to-end). Set before the first settings read so
# the cached get_settings() and every direct Settings(...) in the suite pick it up.
os.environ["DEFAULT_SESSION_NAMESPACE"] = "test"
get_settings.cache_clear()
# Resolve the read-only sibling repo via the app's own settings so the suite works
# from any checkout/worktree: honors REPOS_DIR/.env, else falls back to the sibling
# of this project (the layout in the primary checkout). Keeps tests location-portable.
BENCH_REPO = get_settings().bench_repo
BR_DIR = BENCH_REPO / "llmdbenchmark" / "analysis" / "benchmark_report"
BR_SCHEMA = BR_DIR / "br_v0_2_json_schema.json"
BR_EXAMPLE = BR_DIR / "br_v0_2_example.yaml"


@pytest.fixture(scope="session")
def bench_repo() -> Path:
    return BENCH_REPO


@pytest.fixture(scope="session")
def br_schema() -> Path:
    return BR_SCHEMA


@pytest.fixture(scope="session")
def br_example() -> Path:
    return BR_EXAMPLE


@pytest.fixture(scope="session")
def allowlist():
    from app.security.allowlist import Allowlist
    return Allowlist.from_file(ALLOWLIST_PATH)


@pytest.fixture()
def tool_ctx(tmp_path):
    """A ToolContext wired to the real repos but an isolated temp workspace."""
    from app.config import get_settings
    from app.security.allowlist import Allowlist
    from app.security.runner import CommandRunner
    from app.tools.context import ToolContext

    s = get_settings()
    al = Allowlist.from_file(ALLOWLIST_PATH)
    runner = CommandRunner(s.repo_paths)
    return ToolContext(settings=s, allowlist=al, runner=runner, workspace=tmp_path / "ws")


@pytest.fixture(autouse=True)
def _ground_skills_by_default(monkeypatch):
    """Pre-ground every ToolContext built during a test so the skill-grounding gate
    (app/tools/run/skill_gate.py) is INERT by default. That gate refuses a mutating operation (and the
    plan proposing it) until its grounding task is in ``ctx.consulted_skills`` — a per-session ledger
    the real agent fills by calling ``fetch_key_docs``. Tests that aren't ABOUT the gate assume the
    agent already grounded (exactly as they assume no gated-model block by default), so seed every
    context here; the gate's own tests (``tests/tools/test_skill_gate.py``) clear ``consulted_skills`` to
    exercise it. Auto-reverts after each test."""
    from app.tools.context import ToolContext
    from app.tools.run import skill_gate

    all_tasks = {"quickstart"} | set(skill_gate._TASK_BY_SUBCOMMAND.values())
    orig_init = ToolContext.__init__

    def _init(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        self.consulted_skills.update(all_tasks)

    monkeypatch.setattr(ToolContext, "__init__", _init)


@pytest.fixture()
def catalog() -> dict[str, list[str]]:
    """A small stand-in for the live on-disk catalog."""
    return {
        "specs": ["cicd/kind", "examples/gpu", "guides/optimized-baseline"],
        "harnesses": ["inference-perf", "guidellm", "vllm-benchmark", "nop"],
        "workloads": ["sanity_random.yaml", "chatbot_synthetic.yaml", "shared_prefix_synthetic.yaml"],
    }
