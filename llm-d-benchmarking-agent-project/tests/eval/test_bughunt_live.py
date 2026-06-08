"""(B) OPT-IN autonomous exploratory bug-hunter — SPENDS QUOTA, off by default.

An LLM drives the REAL app over its HTTP+WS surface in an open-ended way (the agent runs
scripted-but-real, so only the explorer LLM spends quota — one small selector call per action).
The DETERMINISTIC invariant battery is the authoritative oracle; only a deterministic finding
with ``severity >= high`` fails this test. The LLM triage is advisory-only and recorded in the
report, never gating. It writes a reviewable report artifact under the gitignored
``workspace/eval/``.

COST: bounded + printed up front — ``len(seeds) * actions_budget`` selector calls (worst case).
Gated by the SAME ``LLM_EVAL_LIVE=1`` switch the live flow eval uses, AND a second ``BUGHUNT=1``
flag (extra conservatism, per the spec) — so it NEVER runs in plain ``pytest`` or gating CI.
Run it with::

    LLM_EVAL_LIVE=1 BUGHUNT=1 .venv/bin/python -m pytest tests/eval/test_bughunt_live.py -v --timeout=600
    # or: make bughunt
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.llm.provider import ProviderError, get_provider

from .bug_report import build_bug_report, severity_ge, write_bug_report
from .explorer import max_selector_calls, run_bughunt

_LIVE = os.getenv("LLM_EVAL_LIVE") == "1"
_BUGHUNT = os.getenv("BUGHUNT") == "1"

# A modest default so the worst-case quota stays small; both are env-overridable for a deeper run.
_SEEDS = [int(s) for s in os.getenv("BUGHUNT_SEEDS", "1,7,42").split(",")]
_BUDGET = int(os.getenv("BUGHUNT_BUDGET", "20"))

pytestmark = [
    pytest.mark.skipif(
        not (_LIVE and _BUGHUNT),
        reason="exploratory bug-hunter is opt-in — set LLM_EVAL_LIVE=1 AND BUGHUNT=1 "
               "(and configure an API key in .env)",
    ),
    pytest.mark.timeout(600),
]


def _has_auth() -> bool:
    """True when the configured provider can be built — auth is in place. Copied from
    test_flows_live.py (provider-agnostic: keyless claude-agent-sdk constructs fine)."""
    try:
        get_provider(get_settings())
        return True
    except ProviderError:
        return False


async def test_bughunt_no_high_severity_findings(tmp_path, capsys) -> None:
    if not _has_auth():
        pytest.skip("no LLM provider configured — set an API key in .env, or log in to the "
                    "`claude` CLI for LLM_PROVIDER=claude-agent-sdk")

    settings = get_settings()
    provider = get_provider(settings)
    explorer_model = getattr(settings, "anthropic_model", None) or settings.llm_provider

    # Print the worst-case quota up front so the cost is never a surprise (acceptance criterion).
    worst = max_selector_calls(_SEEDS, _BUDGET, has_provider=True)
    print(f"\n[bughunt] worst-case selector LLM calls: {worst} "
          f"({len(_SEEDS)} seeds * {_BUDGET} actions)")

    from app.main import app

    findings, total = await run_bughunt(
        app, lambda: TestClient(app), tmp_path,
        seeds=_SEEDS, actions_budget=_BUDGET, provider=provider,
    )

    report = build_bug_report(
        findings, explorer_model=explorer_model, seeds=_SEEDS,
        actions_budget=_BUDGET, total_actions=total,
    )
    eval_dir = settings.resolved_workspace_dir / "eval"
    json_path = write_bug_report(report, eval_dir)

    # Only DETERMINISTIC findings >= high fail the build (LLM triage is advisory).
    blocking = [
        f for f in report["findings"]
        if f["deterministic"] and severity_ge(f["severity"], "high")
    ]
    assert not blocking, (
        f"{len(blocking)} deterministic high-severity finding(s).\n"
        f"report: {json_path}\n"
        + "\n".join(
            f"  - {f['id']} [{f['severity']}/{f['category']}] {f['title']} "
            f"(seed={f['seed']}, action_index={f['action_index']})"
            for f in blocking
        )
    )
