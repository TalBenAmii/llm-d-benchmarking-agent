"""Opt-in LIVE eval — does the agent pull the relevant ``llm-d-skills`` SKILL.md into context?

This targets a real, confirmed gap: in a live session the agent ran an operation and NEVER
grounded it in the RIGHT doc. Each scenario points the REAL model at a natural ask that should
trigger one operation, then asserts the agent pulled that operation's grounding doc into context
BEFORE acting on it — matching the FINALIZED, spec-aware routing the skill_gate enforces: a
kind / CPU-sim ask grounds in the ``quickstart`` RUNBOOK (NOT deploy_skill), while a GPU/guide
deploy/benchmark/teardown grounds in its own ``*_skill``, compare in ``compare_skill``, and
autoscaling/WVA in ``wva_skill``.

The signal is mechanism-agnostic: a call counts if it is ``fetch_key_docs(task=<key>)`` OR a
``read_repo_doc`` on a path under the route's ``read_prefix``. It keys on the tool CALL (recorded
in ``run.tool_calls`` regardless of read outcome), so scoring is unaffected by whether the file
read itself succeeds.

NON-GATING: skipped unless ``LLM_EVAL_LIVE=1`` and a provider is configured. Because a live model
is nondeterministic, each scenario runs ``SKILL_EVAL_RUNS`` times (default 3) and passes on a
majority. Run it with::

    LLM_EVAL_LIVE=1 REPOS_DIR=/home/tal/llm-d-benchmarking-agent \
        .venv/bin/python -m pytest tests/eval/simulate/test_skill_usage_live.py -v

``REPOS_DIR`` must point at a checkout whose sibling repos are POPULATED (a git worktree gets them
EMPTY). ``run_flow`` builds a hermetic sandbox (``repos_dir=tmp_path/"repos"``) and copies the real
``llm-d-benchmark`` ``config/`` tree into it from ``REPOS_DIR`` — without it the capacity/plan tools
hit a fake skeleton and can derail the simulate walk before the agent reaches the fetch decision.
(The skills repo is not materialized into the sandbox, so the skill read returns ``found=False``;
that is fine — the eval scores the tool call, not the fetched body.)
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

from app.config import get_settings
from app.llm.provider import get_provider
from tests._auth import has_auth
from tests.flows.flows import Flow
from tests.flows.harness import run_flow

_LIVE = os.getenv("LLM_EVAL_LIVE") == "1"
RUNS = int(os.getenv("SKILL_EVAL_RUNS", "3"))

pytestmark = [
    pytest.mark.skipif(
        not _LIVE,
        reason="live LLM eval is opt-in — set LLM_EVAL_LIVE=1 (and configure an API key in .env)",
    ),
    # RUNS multi-step live sessions per scenario — a generous ceiling so slow-but-healthy runs
    # aren't misreported as failures.
    pytest.mark.timeout(600),
]

# Tool calls that START the operation the skill must precede.
_OPERATION_TOOLS = frozenset({"propose_session_plan", "execute_llmdbenchmark"})


@dataclass(frozen=True)
class SkillScenario:
    key: str          # the key_docs `task` name that grounds this route + the parametrize id
    ask: str          # a natural user request that should trigger this operation
    read_prefix: str  # a read-path prefix under the read-only repos that ALSO counts as grounding
                      # (usually the skill dir; the quickstart DOCS path for the kind route)


# One scenario per grounding ROUTE, matching the FINALIZED spec-aware gate: a kind / CPU-sim deploy
# grounds in the `quickstart` RUNBOOK — NOT deploy_skill (on the cicd/kind path the gate overrides
# the subcommand with quickstart) — while GPU/guide deploy/benchmark/teardown ground in their own
# *_skill, compare in compare_skill, and autoscaling/WVA in wva_skill (fetched dynamically,
# description-driven — not code-gated). `key` is the exact `task` in knowledge/key_docs.yaml; the
# primary pass signal is a fetch_key_docs(task=key) call, with a read under `read_prefix` as the
# mechanism-agnostic fallback. Asks are worded to pin the ROUTE: "local kind quickstart" → the
# quickstart runbook; "guide … on my GPU cluster" → the operation's *_skill.
SCENARIOS = [
    # kind / CPU-sim deploy → the quickstart runbook (spec-aware: quickstart, NOT deploy_skill).
    SkillScenario(
        "quickstart",
        "Set up a fresh llm-d stack on my local kind quickstart cluster — no GPU.",
        "llm-d-benchmark/docs/quickstart",
    ),
    # GPU / guide deploy → deploy_skill.
    SkillScenario(
        "deploy_skill",
        "Deploy the llm-d optimized-baseline guide on my GPU cluster.",
        "llm-d-skills/skills/deploy-llm-d/",
    ),
    # GPU / guide teardown → teardown_skill.
    SkillScenario(
        "teardown_skill",
        "Tear down my llm-d optimized-baseline guide deployment on the GPU cluster.",
        "llm-d-skills/skills/teardown-llm-d/",
    ),
    # Benchmark an existing GPU / guide stack → benchmark_skill.
    SkillScenario(
        "benchmark_skill",
        "Benchmark a small chat model on my already-running GPU llm-d guide stack.",
        "llm-d-skills/skills/run-llm-d-benchmark/",
    ),
    # Compare two configurations → compare_skill.
    SkillScenario(
        "compare_skill",
        "Compare two llm-d configurations to see which one serves better.",
        "llm-d-skills/skills/compare-llm-d-configurations/",
    ),
    # Autoscaling / WVA → wva_skill (fetched dynamically, description-driven — not code-gated).
    SkillScenario(
        "wva_skill",
        "Configure WVA autoscaling for my llm-d deployment.",
        "llm-d-skills/skills/configure-wva-autoscaling-llm-d/",
    ),
]


def _skill_index(tool_calls: list[dict], scenario: SkillScenario) -> int | None:
    """Index of the FIRST call that pulled this scenario's grounding doc into context, else None."""
    for i, call in enumerate(tool_calls):
        inp = call["input"]
        if call["name"] == "fetch_key_docs" and inp.get("task") == scenario.key:
            return i
        if call["name"] == "read_repo_doc" and scenario.read_prefix in inp.get("path", ""):
            return i
    return None


def _operation_index(tool_calls: list[dict]) -> int | None:
    """Index of the first plan/execute operation the skill must precede, else None."""
    for i, call in enumerate(tool_calls):
        if call["name"] in _OPERATION_TOOLS:
            return i
    return None


def _run_passes(tool_calls: list[dict], scenario: SkillScenario) -> bool:
    """A run passes if the skill entered context AND did so before the operation (or the
    operation was never reached). It fails if the skill never entered context."""
    skill_idx = _skill_index(tool_calls, scenario)
    if skill_idx is None:
        return False
    op_idx = _operation_index(tool_calls)
    return op_idx is None or skill_idx < op_idx


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.key for s in SCENARIOS])
async def test_agent_pulls_skill_into_context(scenario, tmp_path):
    if not has_auth():
        pytest.skip("no LLM provider configured — set an API key in .env, or log in to the "
                    "`claude` CLI for LLM_PROVIDER=claude-agent-sdk")

    provider = get_provider(get_settings())
    # turns=[] is a placeholder: with a real provider the harness ignores the golden transcript
    # (ScriptedProvider is only used when provider is None).
    flow = Flow(
        name=f"skill-{scenario.key}",
        title=f"skill usage — {scenario.key}",
        description=f"Does the agent ground task {scenario.key!r} (fetch_key_docs, or a read under "
                    f"{scenario.read_prefix}) before acting?",
        mock_user_input=scenario.ask,
        turns=[],
    )

    # simulate=True so the multi-step operation walks far enough to observe the skill fetch — in
    # plain live mode a careful agent stops at the plan gate before it would read the skill.
    passes = 0
    diagnostics: list[str] = []
    for i in range(RUNS):
        run = await run_flow(flow, tmp_path=tmp_path / f"run{i}", provider=provider, simulate=True)
        in_context = _skill_index(run.tool_calls, scenario) is not None
        if _run_passes(run.tool_calls, scenario):
            passes += 1
        names = [c["name"] for c in run.tool_calls]
        diagnostics.append(f"  run {i}: skill_in_context={in_context} tools={names}")

    detail = "\n".join(diagnostics)
    assert passes > RUNS // 2, (
        f"[{scenario.key}] agent grounded task {scenario.key!r} in only "
        f"{passes}/{RUNS} runs (need a majority). Expected fetch_key_docs(task='{scenario.key}') "
        f"or read_repo_doc under {scenario.read_prefix}, BEFORE "
        f"propose_session_plan/execute_llmdbenchmark.\n{detail}"
    )
