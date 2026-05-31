"""Opt-in LIVE eval — does the *real* LLM drive the right commands from natural language?

This is the complement to the deterministic golden-transcript tests: instead of replaying
a scripted ideal, it points the configured model at each flow's ``mock_user_input`` and
scores the commands the model actually chooses. It runs in the SAME hermetic sandbox
(CaptureRunner — nothing is really executed), so it needs only an API key, no Docker / kind
/ repos, and it never touches your cluster.

NON-GATING: skipped unless ``LLM_EVAL_LIVE=1`` and a key is configured. Run it with::

    LLM_EVAL_LIVE=1 .venv/bin/python -m pytest tests/flows/test_flows_live.py -v
    # or: make validate-live

Because a live model is nondeterministic, treat failures as signal to investigate (a
prompt/knowledge gap, or a genuinely wrong choice), not as a hard build break.
"""
from __future__ import annotations

import os

import pytest

from app.config import get_settings
from app.llm.provider import get_provider

from .flows import ALL_FLOWS
from .harness import run_flow, score_flow

_LIVE = os.getenv("LLM_EVAL_LIVE") == "1"
_LIVE_FLOWS = [f for f in ALL_FLOWS if f.live_eval]

pytestmark = pytest.mark.skipif(
    not _LIVE,
    reason="live LLM eval is opt-in — set LLM_EVAL_LIVE=1 (and configure an API key in .env)",
)


def _has_key() -> bool:
    s = get_settings()
    return bool(s.anthropic_api_key or s.openai_api_key)


@pytest.mark.parametrize("flow", _LIVE_FLOWS, ids=[f.name for f in _LIVE_FLOWS])
async def test_live_flow_drives_the_right_commands(flow, tmp_path):
    if not _has_key():
        pytest.skip("no LLM API key configured in .env")

    provider = get_provider(get_settings())
    run = await run_flow(flow, tmp_path=tmp_path, provider=provider)
    passed, notes = score_flow(run, flow)

    detail = "\n".join(f"  - {n}" for n in notes)
    commands = "\n".join(f"  $ {' '.join(c.argv)}  [{c.mode}]" for c in run.significant) or "  (no significant commands)"
    assert passed, (
        f"[{flow.name}] live eval failed:\n{detail}\n"
        f"commands the model chose:\n{commands}"
    )
