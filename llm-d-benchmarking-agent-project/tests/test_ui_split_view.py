"""Structural regression tests for the live-resource SPLIT VIEW (A3, TODO #2/#8).

There is no JS runtime in CI, so these assert the served static assets are mutually consistent:
the right-hand resource panel exists in the HTML, the split layout exists in the CSS, and the JS
renders the live resource stats into that shared panel (chat-adjacent) rather than inline in the
transcript — opening the split on a `resource_stats` event and collapsing it on `done`.
"""
from __future__ import annotations

from app.config import get_settings


def _ui(name: str) -> str:
    return (get_settings().ui_dir / name).read_text(encoding="utf-8")


def test_index_has_resource_side_panel():
    html = _ui("index.html")
    # The split view's right-hand panel + its body slot the JS writes into.
    assert 'id="resource-side"' in html
    assert 'id="resource-side-body"' in html
    # A manual collapse affordance.
    assert 'id="resource-side-close"' in html


def test_styles_define_split_layout():
    css = _ui("styles.css")
    # The layout toggles on body.split: the side panel opens and the chat column narrows.
    assert "body.split .resource-side" in css
    assert ".resource-side" in css
    # Degrades gracefully on narrow screens (the panel overlays rather than squeezing the chat).
    assert "@media (max-width: 900px)" in css
    # The reusable inner table styles survive the move into the side panel.
    assert ".resource-table" in css


def test_app_js_renders_resource_stats_into_shared_side_panel():
    js = _ui("app.js")
    # The shared panel is referenced (not an inline transcript card).
    assert 'getElementById("resource-side")' in js
    assert "renderResourceSide" in js
    # `resource_stats` opens the split; `done` collapses it.
    assert 'case "resource_stats": renderResourceStats(data)' in js
    assert "clearResourceStats" in js
    assert 'classList.add("split")' in js
    assert 'classList.remove("split")' in js


def test_app_js_resource_view_is_per_chat_and_dashboard_ready():
    js = _ui("app.js")
    # Per-chat snapshot so a chat switch re-renders the front chat's run in the shared panel.
    assert "resourceData" in js
    assert "resourceActive" in js
    # When the event carries a Grafana URL, an "Open Grafana" button (above the metrics) opens the
    # dashboard in a modal overlay; otherwise the live kubectl-top table stands in (graceful fallback).
    assert "dashboard_url" in js
    assert "resource-dash-btn" in js     # the button replaces the old always-on inline iframe
    assert "openGrafanaModal" in js      # click → modal overlay, not an always-on embed
    assert "grafana-modal-frame" in js   # the lazily-loaded iframe lives in the modal now
    # The old always-on inline iframe is gone (replaced by the button-triggered modal).
    assert "resource-dash-frame" not in js


def test_old_inline_resource_panel_is_gone():
    """The old inline transcript card (`.resource-panel` / ensureResourcePanel) is fully replaced
    by the split view, so neither the CSS class nor the helper lingers."""
    css = _ui("styles.css")
    js = _ui("app.js")
    assert ".resource-panel" not in css
    assert "ensureResourcePanel" not in js
