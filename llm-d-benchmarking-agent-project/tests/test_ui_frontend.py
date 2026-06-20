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


def test_metrics_server_passive_hint_on_unavailable_panel():
    """When the live-resource panel reports unavailable (kind ships no metrics-server) it shows a
    PASSIVE hint — no actionable button. The mid-run install button was retired because it lived in
    a busy-only panel and collided with the in-flight-turn guard ("still working on the previous
    request"); the agent now offers the approval-gated install BEFORE the run (a deterministic
    probe fact + a HARD_RULE). Judgment/approval still live in the agent."""
    js = _ui("app.js")
    html = _ui("preview.html")
    # No clickable install control inside the busy-only panel anymore.
    assert "resource-fix-btn" not in js
    assert "Install metrics-server for live stats" not in js
    # A passive hint explains where live stats come from.
    assert "offers to install it" in js
    # The preview still renders the unavailable state so it's hand-verifiable with no backend.
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


def test_agent_suggestion_buttons():
    """The agent's mid-turn 'what next?' offer (suggest_next_steps) renders as clickable buttons
    that send the option's prompt — instead of the agent asking in prose."""
    js = _ui("app.js")
    # The renderer + its dispatch off the suggest_next_steps tool_result.
    assert "function renderAgentSuggestions" in js
    assert 'data.name === "suggest_next_steps"' in js   # rendered from the tool result
    assert "renderAgentSuggestions(r)" in js
    # Clicking a button sends its prompt as the user's next message (same path as welcome chips).
    assert "sendUserMessage(s.prompt)" in js
    # The technical action row is suppressed for this UI-only tool, in BOTH live and replay paths.
    assert js.count('data.name === "suggest_next_steps"') >= 1
    assert 'it.name === "suggest_next_steps"' in js     # renderHistory skips the action row on replay


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


def test_resume_catchup_note_is_deferred_below_history():
    """Switching back to a still-running chat does a full rebuild: `ready` (running=true, non-
    incremental) is immediately followed by a `history` event that renders the restored transcript.
    The 'catching up to live…' note must land at the BOTTOM of that transcript (the seam before the
    live tail replay) — NOT be added eagerly in the `ready` handler, where it would be stranded at
    the very top, above the rebuilt history (the reported bug). So `ready` only FLAGS it and
    renderHistory emits it after the replay loop."""
    js = _ui("app.js")
    # The `ready` handler must not add the note directly — it sets a deferred flag instead.
    assert "cur.pendingResumeNote = true" in js
    ready_block = js.split('case "ready":')[1].split('case "history":')[0]
    assert 'addNote("⏳ Picking up a benchmark already running' not in ready_block, \
        "the catch-up note must be deferred, not added in the ready handler (it strands at the top)"
    # renderHistory consumes the flag and emits the note AFTER rebuilding the transcript.
    rh_block = js.split("function renderHistory(items)")[1].split("function addHistoryTool")[0]
    assert "cur.pendingResumeNote" in rh_block and "pendingResumeNote = false" in rh_block
    assert 'addNote("⏳ Picking up a benchmark already running' in rh_block


def test_approval_card_dedup_self_heals_on_detached_card():
    """Chat-switch-back guard (client side): on reconnect the server re-emits every still-open gate
    (reemit_pending) as the source of truth that it is STILL pending. addApprovalCard must dedup
    against the cached card ONLY when that card is still in the live DOM — if the dedup key survived
    but its card did not (pane rebuilt/evicted/detached, or an older build), it must drop the stale
    ref and re-render, never `return` early and strand the user with no Approve/Decline control."""
    js = _ui("app.js")
    body = js.split("function addApprovalCard(data)")[1].split("function ")[0]
    # The dedup must be GATED on the cached card still being connected to the document...
    assert ".isConnected" in body, \
        "addApprovalCard dedup must verify the cached card is still in the DOM (self-heal)"
    # ...and a stale (disconnected) ref must be cleared so we fall through to re-render it.
    assert "delete cur.pendingApprovals[request_id]" in body, \
        "a stale/disconnected approval card ref must be dropped so the re-emit re-renders it"


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
    # Sharing PUBLISHES by default: after minting, the dialog auto-posts to /publish and shows the
    # public (secret-gist) link, falling back to the same-origin link only if publishing fails.
    assert "function publishShareLink" in js and "function setShareUrl" in js
    assert "/api/share/${encodeURIComponent(token)}/publish" in js
    assert "Publishing a public link" in js and "body.public_url" in js
    assert "gh-missing" in js                                   # the no-gh fallback message branch
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


def test_chat_reading_column_stays_900px():
    """User override #1: the reading column / composer / cards keep the 900px width (NOT 760px)."""
    css = _ui("styles.css")
    # The transcript column and the floating composer both at 900.
    assert "max-width: 900px" in css
    # No content container was left narrowed to 760 (the 760 breakpoint media query is fine).
    for line in css.splitlines():
        if "max-width: 760px" in line:
            assert "@media" in line, f"a 760px content container slipped through: {line.strip()}"


def test_composer_is_filled_panel_card():
    """The composer is a filled, slightly-elevated card (matches the design mock): a --panel fill
    plus rounded border + soft shadow define the box. (Supersedes the earlier 'floating transparent'
    look — see commit b744da1 'give it the mock panel fill'.)"""
    css = _ui("styles.css")
    # Find the #composer rule block and assert it uses the --panel fill (filled card, not transparent).
    block = css.split("#composer {", 1)[1].split("}", 1)[0]
    assert "background: var(--panel)" in block, "composer must be a filled --panel card (matches mock)"
    assert "border-radius: var(--radius-lg)" in block


def test_working_bar_attaches_inside_composer():
    """The working/status bar sits INSIDE the composer box (a top row, hairline-separated)."""
    html = _ui("index.html")
    css = _ui("styles.css")
    # #working is now the first child of the #composer form, not a footer sibling.
    composer_open = html.index('<form id="composer"')
    working_pos = html.index('id="working"')
    input_pos = html.index('id="input"')
    assert composer_open < working_pos < input_pos, "working bar must be inside the composer, above the input"
    # Its CSS gives it a bottom hairline separator (no longer a centered standalone bar).
    work_block = css.split(".working {", 1)[1].split("}", 1)[0]
    assert "border-bottom: 1px solid var(--border)" in work_block


def test_bigger_sidebar_carets():
    """Task 2: the results + namespace-folder carets are bumped up to be more prominent."""
    css = _ui("styles.css")
    folder = css.split(".conv-folder-caret {", 1)[1].split("}", 1)[0]
    results = css.split(".results-caret {", 1)[1].split("}", 1)[0]
    assert "font-size: 16px" in folder
    assert "font-size: 16px" in results


def test_unified_suggestion_button_style():
    """Task 3: welcome chips, next-step buttons, and report actions share ONE suggestion style."""
    css = _ui("styles.css")
    # A single shared rule lists all three component classes together.
    assert ".chip," in css and ".next-step-chip," in css and ".report-action {" in css
    # The next-step list is capped at 4 buttons in app.js.
    js = _ui("app.js")
    assert "r.next_steps.slice(0, 4)" in js


def test_assistant_avatar_is_three_hex_mesh():
    """Task 5: the assistant/report/provenance avatar renders the real 3-hex mesh (not 1 hex)."""
    js = _ui("app.js")
    css = _ui("styles.css")
    assert "function meshAvatarSvg" in js and "function whoEl" in js
    # All four .who call-sites go through whoEl now.
    assert js.count("whoEl(") >= 4, "every assistant/report avatar slot should use whoEl()"
    # The mesh has three hex paths (two purple + one gray), matching the brand logo.
    assert js.count('"hx-p"') >= 2 and '"hx-g"' in js
    # The single-hex ::after mask is now only a fallback for an avatar without an inline svg.
    assert ":not(:has(svg))::after" in css


def test_sidebar_toggle_moved_into_sidebar():
    """Task 6: the collapse control lives in the sidebar; a header expand button re-opens it."""
    html = _ui("index.html")
    js = _ui("app.js")
    css = _ui("styles.css")
    # The in-sidebar toggle sits inside the sidebar brand block (top of the sidebar) — i.e. its id
    # appears in the document AFTER the .sidebar-brand opens.
    assert 'id="sidebar-toggle"' in html
    assert html.index('class="sidebar-brand"') < html.index('id="sidebar-toggle"')
    # The header carries a separate expand affordance, wired + shown only when collapsed.
    assert 'id="sidebar-expand"' in html
    assert 'getElementById("sidebar-expand")' in js
    # Hidden by default, shown only when collapsed. The selector is `.icon-btn.sidebar-expand`
    # (not bare `.sidebar-expand`) so it out-specifies the later `.icon-btn { display:inline-flex }`
    # rule — otherwise the header button leaks through while the sidebar is expanded.
    assert ".icon-btn.sidebar-expand { display: none; }" in css
    assert "body.sidebar-collapsed .icon-btn.sidebar-expand { display: inline-flex; }" in css


def test_share_header_brand():
    """Task 7: shared /share pages show the llm-d brand in the top bar (sidebar is hidden there)."""
    html = _ui("index.html")
    css = _ui("styles.css")
    assert 'class="header-brand"' in html
    assert "body.share-view .header-brand { display: inline-flex; }" in css


def test_persisted_run_duration_on_replay():
    """Task 7: a replayed action row renders the persisted run duration, not just the badge."""
    js = _ui("app.js")
    # addHistoryTool now reads the backend-persisted duration_s (was hard-coded null before).
    assert "it.duration_s" in js


def test_ws_handlers_are_socket_bound():
    """BUG-019: WebSocket handlers must be bound to their own socket instance and gated on
    `sock === ws`, so a superseded socket (after a chat switch / reconnect) stays inert and can't
    spawn a duplicate connection or double-render events. The fragile shared `switching` flag —
    which couldn't gate multiple in-flight deliberate closes — must be gone."""
    js = _ui("app.js")
    # The current socket is captured locally and made the active `ws`.
    assert "const sock = new WebSocket(" in js
    assert "ws = sock;" in js
    # Every handler guards on socket identity; the onclose bail is the key fix.
    assert "if (sock !== ws) return;" in js
    assert "sock.onclose =" in js and "sock.onmessage =" in js
    # The removed flag must not reappear (a single shared boolean can't gate concurrent closes).
    assert "switching = true" not in js
    assert "let switching" not in js


def test_table_row_split_pairs_backticks():
    """BUG-024: splitTableRow must only let a MATCHED pair of backticks open a code span that
    protects pipes. A single stray backtick previously left `inCode` stuck true, swallowing every
    remaining `|` and collapsing the rest of the row into one cell. The fix pre-computes the set of
    paired backtick positions and toggles code mode only on those."""
    js = _ui("app.js")
    # The paired-backtick guard is present; the unconditional toggle is gone.
    assert "paired.has(k)" in js
    assert 'else if (ch === "`") { inCode = !inCode;' not in js


def test_stream_bubble_is_snapshotted_per_chat():
    """BUG-025: the live streaming bubble must be saved/restored per chat (like toolEls/turnUsage),
    so switching away from a mid-stream chat can't append the destination chat's deltas into the
    previous chat's detached pane."""
    js = _ui("app.js")
    assert "streamBubble: null, streamText:" in js          # initialized in makeRecord
    assert "cur.streamBubble = streamBubble" in js          # saved in snapshotActive
    assert "streamBubble = rec.streamBubble" in js          # restored in activate


def test_approval_resolve_guards_socket_before_send():
    """BUG-026: resolving an approval gate must check the socket is OPEN before ws.send — the gate
    buttons stay clickable during a reconnect, and sending on a CLOSING/CLOSED socket throws."""
    js = _ui("app.js")
    i = js.index("const resolve = (ok) =>")
    seg = js[i:i + 600]
    assert "ws.readyState !== WebSocket.OPEN" in seg          # the guard exists in resolve
    assert seg.index("readyState") < seg.index('type: "approval"')  # …and BEFORE the send
