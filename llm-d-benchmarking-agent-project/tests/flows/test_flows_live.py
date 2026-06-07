"""Opt-in LIVE eval — does the *real* LLM drive the right commands from natural language?

This is the complement to the deterministic golden-transcript tests: instead of replaying
a scripted ideal, it points the configured model at each flow's ``mock_user_input`` and
scores the commands the model actually chooses. It runs in the SAME hermetic sandbox
(CaptureRunner — nothing is really executed), so it needs only an API key, no Docker / kind
/ repos, and it never touches your cluster.

NON-GATING: skipped unless ``LLM_EVAL_LIVE=1`` and a key is configured. Run it with::

    LLM_EVAL_LIVE=1 .venv/bin/python -m pytest tests/flows/test_flows_live.py -v
    # or: make validate-live

Set ``LLM_EVAL_SIMULATE=1`` as well to drive every flow in the app's SIMULATE mode — the
agent is told (via the system prompt's SIMULATE_NOTE) to walk the WHOLE workflow end-to-end
without pausing for confirmations or missing hardware (no GPU/Docker/kind needed; the sandbox
already executes nothing). This lets the multi-step deploy/teardown flows be scored on the
subcommands/specs they choose, which they otherwise can't reach in a single eval turn::

    LLM_EVAL_LIVE=1 LLM_EVAL_SIMULATE=1 .venv/bin/python -m pytest tests/flows/test_flows_live.py -v --timeout=300

Because a live model is nondeterministic, treat failures as signal to investigate (a
prompt/knowledge gap, or a genuinely wrong choice), not as a hard build break.
"""
from __future__ import annotations

import os

import pytest

from app.config import get_settings
from app.llm.provider import ProviderError, get_provider

from .flows import ALL_FLOWS
from .harness import run_flow, score_flow

_LIVE = os.getenv("LLM_EVAL_LIVE") == "1"
_SIMULATE = os.getenv("LLM_EVAL_SIMULATE") == "1"
_MODE = "simulate" if _SIMULATE else "live"
# A flow is live-scored in THIS run only if it opts into the live eval AND its ``live_modes`` contains
# the active mode. This is what lets coverage span every feature without false failures: error-recovery
# / safety flows are scored only in "live" (the SIMULATE_NOTE would tell the agent to barrel past the
# failure/refusal they test), and multi-step GPU-guide deploys are scored only in "simulate" (they
# can't reach standup/run in real "live" mode — a careful agent refuses a GPU guide on a GPU-less host).
# Read-only / single-decision tool-choice flows declare both. Run BOTH modes to exercise everything:
#   LLM_EVAL_LIVE=1 pytest tests/flows/test_flows_live.py                    # the "live" set
#   LLM_EVAL_LIVE=1 LLM_EVAL_SIMULATE=1 pytest tests/flows/test_flows_live.py # the "simulate" set
_LIVE_FLOWS = [f for f in ALL_FLOWS if f.live_eval and _MODE in f.live_modes]

pytestmark = pytest.mark.skipif(
    not _LIVE,
    reason="live LLM eval is opt-in — set LLM_EVAL_LIVE=1 (and configure an API key in .env)",
)


def _has_auth() -> bool:
    """True when the configured provider can be built — i.e. auth is in place. Provider-agnostic
    on purpose: key-based providers (anthropic/openai) raise ProviderError without their key,
    while keyless ones (``claude-agent-sdk`` — auth via the logged-in ``claude`` CLI subscription,
    no key in config) construct successfully. So this no longer skips the Max-plan setup."""
    try:
        get_provider(get_settings())
        return True
    except ProviderError:
        return False


@pytest.mark.parametrize("flow", _LIVE_FLOWS, ids=[f.name for f in _LIVE_FLOWS])
async def test_live_flow_drives_the_right_commands(flow, tmp_path):
    if not _has_auth():
        pytest.skip("no LLM provider configured — set an API key in .env, or log in to the "
                    "`claude` CLI for LLM_PROVIDER=claude-agent-sdk")

    provider = get_provider(get_settings())
    run = await run_flow(flow, tmp_path=tmp_path, provider=provider, simulate=_SIMULATE)
    passed, notes = score_flow(run, flow)

    mode = "SIMULATE" if _SIMULATE else "live"
    detail = "\n".join(f"  - {n}" for n in notes)
    commands = "\n".join(f"  $ {' '.join(c.argv)}  [{c.mode}]" for c in run.significant) or "  (no significant commands)"
    assert passed, (
        f"[{flow.name}] {mode} eval failed:\n{detail}\n"
        f"commands the model chose:\n{commands}"
    )
