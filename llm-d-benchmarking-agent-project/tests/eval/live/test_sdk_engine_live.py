"""OPT-IN live smoke for the SDK-native engine — the real ``claude`` CLI, real inference.

Skipped unless ``SDK_ENGINE_LIVE=1`` (spends real subscription quota — USER-GATED, never
auto-run; separate from the LLM_EVAL_LIVE flow eval). This module is the landing place for
the Phase 4-live items:

  * the one-turn end-to-end smoke below (engine → CLI → benchtools MCP → stream → events);
  * TODO(phase4-live): the cost/cache-ratio comparison vs the Phase 0 old-engine baseline —
    run the same scripted conversation on both engines live and compare ResultMessage usage
    (cache_read ratio, total tokens). Lands here when the user green-lights the live run.

Run:  SDK_ENGINE_LIVE=1 pytest tests/eval/live/test_sdk_engine_live.py
Nothing outside the test bodies touches the CLI (collection stays hermetic).
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("SDK_ENGINE_LIVE") != "1",
    reason="opt-in live smoke: set SDK_ENGINE_LIVE=1 (spends real quota; user-gated)",
)


async def test_one_turn_end_to_end_real_cli(tmp_path):
    """One real turn through the logged-in CLI: connect, stream, end with ``done`` and no
    error — the minimal proof the engine drives the real transport, not just FakeTransport.
    Commands stay captured (CaptureRunner), so the live model can probe but never mutate."""
    from app.agent.engine import SdkNativeEngine
    from app.agent.session import Session
    from tests._helpers import _capture_ctx

    ctx, _runner = _capture_ctx(tmp_path)
    session = Session(id="sdk-live-smoke", ctx=ctx, catalog_injected=True)
    events: list[tuple[str, dict]] = []

    async def emit(t, p):
        events.append((t, p))

    async def approve(kind, payload):
        return False  # a live smoke must never approve a mutation

    engine = SdkNativeEngine()  # no transport factory → the real CLI
    await engine.run_turn(
        session,
        "Reply with the single word OK. Do not call any tools.",
        emit=emit,
        request_approval=approve,
    )

    types = [t for t, _ in events]
    assert types[-1] == "done"
    assert "error" not in types
    assert session.sdk_session_id, "the CLI conversation id should be minted for resume"
    assert any(p.get("text") for t, p in events if t == "assistant_text")
