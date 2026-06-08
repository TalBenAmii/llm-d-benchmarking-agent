"""Structural regression tests for the frontend UI/UX iteration.

There is no JS runtime in CI, so (like ``test_ui_split_view.py``) these assert the three served
static assets stay mutually consistent: every element the JS reaches for exists in the HTML, every
new visual component is wired in all three files, and ``app.js`` stays brace-balanced. They guard
the wiring against drift; the visual behaviour itself is exercised by hand via ``ui/preview.html``.

Covers this iteration's additions:
  * run progress stepper (workflow phase rail)
  * Stop button (cancel control frame + the previously-ignored `cancelled` event)
  * goodput gauge, Pareto scatter, A/B delta bars, harness-compare table
  * live per-pod resource trend sparklines
  * copy-to-clipboard buttons, jump-to-latest, off-canvas mobile sidebar
"""
from __future__ import annotations

from app.config import get_settings


def _ui(name: str) -> str:
    return (get_settings().ui_dir / name).read_text(encoding="utf-8")


def test_app_js_is_brace_balanced():
    """A cheap structural smoke test: block braces must balance (catches a truncated edit)."""
    js = _ui("app.js")
    assert js.count("{") == js.count("}"), "app.js brace mismatch — likely a broken edit"


def test_every_new_element_id_is_wired_in_js():
    """Each new element the JS controls must exist in the HTML AND be looked up in the JS."""
    html = _ui("index.html")
    js = _ui("app.js")
    for el_id in ("run-steps", "stop-run", "jump-latest", "sidebar-toggle", "sidebar-scrim"):
        assert f'id="{el_id}"' in html, f"missing #{el_id} in index.html"
        assert f'getElementById("{el_id}")' in js, f"#{el_id} not wired in app.js"


def test_run_progress_stepper():
    html = _ui("index.html")
    js = _ui("app.js")
    css = _ui("styles.css")
    assert 'id="run-steps"' in html
    # Driven from the tool_call stream and rendered from the per-chat record.
    assert "advancePhase(data.name, data.input)" in js
    assert "function renderRunSteps" in js
    assert "RUN_PHASES" in js and "TOOL_PHASE" in js
    # execute_llmdbenchmark spans phases via its subcommand.
    assert "EXECUTE_SUBCMD_PHASE" in js
    # Phase state is per-chat (survives switches) and resets on a full transcript rebuild.
    assert "phaseReached" in js and "phaseActive" in js
    assert ".run-step.active" in css and ".run-step.done" in css


def test_stop_button_and_cancelled_event():
    js = _ui("app.js")
    css = _ui("styles.css")
    # Sends the cancel control frame the backend has supported since Phase 16…
    assert 'type: "cancel"' in js
    assert "function cancelRun" in js
    # …and now HANDLES the cancelled event the UI used to ignore.
    assert 'case "cancelled"' in js
    assert ".stop-run" in css


def test_goodput_gauge_in_results_card():
    js = _ui("app.js")
    css = _ui("styles.css")
    assert "function goodputGauge" in js
    assert "results-goodput" in js
    # Binding-constraint surfacing: the first missed SLO verdict.
    assert "Limited by" in js
    assert ".gauge-val" in css


def test_pareto_scatter_and_comparison_cards():
    js = _ui("app.js")
    css = _ui("styles.css")
    # Prominent renders dispatched from the analysis tool_results in finishTool.
    for fn in ("renderParetoCard", "renderComparisonCard", "renderHarnessCompareCard",
               "scatterPlot", "deltaBar"):
        assert f"function {fn}" in js, f"missing {fn}"
    assert "renderParetoCard(r)" in js
    assert "renderComparisonCard(r)" in js
    # Pareto uses the per-run objective coordinates already on the analyze_results result.
    assert "on_frontier" in js and "pareto.objectives" in js
    # A/B colours by direction-aware improvement vs the baseline.
    assert "delta_pct" in js
    assert ".scatter-frontier" in css and ".delta-fill" in css


def test_live_resource_trend_sparklines():
    js = _ui("app.js")
    css = _ui("styles.css")
    assert "function accumulateResourceHistory" in js
    assert "function renderResourceTrends" in js
    assert "function resSpark" in js
    # kubectl-top unit normalisation so the sparklines share a stable scale.
    assert "parseCpuMillicores" in js and "parseMemMiB" in js
    # History lives on the per-chat record (survives reconnect/switch).
    assert "resourceHistory" in js
    assert ".res-spark-line" in css


def test_metrics_server_install_offer_on_unavailable_panel():
    """When the live-resource panel reports unavailable (kind ships no metrics-server), it must
    surface a one-click "Install metrics-server" offer. The agent never sees the zero-LLM poller
    event, so this button is what makes the offer visible; it just sends a normal user message and
    the agent does the real, approval-gated install (thin code / thick agent)."""
    js = _ui("app.js")
    css = _ui("styles.css")
    html = _ui("preview.html")
    # The offer button lives in the available===false branch of the resource panel renderer…
    assert "resource-fix-btn" in js
    assert "Install metrics-server for live stats" in js
    # …and it asks the AGENT to install it (a normal message — judgment/approval stay in the agent).
    assert "sendUserMessage(" in js and "Install the in-cluster metrics-server" in js
    assert ".resource-fix-btn" in css
    # The preview harness shows the unavailable state so the offer is hand-verifiable with no backend.
    assert "available: false" in html and "no metrics-server" in html


def test_analyzer_next_steps_chips():
    """The analyzer's ranked next_steps render as clickable chips that send the step as a message."""
    js = _ui("app.js")
    css = _ui("styles.css")
    assert "function renderNextSteps" in js
    assert "next_steps" in js
    assert "renderNextSteps(r)" in js                 # dispatched alongside the sweep scatter
    assert "sendUserMessage(prompt)" in js            # a click acts on the suggestion
    assert ".next-step-chip" in css


def test_preflight_status_cards():
    """The read-only diagnostic tools render friendly status cards (not just raw JSON)."""
    js = _ui("app.js")
    css = _ui("styles.css")
    for fn in ("renderEnvStatus", "renderCapacityCard", "renderReadinessCard",
               "renderAcceleratorCard", "renderDoeCard", "renderOrchestrateCard", "statusCell"):
        assert f"function {fn}" in js, f"missing {fn}"
    # Each is dispatched from finishTool by tool name.
    for tool, fn in (("probe_environment", "renderEnvStatus"),
                     ("check_capacity", "renderCapacityCard"),
                     ("check_endpoint_readiness", "renderReadinessCard"),
                     ("advise_accelerators", "renderAcceleratorCard"),
                     ("generate_doe_experiment", "renderDoeCard"),
                     ("orchestrate_benchmark_run", "renderOrchestrateCard")):
        assert f'data.name === "{tool}") {fn}(r)' in js, f"{tool} not dispatched to {fn}"
    # Shared status visuals.
    assert ".status-dot-ok" in css and ".status-grid" in css and ".diag-list" in css


def test_keyboard_shortcuts_and_help_overlay():
    html = _ui("index.html")
    js = _ui("app.js")
    css = _ui("styles.css")
    assert 'id="shortcuts"' in html and 'id="help-toggle"' in html
    assert "function toggleHelp" in js and "showModal" in js
    # Modifier-gated shortcuts that never swallow typing.
    assert 'e.key === "k"' in js and "input.focus()" in js          # Cmd/Ctrl+K
    assert "sidebar-hidden" in js and "body.sidebar-hidden .sidebar" in css  # Cmd/Ctrl+B focus mode
    assert ".shortcuts::backdrop" in css and "kbd" in css


def test_results_card_copy_summary():
    js = _ui("app.js")
    css = _ui("styles.css")
    assert "function resultsCardMarkdown" in js and "function addCardCopy" in js
    assert "addCardCopy(root, resultsCardMarkdown(card))" in js
    assert ".card-copy" in css


def test_preview_harness_exists_and_is_self_contained():
    """ui/preview.html drives the renderers with fixtures and no backend, for hand verification.
    It must set the preview flag (so app.js skips its live boot), reference the assets relatively
    (works from a plain static server), and use the exposed render API."""
    html = _ui("preview.html")
    js = _ui("app.js")
    assert "__LLMD_PREVIEW__" in html
    assert "__LLMD_PREVIEW__" in js, "app.js must honor the preview flag in its boot guard"
    assert 'src="app.js"' in html and 'href="styles.css"' in html, "preview must use relative assets"
    # The boot guard exposes the render entry points the preview calls.
    assert "window.__llmd" in js
    for fn in ("renderParetoCard", "renderComparisonCard", "renderResultsCard", "renderResourceStats"):
        assert f"A.{fn}(" in html or f"{fn}," in js


def test_copy_buttons_jump_latest_and_mobile_sidebar():
    js = _ui("app.js")
    css = _ui("styles.css")
    # Copy-to-clipboard on code/JSON blocks, with a non-secure-context fallback.
    assert "function wrapWithCopy" in js and "function fallbackCopy" in js
    assert "enhanceCodeBlocks(bubble)" in js
    assert ".copy-btn" in css
    # Jump-to-latest floating button.
    assert "function" in js and 'getElementById("jump-latest")' in js
    assert ".jump-latest" in css
    # Off-canvas sidebar is desktop-inert (only acts within the breakpoint).
    assert "function setSidebar" in js
    assert "body.sidebar-open .sidebar" in css


def test_benchmark_builder_wizard():
    """The guided builder: header CTA + welcome CTA + dialog that composes a brief and sends it.
    Critically, the builder must NOT decide the spec/harness/workload — it only phrases the request
    and hands the mapping back to the agent (thin code / thick agent)."""
    html = _ui("index.html")
    js = _ui("app.js")
    css = _ui("styles.css")
    # Entry points are present and wired.
    assert 'id="builder-toggle"' in html and 'id="builder"' in html
    assert 'getElementById("builder-toggle")' in js and 'getElementById("builder")' in js
    assert "function openBuilder" in js and "function composeBrief" in js and "function submitBuilder" in js
    # All sections the composer reads exist as data-field chip groups.
    for field in ("usecase", "scale", "pattern", "input", "output", "hardware"):
        assert f'data-field="{field}"' in html, f"missing builder field {field}"
    # SLO numeric targets feed the brief.
    for slo in ("slo-ttft", "slo-tpot", "slo-tput"):
        assert f'id="{slo}"' in html and f'"{slo}"' in js
    # The brief is dispatched through the SAME path a typed message uses…
    assert "sendUserMessage(text)" in js
    # …and the closing line hands the actual mapping back to the agent (judgment stays in the agent).
    assert "recommend the right scenario, harness, and workload" in js
    # Click-to-open from the welcome card + a Cmd/Ctrl+J shortcut.
    assert "welcome-build" in js and ".welcome-build" in css
    assert 'e.key === "j"' in js
    assert ".builder::backdrop" in css and ".bchip.sel" in css


def test_metrics_glossary_and_explainers():
    """Plain-language definitions come from /api/glossary (knowledge-sourced) and surface as a
    dialog plus hover '?' explainers on results-card metrics."""
    html = _ui("index.html")
    js = _ui("app.js")
    css = _ui("styles.css")
    assert 'id="glossary-toggle"' in html and 'id="glossary"' in html
    assert "function loadGlossary" in js and '"/api/glossary"' in js
    assert "function openGlossary" in js and "function setGlossary" in js
    # Inline explainer: a "?" badge carrying the definition, attached to each metric row.
    assert "function metricHelp" in js and "metricHelp(m.label)" in js
    # Definitions are NOT duplicated in JS — only the label->term alias is (presentation only).
    assert "METRIC_GLOSSARY_ALIAS" in js and "glossaryIndex" in js
    assert ".glossary::backdrop" in css and ".metric-help" in css
    # Loaded once at boot alongside sessions/history.
    assert "loadGlossary();" in js


def test_builder_and_glossary_in_preview_harness():
    """The preview harness must exercise the new render paths too (no backend)."""
    html = _ui("preview.html")
    js = _ui("app.js")
    # Builder + glossary entry points are exposed on the preview API…
    assert "openBuilder, openGlossary, setGlossary" in js
    # …and the preview seeds the glossary so the dialog + metric explainers render from fixtures.
    assert "A.setGlossary(" in html
    assert 'id="builder"' in html and 'id="glossary"' in html


def test_markdown_table_rendering_is_wired():
    """The assistant bubble's markdown renderer must turn GFM pipe-tables into real <table>
    markup (was: raw `| … |` text). Wiring-level guard; the rendered table is eyeballed via
    ui/preview.html (which seeds an assistant bubble containing a pipe-table)."""
    js = _ui("app.js")
    css = _ui("styles.css")
    # The renderer branch + its helpers exist and emit the styled table class…
    assert "function splitTableRow" in js
    assert "function isTableDelim" in js and "function isTableStart" in js
    assert "function tableCellAlign" in js
    assert "isTableStart(lines, i)" in js          # dispatched from the main block loop
    assert '<table class="md-table">' in js
    assert "<thead>" in js and "<tbody>" in js
    # …and a paragraph stops when a table begins (so the header row isn't swallowed as prose).
    assert "!isTableStart(lines, i)) { para.push" in js
    # …with matching styles (scrollable, zebra header).
    assert "table.md-table" in css and ".md-table th" in css
