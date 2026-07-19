"""(A) OPT-IN LLM-judge eval — SPENDS QUOTA, off by default.

A judge LLM scores each agent session transcript against the versioned ``rubric.md`` and emits
an aggregate AGENT-QUALITY SCORE — the signal that catches *behavioral* regressions the
deterministic flow-eval cannot (the flow-eval asserts the *right commands*; the judge assesses
*interaction quality*). It writes a reviewable scorecard artifact under the gitignored
``workspace/eval/`` and asserts the rubric gate passes.

COST: one judge call per scored flow (plus the real engine running each flow). Gated by the
SAME ``LLM_EVAL_LIVE=1`` switch ``tests/eval/live/test_flows_live.py`` uses — it NEVER runs in plain
``pytest`` or gating CI. Run it with::

    LLM_EVAL_LIVE=1 .venv/bin/python -m pytest tests/eval/live/test_judge_live.py -v --timeout=600
    # or: make eval-judge

Set ``LLM_EVAL_SIMULATE=1`` to drive every flow in the app's SIMULATE mode (so multi-step
deploy flows reach standup/run to be scored) — same dual-mode trick as the live flow eval.
Because a live model is nondeterministic, treat a gate failure as a signal to investigate (a
prompt/knowledge gap, or a genuinely wrong choice), not a hard build break — the always-on
deterministic shadow (``test_scorecard_shadow.py``) carries the real CI weight.
"""
from __future__ import annotations

import os

import pytest

from app.config import get_settings
from tests._auth import has_auth
from tests.eval.judge import judge_session, load_rubric
from tests.eval.scorecard import build_scorecard, write_scorecard
from tests.flows.flows import ALL_FLOWS
from tests.flows.harness import run_flow

_LIVE = os.getenv("LLM_EVAL_LIVE") == "1"
_SIMULATE = os.getenv("LLM_EVAL_SIMULATE") == "1"
_MODE = "simulate" if _SIMULATE else "live"
# Score the SAME flow set the live flow-eval scores in this mode (so the judge spans every
# feature without false failures — error/safety flows in "live", GPU-guide deploys in "simulate").
_JUDGE_FLOWS = [f for f in ALL_FLOWS if f.live_eval and _MODE in f.live_modes]

pytestmark = [
    pytest.mark.skipif(
        not _LIVE,
        reason="LLM-judge eval is opt-in — set LLM_EVAL_LIVE=1 (and log in to the `claude` CLI)",
    ),
    # A judge call per flow over the real agent loop runs well past the 60s hermetic per-test
    # backstop; pin a generous ceiling (matches the --timeout=600 invocation + the Makefile target).
    pytest.mark.timeout(600),
]


async def test_judge_scores_pass_the_gate(tmp_path) -> None:
    if not has_auth():
        pytest.skip("unsupported LLM_PROVIDER — the eval runs on the Claude Agent SDK "
                    "(log in to the `claude` CLI)")

    settings = get_settings()
    rubric = load_rubric()
    judge_model = settings.agent_sdk_model or settings.llm_provider

    results = []
    for flow in _JUDGE_FLOWS:
        run = await run_flow(flow, tmp_path=tmp_path / flow.name, live=True, simulate=_SIMULATE)
        results.append(await judge_session(rubric, run, flow))

    scorecard = build_scorecard(results, rubric, judge_model=judge_model, mode=_MODE)
    # Write the reviewable artifact under the gitignored workspace/eval/ (resolved real workspace).
    eval_dir = settings.resolved_workspace_dir / "eval"
    json_path = write_scorecard(scorecard, eval_dir)

    agg = scorecard["aggregate"]
    detail = "\n".join(
        f"  - {s['flow']}: overall={s['overall']} scores={s['scores']} "
        f"deductions={s['deductions']}"
        for s in scorecard["sessions"]
    )
    assert agg["gate"]["passed"], (
        f"agent-quality gate FAILED (mode={_MODE}, threshold "
        f"{agg['gate']['min_overall_threshold']}, min_overall {agg['min_overall']}).\n"
        f"scorecard: {json_path}\nper-session:\n{detail}"
    )
