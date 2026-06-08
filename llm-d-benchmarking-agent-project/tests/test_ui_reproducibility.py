"""Structural regression tests for the reproducibility UI affordances.

No JS runtime in CI, so (like test_ui_frontend.py) these assert the served static assets stay
mutually consistent: the report card + results sidebar expose Reproduce + Export, the
export_run_bundle result renders its own card, the affordances point at the new backend routes,
and app.js stays brace-balanced. Behaviour itself is exercised by hand via ui/preview.html.
"""
from __future__ import annotations

from app.config import get_settings


def _ui(name: str) -> str:
    return (get_settings().ui_dir / name).read_text(encoding="utf-8")


def test_app_js_still_brace_balanced():
    js = _ui("app.js")
    assert js.count("{") == js.count("}"), "app.js brace mismatch — likely a broken edit"


def test_report_actions_helper_offers_reproduce_and_export():
    js = _ui("app.js")
    assert "function reportActions" in js
    # Reproduce sends a canned user message (prompts the agent to call reproduce_run) — NOT a
    # direct mutation. Export opens the self-contained report-card download.
    assert "Reproduce this run" in js
    assert "Export report card" in js
    assert "sendUserMessage(`Reproduce this run from its provenance bundle" in js
    assert "/report-card.html" in js
    assert "window.open(" in js


def test_export_bundle_result_renders_its_own_card():
    js = _ui("app.js")
    # finishTool dispatches the export_run_bundle result to its renderer.
    assert 'data.name === "export_run_bundle"' in js
    assert "renderReproducibilityCard(r)" in js
    assert "function renderReproducibilityCard" in js
    # The card shows a loud dirty banner + the copy-paste regenerate command (reuses copy helper).
    assert "prov-dirty-banner" in js
    assert "wrapWithCopy(pre)" in js


def test_report_summary_card_gains_reproducibility_footer():
    js = _ui("app.js")
    # renderReportSummary appends a .report-actions footer wired to the current session.
    assert "reportActions(result.bundle_id, currentSession)" in js


def test_history_sidebar_rows_get_affordances_when_a_bundle_exists():
    js = _ui("app.js")
    # A stored record with a bundle_id (+ its own session_id) gets Reproduce + Export.
    assert "rec.bundle_id && rec.session_id" in js
    assert "reportActions(rec.bundle_id, rec.session_id)" in js


def test_reproducibility_css_present():
    css = _ui("styles.css")
    for sel in (".report-actions", ".report-action", ".prov-dirty-banner", ".prov-chip", ".prov-cmd"):
        assert sel in css, f"missing {sel} in styles.css"
