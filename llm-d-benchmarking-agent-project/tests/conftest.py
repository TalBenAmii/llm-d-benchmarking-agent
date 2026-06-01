"""Shared pytest fixtures."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import get_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST_PATH = PROJECT_ROOT / "security" / "allowlist.yaml"
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


@pytest.fixture()
def catalog() -> dict[str, list[str]]:
    """A small stand-in for the live on-disk catalog."""
    return {
        "specs": ["cicd/kind", "examples/gpu", "guides/optimized-baseline"],
        "harnesses": ["inference-perf", "guidellm", "vllm-benchmark", "nop"],
        "workloads": ["sanity_random.yaml", "chatbot_synthetic.yaml", "shared_prefix_synthetic.yaml"],
    }
