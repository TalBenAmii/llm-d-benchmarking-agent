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
    # A full-history restore (pane evicted / page reload / resume cursor past the live buffer)
    # must rebuild the rail from the replayed tool calls — otherwise switching away and back wipes
    # the stepper. renderHistory re-derives each phase exactly as the live tool_call stream does.
    assert "advancePhase(it.name, it.input)" in js, \
        "renderHistory must rebuild the run stepper from replayed tool calls"


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
    assert "Install the in-cluster metrics-server" in js
    assert ".resource-fix-btn" in css
    # The preview harness shows the unavailable state so the offer is hand-verifiable with no backend.
    assert "available: false" in html and "no metrics-server" in html


def test_metrics_server_button_queues_when_busy():
    """REGRESSION: the resource panel is shown ONLY during a run (busy === true), and
    sendUserMessage() refuses to send while busy — so a button that called it directly would
    silently no-op (the reported "clicking does nothing"). The button must instead go through
    sendOrQueueUserMessage, which DEFERS the request to turn-end, and that queue must be flushed
    from the `done` handler so the install fires the instant the run finishes."""
    js = _ui("app.js")
    # The button queues rather than calling the busy-guarded sendUserMessage directly.
    assert "sendOrQueueUserMessage(" in js
    assert "function sendOrQueueUserMessage" in js
    assert "function flushPendingUserSend" in js
    # The queue is flushed when a turn ends, so the deferred message actually sends.
    assert "flushPendingUserSend()" in js
    assert 'case "done":' in js and "flushPendingUserSend();" in js
    # A click registers visibly (queued label + sticky flag survives the panel's frequent re-renders).
    assert "metricsInstallRequested" in js
    assert "install queued" in js


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


def test_resilience_drill_card():
    """The run_resilience_drill result renders a resilience card (injected-faults table +
    restart panel + verdict), reusing the existing results-table / slo-pass / slo-fail visuals."""
    js = _ui("app.js")
    css = _ui("styles.css")
    assert "function renderResilienceCard" in js
    # Dispatched from finishTool by tool name…
    assert 'data.name === "run_resilience_drill") renderResilienceCard(r)' in js
    # …and from the results_card event (card.kind === "resilience").
    assert 'card.kind === "resilience"' in js
    assert "renderResilienceCard(card)" in js
    # The card surfaces the proof's three parts.
    assert "Injected faults" in js and "Orchestrator restart drill" in js and "Verdict:" in js
    # A dedicated accent class on the card; otherwise it reuses the shared SLO visuals.
    assert ".results-card.resilience" in css
    # Exposed in the preview boot guard for hand verification.
    assert "renderResilienceCard," in js


def test_autotune_convergence_card():
    """The autotune_search action='status' result renders a goal-seeking convergence card
    (trial table + incumbent + SLO-feasible frontier + budget), reusing the shared
    results-table / slo-pass / slo-fail visuals. Facts only — the card must NOT introduce a
    converge/stop verdict (the stop decision is the agent's)."""
    js = _ui("app.js")
    css = _ui("styles.css")
    assert "function renderAutotuneCard" in js
    # Dispatched from finishTool by tool name (reshaping the raw status result)…
    assert 'data.name === "autotune_search") renderAutotuneCard(_card_from_autotune_status(r))' in js
    assert "function _card_from_autotune_status" in js
    # …and from the results_card event (card.kind === "autotune").
    assert 'card.kind === "autotune"' in js
    assert "renderAutotuneCard(card)" in js
    # The card surfaces the convergence parts.
    assert "Autotune search" in js and "Best feasible so far" in js
    assert "SLO-feasible Pareto frontier" in js
    # Facts-only: the renderer never asserts convergence on its own.
    assert "converged" not in js.split("renderAutotuneCard")[1].split("function renderHistory")[0]
    # A dedicated accent class on the card; otherwise it reuses the shared SLO visuals.
    assert ".results-card.autotune" in css
    # Exposed in the preview boot guard for hand verification.
    assert "renderAutotuneCard," in js


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


def test_results_trends_collapsible():
    """The sidebar Results/trends panel collapses by default and expands UPWARD above an
    always-visible toggle bar pinned at the bottom of the sidebar; state persists like theme/debug."""
    html = _ui("index.html")
    js = _ui("app.js")
    css = _ui("styles.css")
    # The trend content is wrapped in a collapsible body; the toggle bar (with the refresh button)
    # comes AFTER it in source so it renders at the bottom and the body expands above it.
    assert 'id="history-body"' in html
    assert 'id="results-toggle"' in html and 'aria-controls="history-body"' in html
    body_at = html.index('id="history-body"')
    bar_at = html.index('id="results-toggle"')
    assert body_at < bar_at, "toggle bar must follow the collapsible body (pinned at the bottom)"
    # CSS: collapsed is the DEFAULT; `.results-open` (a class on the sidebar) reveals the body.
    assert ".history-body {" in css and "display: none" in css.split(".history-body {")[1]
    assert ".sidebar.results-open .history-body { display: flex; }" in css
    # Caret points up when collapsed (expand) and down when open (collapse).
    assert ".sidebar.results-open .results-caret" in css
    # JS: single source of truth + persistence, defaulting to collapsed.
    assert "function setResultsOpen" in js
    assert '"llmd-results-open"' in js
    assert 'classList.toggle("results-open"' in js


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
    # Click-to-open from the welcome card.
    assert "welcome-build" in js and ".welcome-build" in css
    assert ".builder::backdrop" in css and ".bchip.sel" in css


def test_builder_in_preview_harness():
    """The preview harness must exercise the builder render path too (no backend)."""
    html = _ui("preview.html")
    js = _ui("app.js")
    # The builder entry point is exposed on the preview API and present in the markup.
    assert "openBuilder," in js
    assert 'id="builder"' in html


def test_debug_view_renders_commands_inline_in_chat():
    """The debug view (>_ toggle) reveals each executed command INLINE in the transcript, in
    execution order between the messages — not on a separate command-log screen. Commands are
    appended to the active pane (live and on history replay) and stay hidden until debug is on."""
    html = _ui("index.html")
    js = _ui("app.js")
    css = _ui("styles.css")
    # The toggle button is still present and its state persists across reloads.
    assert 'id="debug-toggle"' in html and 'getElementById("debug-toggle")' in js
    assert 'localStorage.setItem("llmd-debug"' in js
    # A command renders inline by appending a .cmd-inline row to the active transcript pane.
    assert "function addInlineCommand" in js
    assert 'el("div", "cmd-inline")' in js and "activePane.appendChild(row)" in js
    # Both the live `command` event and a replayed `command` history item flow through it.
    assert "addInlineCommand(data)" in js                          # onCommand (live)
    assert 'it.role === "command") addInlineCommand(it)' in js     # renderHistory (resume)
    # Hidden until debug mode is on, then shown in place — no separate screen.
    assert ".cmd-inline { display: none; }" in css
    assert 'html[data-debug="on"] .cmd-inline' in css
    # The OLD separate-screen approach is gone: no command-log section, and debug mode no longer
    # hides the chat transcript.
    assert 'id="cmdlog"' not in html
    assert 'html[data-debug="on"] #transcript' not in css
    assert "cmdlog" not in css
    # The preview harness seeds command events so the inline trail is hand-verifiable with no backend.
    assert 'type: "command"' in _ui("preview.html")


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
    # …and the table is width-contained: it caps at the bubble and scrolls internally rather
    # than blowing the chat column past the viewport (regression guard — see screenshot bug).
    assert "max-width: 100%" in css and "overflow-x: auto" in css
    # The fix only works because the bubble flex item is allowed to shrink below its content
    # width; without min-width:0 a max-content table widens the whole column.
    assert "flex: 1; min-width: 0;" in css


def test_share_a_chat_via_link():
    """Share-a-chat-via-link: the 🔗 header dialog (create + copy + revoke) and the public
    /share/<token> read-only viewer that reuses the live transcript renderers."""
    html = _ui("index.html")
    js = _ui("app.js")
    css = _ui("styles.css")
    # The 🔗 header button + the modal dialog and its controls exist and are wired in JS.
    assert 'id="share-chat"' in html
    for el_id in ("share-chat", "share-dialog", "share-close", "share-done",
                  "share-status", "share-url", "share-copy", "share-open", "share-revoke",
                  "share-download"):
        assert f'id="{el_id}"' in html, f"missing #{el_id} in index.html"
        assert f'getElementById("{el_id}")' in js, f"#{el_id} not wired in app.js"
    # There is deliberately NO read-only banner — the stripped-down viewer makes that obvious.
    assert 'id="share-banner"' not in html and "share-banner" not in css
    # The single-file export: the Download link points at the self-contained .html route.
    assert "/page.html" in js and 'shareDownloadLink.href' in js
    # Create / revoke hit the real owner-only routes; the link is copied with the shared helper.
    assert "function shareChat" in js and "function revokeShare" in js
    assert "/api/sessions/${encodeURIComponent(currentSession)}/share" in js
    assert 'method: "DELETE" }' in js and "/api/share/${encodeURIComponent(shareToken)}" in js
    assert "copyText(shareUrlInput" in js
    # The public viewer: boot detects /share/<token>, renders the snapshot read-only, NO WebSocket.
    assert "function shareTokenFromPath" in js
    assert "function bootShareView" in js
    assert 'location.pathname.match(/^\\/share\\/([0-9a-f]{32})$/)' in js
    assert "} else if (shareTokenFromPath()) {" in js          # boot branch before the live boot
    assert "/api/share/${encodeURIComponent(token)}" in js     # fetches the public transcript
    assert "renderHistory(data.items" in js                    # reuses the live transcript renderer
    assert 'document.body.classList.add("share-view")' in js
    # Read-only mode strips every interactive affordance; a "Read-only snapshot" meta line is
    # the only cue (no banner — see above). The composer lives in <footer>, hidden via that.
    assert 'el("div", "share-meta")' in js and "Read-only snapshot" in js
    assert "body.share-view footer" in css and "body.share-view #sidebar" in css
    assert "body.share-view #composer" not in css  # composer is hidden via <footer>, not directly
    assert ".share-dialog::backdrop" in css
    # Offline self-contained export: the SAME SPA boots from an EMBEDDED snapshot, no network.
    assert "window.__LLMD_SHARED__" in js and "function bootSharedStatic" in js
    assert "function renderSharedSnapshot" in js   # the render path shared by live + static viewers


def test_share_view_in_preview_harness():
    """The preview harness exposes the share-view render paths (live + offline static)."""
    js = _ui("app.js")
    assert "bootShareView," in js and "bootSharedStatic," in js
