"""Structural wiring tests for the live token-streaming UI (no JS runtime in CI).

Like the other ``test_ui_*`` suites these assert the static asset stays internally consistent —
the streaming handlers exist and are hooked into the right lifecycle points — rather than
running the JS. The visual behaviour is exercised by hand via ``ui/preview.html``.
"""
from __future__ import annotations

from app.config import get_settings


def _app_js() -> str:
    return (get_settings().ui_dir / "app.js").read_text(encoding="utf-8")


def test_assistant_delta_case_and_helpers_are_wired():
    js = _app_js()
    # the live-delta event is handled, and finalize/append helpers exist and are used
    assert 'case "assistant_delta":' in js
    assert "function appendStreamDelta(" in js
    assert "function finalizeStreamBubble(" in js
    assert "appendStreamDelta(data.text" in js
    # assistant_text finalizes the streamed bubble (falling back to a fresh bubble if none open)
    assert "finalizeStreamBubble(data.text)" in js


def test_stream_bubble_reset_on_pane_clear_and_turn_end():
    js = _app_js()
    assert "function resetStreamBubble(" in js
    # the live-bubble reference must be dropped when the pane is wiped and when a turn terminates,
    # so the next turn's first delta starts a fresh bubble instead of appending to a stale node.
    assert js.count("resetStreamBubble()") >= 4  # clearActivePane + done + error + cancelled
