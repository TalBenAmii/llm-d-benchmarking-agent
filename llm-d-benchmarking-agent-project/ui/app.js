// Chat UI client for the llm-d Benchmarking Assistant.
// Talks to the backend over a WebSocket. Renders chat, streamed command output, and
// Approve/Reject cards. No secrets or commands originate here.
//
// Chats are persisted server-side. The left sidebar lists recent chats; selecting one
// reconnects with ?session=<id> so the backend replays its transcript (a "history"
// event), Claude-web style. "New chat" starts a fresh session.

const transcript = document.getElementById("transcript");
const statusEl = document.getElementById("status");
const form = document.getElementById("composer");
const input = document.getElementById("input");
const sendBtn = document.getElementById("send");
// The composer stays usable WHILE a turn runs so the user can steer; the placeholder swaps to
// signal that a send now redirects the in-flight turn rather than starting a new one.
const IDLE_PLACEHOLDER = input.getAttribute("placeholder") || "";
const STEER_PLACEHOLDER = "Steer the agent — type to add to what it's working on…";
const themeBtn = document.getElementById("theme-toggle");
const convList = document.getElementById("conv-list");
const newChatBtn = document.getElementById("new-chat");
const debugBtn = document.getElementById("debug-toggle");
const autoApproveBtn = document.getElementById("autoapprove-toggle");
const historyList = document.getElementById("history-list");
const historyRefresh = document.getElementById("history-refresh");
const trendMetric = document.getElementById("trend-metric");
const trendView = document.getElementById("trend-view");
const sidebarEl = document.getElementById("sidebar");
const resultsToggle = document.getElementById("results-toggle");
const workingEl = document.getElementById("working");
const workWordEl = workingEl.querySelector(".working-word");
const workStatsEl = workingEl.querySelector(".working-stats");
const contextChip = document.getElementById("context-window");
const stopBtn = document.getElementById("stop-run");
const resourceSide = document.getElementById("resource-side");
const resourceSideBody = document.getElementById("resource-side-body");
const resourceSideClose = document.getElementById("resource-side-close");
const runSteps = document.getElementById("run-steps");
const sidebarToggle = document.getElementById("sidebar-toggle");   // collapse control INSIDE the sidebar
const sidebarExpand = document.getElementById("sidebar-expand");    // header re-open affordance (collapsed only)
const sidebarScrim = document.getElementById("sidebar-scrim");
const jumpBtn = document.getElementById("jump-latest");
const builderToggle = document.getElementById("builder-toggle");
const builderDlg = document.getElementById("builder");
const builderClose = document.getElementById("builder-close");
const builderCancel = document.getElementById("builder-cancel");
const builderSend = document.getElementById("builder-send");
const builderPreview = document.getElementById("builder-preview");
// The header shows the ACTIVE conversation's title (the brand mark/name lives in the sidebar now).
// renderConvRow sets it authoritatively for the active chat; switchTo updates it optimistically from
// this cache the moment a row is clicked; the shared viewer sets it to the snapshot's title.
const headerTitle = document.getElementById("header-title");
const convTitles = {};
function setHeaderTitle(t) { if (headerTitle) headerTitle.textContent = t || "New chat"; }
// Whether any MUTATING command streamed under the currently-open live tool — drives its READ-ONLY
// vs MUTATING badge at finish (history tools carry a backend-derived `mutating` flag instead).
let activeToolMutating = false;
// Right-aligned meta on a collapsed tool row: a read-only/mutating badge + (live only) its run time.
function toolMetaSpan(mutating, durText) {
  const meta = el("span", "tool-meta");
  meta.appendChild(el("span", "badge " + (mutating ? "mut" : "ro"), mutating ? "mutating" : "read-only"));
  if (durText) meta.appendChild(el("span", "tool-dur", durText));
  return meta;
}
function fmtDurShort(sec) {
  if (sec == null || !isFinite(sec)) return "";
  if (sec < 10) return sec.toFixed(1) + "s";
  if (sec < 60) return Math.round(sec) + "s";
  const m = Math.floor(sec / 60), s = Math.round(sec % 60);
  return s ? `${m}m ${s}s` : `${m}m`;
}
// Share-a-chat-via-link controls (the 🔗 header button + its modal dialog).
const shareBtn = document.getElementById("share-chat");
const shareDlg = document.getElementById("share-dialog");
const shareClose = document.getElementById("share-close");
const shareDone = document.getElementById("share-done");
const shareStatus = document.getElementById("share-status");
const shareUrlInput = document.getElementById("share-url");
const shareCopyBtn = document.getElementById("share-copy");
const shareOpenLink = document.getElementById("share-open");
const shareDownloadLink = document.getElementById("share-download");
const shareRevokeBtn = document.getElementById("share-revoke");

// ---- theme (dark default, light optional; persisted) --------------------
function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  themeBtn.textContent = theme === "dark" ? "☀" : "☾";
  themeBtn.setAttribute("aria-label", theme === "dark" ? "Switch to light theme" : "Switch to dark theme");
}
function initTheme() {
  let theme = "dark";
  try { theme = localStorage.getItem("llmd-theme") || "dark"; } catch (e) {}
  applyTheme(theme);
}
themeBtn.addEventListener("click", () => {
  const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
  try { localStorage.setItem("llmd-theme", next); } catch (e) {}
  applyTheme(next);
});
initTheme();

// ---- debug view (reveal the executed commands inline in the chat; persisted) ----
// Each command the agent runs is appended to the transcript in execution order, between the
// messages (see addInlineCommand). They're CSS-hidden until debug mode is on, so toggling the
// button just shows/hides the inline command trail in place — no separate screen.
function applyDebug(on) {
  document.documentElement.setAttribute("data-debug", on ? "on" : "off");
  debugBtn.setAttribute("aria-pressed", on ? "true" : "false");
  debugBtn.title = on
    ? "Hide the executed commands from the chat"
    : "Debug view — show the commands the agent ran inline in the chat";
  updateDebugSession();
}
// Refresh the debug-only session-id chip from the active chat's id. Called on debug toggle and
// whenever currentSession changes, so the chip is correct the moment debug mode is revealed.
function updateDebugSession() {
  const chip = document.getElementById("debug-session");
  if (chip) chip.textContent = currentSession ? "session " + currentSession : "no session yet";
}
function initDebug() {
  let on = false;
  try { on = localStorage.getItem("llmd-debug") === "on"; } catch (e) {}
  applyDebug(on);
}
debugBtn.addEventListener("click", () => {
  const on = document.documentElement.getAttribute("data-debug") !== "on";
  try { localStorage.setItem("llmd-debug", on ? "on" : "off"); } catch (e) {}
  applyDebug(on);
});

// ---- auto-approve commands (per-session; server-authoritative, NOT localStorage) ----
// When on, mutating COMMAND approval cards are auto-approved by the backend (the SessionPlan
// gate still always prompts). State is per-chat and lives on the server; the `ready` frame
// seeds the button on connect/reload/chat-switch, so we never persist it client-side.
function applyAutoApprove(on) {
  if (!autoApproveBtn) return;
  autoApproveBtn.setAttribute("aria-pressed", on ? "true" : "false");
  autoApproveBtn.title = on
    ? "Auto-approve is ON — commands run without the Approve card (the plan still asks). Click to turn off."
    : "Auto-approve commands — skip the Approve card for commands in this chat (the plan still asks)";
}
if (autoApproveBtn) {
  autoApproveBtn.addEventListener("click", () => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;  // no socket -> nothing to toggle
    const on = autoApproveBtn.getAttribute("aria-pressed") !== "true";
    ws.send(JSON.stringify({ type: "set_auto_approve", enabled: on }));
    applyAutoApprove(on);  // optimistic; the server persists and re-seeds via `ready`
  });
}
// initDebug() is invoked AFTER currentSession is declared below — applyDebug -> updateDebugSession
// reads currentSession, which is a `let` (temporal dead zone) until its declaration runs.

let ws = null;
let busy = false;
let activeConsole = null;     // <pre> for the currently-running command's output
let currentSession = null;    // id of the chat we're attached to (null until "ready")
initDebug();                  // safe now that currentSession exists (updateDebugSession reads it)
let welcomeCard = null;       // the start-of-chat suggestion-chips card, removed once a turn starts
let readyNoteTimer = null;    // defers the plain "Session ready" note so chips can supersede it
// Split view: the live resource view renders into a single shared right-hand panel (#resource-side),
// NOT inline in the transcript. We hold the ACTIVE chat's latest snapshot here so chat switches can
// re-render the shared panel for whatever chat is now in front (see makeRecord/activate).
let resourceData = null;      // last `resource_stats` payload for the active chat (null = none yet)
let resourceActive = false;   // whether the split view should be open for the active chat

let toolEls = {}; // id -> details element (swapped per chat; the ACTIVE chat's map)

// ---- per-chat state cache (seamless switching) --------------------------
// Switching chats USED to wipe the transcript and rebuild it from history, which reset the
// "thinking" timer to 0, lost scroll position, and collapsed expanded tool panels. Instead we
// keep a per-chat record holding that chat's own DOM pane + working-set, detach/reattach the
// pane on switch (never destroy it), and reconnect with a resume cursor so the server replays
// only the events the cached view missed. Each chat's live state is preserved EXACTLY; a chat
// that kept working in the background catches up the instant you return.
const sessions = {};          // session id -> record (see makeRecord)
let cur = null;               // the active chat's record (drives the renderers below)
let activePane = null;        // cur.pane — the <div.chat-pane> renderers append into
let viewClock = 0;            // monotonic counter for LRU eviction ordering
let stickBottom = true;       // sticky auto-scroll: only jump to bottom if already near it
let unreadCount = 0;          // new messages that arrived while scrolled up — shown on the jump button
const MAX_PANES = 8;          // cap cached panes (memory bound); evict least-recently-viewed

function makeRecord(sid) {
  viewClock += 1;
  return {
    id: sid || null,
    pane: el("div", "chat-pane"),
    toolEls: {}, activeConsole: null,
    welcomeCard: null, resourceData: null, resourceActive: false,
    workStart: 0, workActivity: null, workWordFixed: false, workWord: "Working", workingHidden: true,
    turnUsage: null,
    // The live streaming bubble + its accumulated markdown belong to THIS chat's in-flight turn —
    // snapshot/restore them like the other live-turn state so a mid-stream chat switch can't append
    // one chat's deltas into another chat's (detached) pane.
    streamBubble: null, streamText: "",
    lastSeq: 0, running: false, scrollTop: 0,
    pendingApprovals: {}, order: viewClock,
    // Run progress stepper: furthest workflow phase this chat has reached + the one currently
    // running (-1 = none). State lives only on the record; the shared #run-steps rail renders
    // from the ACTIVE chat's record, so a switch restores each chat's own progress (see activate).
    phaseReached: -1, phaseActive: -1,
    // Live resource history: per-pod CPU/mem samples accumulated from resource_stats ticks,
    // drawn as sparklines in the side panel (the raw kubectl-top table only shows the latest).
    resourceHistory: {},
  };
}

// Snapshot the active chat's working-set into its record and DETACH (not destroy) its pane, so
// returning later restores it byte-for-byte. The live ticking intervals are stopped; the record
// keeps workStart so elapsed continues correctly on return (the server also re-seeds it).
function snapshotActive() {
  if (!cur) return;
  clearInterval(workTimer); clearInterval(wordTimer); workTimer = wordTimer = null;
  if (readyNoteTimer) { clearTimeout(readyNoteTimer); readyNoteTimer = null; }  // don't fire into another pane
  cur.toolEls = toolEls;
  cur.activeConsole = activeConsole;
  cur.welcomeCard = welcomeCard;
  cur.resourceData = resourceData;
  cur.resourceActive = resourceActive;
  cur.workStart = workStart; cur.workActivity = workActivity; cur.workWordFixed = workWordFixed;
  cur.workWord = workWordEl.textContent; cur.workingHidden = workingEl.hidden;
  cur.turnUsage = turnUsage;
  cur.streamBubble = streamBubble; cur.streamText = streamText;  // live stream state stays with its chat
  // The executed-command trail now lives inline in the pane DOM, so detaching the pane
  // preserves it byte-for-byte — no separate cmdlog snapshot needed.
  if (cur.pane) { cur.scrollTop = transcript.scrollTop; cur.pane.remove(); }
}

// Make a record the active chat: attach its pane, load its working-set into the module globals
// the renderers use, restore scroll, and reflect its running state in the shared "working" line.
function activate(rec) {
  cur = rec;
  activePane = rec.pane;
  transcript.appendChild(activePane);
  toolEls = rec.toolEls;
  activeConsole = rec.activeConsole;
  welcomeCard = rec.welcomeCard;
  resourceData = rec.resourceData;
  resourceActive = rec.resourceActive;
  renderResourceSide();                 // reflect THIS chat's run in the shared right-hand panel
  renderRunSteps();                     // reflect THIS chat's workflow progress in the shared rail
  readyNoteTimer = null;
  workStart = rec.workStart; workActivity = rec.workActivity; workWordFixed = rec.workWordFixed;
  turnUsage = rec.turnUsage;
  streamBubble = rec.streamBubble; streamText = rec.streamText;  // restore THIS chat's live stream (or null)
  workWordEl.textContent = rec.workWord || WORK_WORDS[0];
  clearInterval(workTimer); clearInterval(wordTimer); workTimer = wordTimer = null;
  if (rec.running) {
    workingEl.hidden = false;
    renderWorkStats();
    workTimer = setInterval(renderWorkStats, 250);
    wordTimer = setInterval(cycleWord, 2200);
  } else {
    workingEl.hidden = true;
  }
  transcript.scrollTop = rec.scrollTop || transcript.scrollHeight;
  unreadCount = 0;   // a switched-to chat starts "caught up" — don't carry the prior chat's tally
  updateJumpBtn();   // recompute for THIS chat's scroll — a fresh/short pane has nothing to jump to,
                     // so the floating "↓ Latest" button from the previous chat must hide here (the
                     // scroll handler alone won't fire if scrollTop doesn't actually change).
}

// Full-rebuild reset of the ACTIVE pane (used when the server sends a fresh history rather than
// an incremental patch — e.g. first open of a chat, or the resume cursor fell off the buffer).
function clearActivePane() {
  if (activePane) activePane.innerHTML = "";
  resetStreamBubble();          // the live streaming bubble (if any) was just wiped with the pane
  unreadCount = 0;              // the pane was wiped — nothing is unread now
  toolEls = cur ? (cur.toolEls = {}) : {};
  activeConsole = null; if (cur) cur.activeConsole = null;
  welcomeCard = null; if (cur) cur.welcomeCard = null;
  resourceData = null; resourceActive = false;
  if (cur) { cur.resourceData = null; cur.resourceActive = false; }
  renderResourceSide();
  if (cur) { cur.phaseReached = -1; cur.phaseActive = -1; cur.resourceHistory = {}; }
  renderRunSteps();
  turnUsage = null; if (cur) cur.turnUsage = null;
  if (readyNoteTimer) { clearTimeout(readyNoteTimer); readyNoteTimer = null; }
  if (cur) { cur.pendingApprovals = {}; cur.lastSeq = 0; }
}

// Bound memory: keep at most MAX_PANES cached panes, evicting the least-recently-viewed chats
// that are neither active nor running (a running chat must always return to a live-correct view).
function evictPanes() {
  const recs = Object.keys(sessions).map((id) => sessions[id]);
  let over = recs.length - MAX_PANES;
  if (over <= 0) return;
  const evictable = recs.filter((r) => r !== cur && !r.running).sort((a, b) => a.order - b.order);
  for (const r of evictable) {
    if (over <= 0) break;
    if (r.pane) r.pane.remove();
    delete sessions[r.id];
    over -= 1;
  }
}

// "working" indicator state (spinning-hexagon status line; see helpers below)
let workTimer = null, wordTimer = null, workStart = 0, workActivity = null, workWordFixed = false;

// ---- token usage (REAL provider counts; see the `usage` event) -----------
let turnUsage = null;      // latest in-progress-turn totals (live line + per-turn footer)

// Compact token formatting: <1000 -> integer; <1M -> one-decimal k; else one-decimal M.
function fmtTokens(n) {
  n = Number(n) || 0;
  if (n < 1000) return String(Math.round(n));
  if (n < 1000000) return (n / 1000).toFixed(1) + "k";
  return (n / 1000000).toFixed(1) + "M";
}

// The estimated current context-window chip (debugging token usage): shows the ~size of the
// assembled context just sent to the model + a hover breakdown of what dominates it (system vs
// replayed history vs the last tool result). All values are char/4 ESTIMATES (not a tokenizer).
function setContextEstimate(est) {
  if (!contextChip) return;
  if (!est || !est.total_tokens_est) { contextChip.hidden = true; return; }
  contextChip.hidden = false;
  contextChip.textContent = "~" + fmtTokens(est.total_tokens_est) + " ctx";
  contextChip.title =
    "Estimated current context window (≈ chars/4): " +
    "system ~" + fmtTokens(est.system_tokens_est) + " · " +
    "history ~" + fmtTokens(est.history_tokens_est) + " · " +
    "last tool result ~" + fmtTokens(est.last_tool_result_tokens_est) + " (estimate)";
}

// The REAL current context-window meter — the number Claude Code shows as "context used".
// `cw.tokens` is the provider's total_input (fresh + cache_read + cache_write) for the most
// recent call. Renders "N ctx" — the raw count, no model limit/percentage: the active model
// can change (and may be a remote API), so a fixed denominator would be unreliable. The optional
// char/4 `est` (when present) enriches the hover breakdown; falls back to the estimate chip when
// there's no real number yet (pre-feature backend).
function setContextWindow(cw, est) {
  if (!contextChip) return;
  if (!cw || !cw.tokens) {                          // no real number — use the estimate if we have one
    if (est) setContextEstimate(est); else contextChip.hidden = true;
    return;
  }
  contextChip.hidden = false;
  contextChip.textContent = fmtTokens(cw.tokens) + " ctx";
  let tip =
    "Current context window: " + fmtTokens(cw.tokens) + " tokens — real provider count\n" +
    "fresh input " + fmtTokens(cw.input || 0) +
    " · cache read " + fmtTokens(cw.cache_read || 0) +
    " · cache write " + fmtTokens(cw.cache_write || 0);
  if (est && est.total_tokens_est) {
    tip += "\nbreakdown (est ≈ chars/4): system ~" + fmtTokens(est.system_tokens_est) +
      " · history ~" + fmtTokens(est.history_tokens_est) +
      " · last tool result ~" + fmtTokens(est.last_tool_result_tokens_est);
  }
  contextChip.title = tip;
}

// A `usage` event (per LLM call): refresh the running turn tally (live line) + the context meter.
function onUsage(data) {
  turnUsage = data.turn || null;
  // Prefer the REAL context-window meter; fall back to the char/4 estimate (pre-feature backend).
  if (data.context_window) setContextWindow(data.context_window, data.context_est);
  else if (data.context_est) setContextEstimate(data.context_est);
  renderWorkStats();
}

// On `done`, append a small grey footer beneath the just-finished assistant turn.
function appendTurnTokens() {
  if (!turnUsage) return;
  const up = (turnUsage.input || 0) + (turnUsage.cache_read || 0) + (turnUsage.cache_write || 0);
  const down = turnUsage.output || 0;
  const thisTurn = up + down;
  let text = `↑${fmtTokens(up)} ↓${fmtTokens(down)} · ${fmtTokens(thisTurn)} this turn (${turnUsage.calls || 0} call${turnUsage.calls === 1 ? "" : "s"}`;
  if (turnUsage.cache_read > 0) text += ` · ${fmtTokens(turnUsage.cache_read)} cached`;
  text += ")";
  activePane.appendChild(el("div", "turn-tokens", text));
  turnUsage = null;
}

// ---- connection ---------------------------------------------------------

function connect(sid, afterSeq) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  let qs = sid ? `?session=${encodeURIComponent(sid)}` : "";
  // Resume cursor: when we already hold a cached pane for this chat, ask the server to replay
  // only the events past what we've rendered (it patches our view instead of resending history).
  if (sid && afterSeq != null && afterSeq > 0) qs += `${qs ? "&" : "?"}after_seq=${afterSeq}`;
  // Bind every handler to THIS socket instance and ignore events from a socket we've already
  // replaced. A chat switch (or a reconnect) opens a fresh socket and reassigns `ws`; the old
  // socket's close/message then arrive on a later tick when `sock !== ws`. Without this guard a
  // superseded socket's `onclose` would fall through to the auto-reconnect below and spawn a
  // duplicate connection to the now-active chat (and its `onmessage` would double-render events).
  const sock = new WebSocket(`${proto}://${location.host}/ws${qs}`);
  ws = sock;

  sock.onopen = () => { if (sock === ws) setStatus("connected", "ok"); };
  sock.onclose = () => {
    if (sock !== ws) return;                         // superseded socket (a switch/reconnect took over)
    setStatus("disconnected — retrying…", "down");
    setEnabled(false);
    stopWorking();                                   // don't keep spinning while disconnected; "ready".running restarts it
    // Reconnect resumes the SAME chat WITH its cursor: the pane is intact, so the server patches
    // the missed tail rather than rebuilding (no flash on a brief drop).
    setTimeout(() => connect(currentSession, cur ? cur.lastSeq : null), 1500);
  };
  sock.onerror = () => { if (sock === ws) setStatus("connection error", "down"); };
  sock.onmessage = (ev) => { if (sock === ws) handle(JSON.parse(ev.data)); };
}

function switchTo(sid) {
  snapshotActive();                                  // save + detach the chat we're leaving
  // Close the socket we're leaving; connect() below reassigns `ws` to the new socket, so the
  // old socket's deferred onclose sees `sock !== ws` and stays inert (no spurious reconnect).
  try { if (ws) ws.close(); } catch (e) {}
  let rec = sid ? sessions[sid] : null;
  const cacheHit = !!rec;                             // we've shown this chat before → patch it
  if (!rec) { rec = makeRecord(sid); if (sid) sessions[sid] = rec; }
  rec.order = ++viewClock;
  activate(rec);                                      // attach its pane + restore its working-set
  currentSession = sid || null;
  setActiveConvRow(currentSession);                   // move the sidebar highlight NOW — no list rebuild
  updateDebugSession();
  setHeaderTitle(sid ? convTitles[sid] : "New chat"); // optimistic; renderConvRow confirms it
  evictPanes();
  connect(sid || null, cacheHit ? rec.lastSeq : null);
  setSidebar(false);                                  // close the mobile drawer once a chat is chosen
}

function newChat() { switchTo(null); }
function openSession(sid) { if (sid !== currentSession) switchTo(sid); }

// Boot the first chat without going through switchTo (no prior socket to close). The socket
// connect() opens becomes the current `ws`, so a later real disconnect auto-reconnects.
function bootChat() {
  activate(makeRecord(null));
  connect(null, null);
}

function setStatus(text, cls) {
  statusEl.textContent = text;
  statusEl.className = "status" + (cls ? " " + cls : "");
}

function setEnabled(on) {
  busy = !on;
  // Decouple "can type" from "turn idle": the composer is usable whenever the socket is OPEN —
  // even mid-turn — so the user can STEER (a send while busy is queued and the agent picks it up
  // at its next step, Claude-Code style). It locks only when we're disconnected. `on` now only
  // drives autofocus + the steer-hint placeholder, not the enabled state.
  const usable = !!ws && ws.readyState === WebSocket.OPEN;
  input.disabled = !usable;
  sendBtn.disabled = !usable;
  input.placeholder = busy && usable ? STEER_PLACEHOLDER : IDLE_PLACEHOLDER;
  if (on && usable) input.focus();
}

function handle(msg) {
  const { type, data, seq } = msg;
  // Capture whether we're pinned to the bottom BEFORE rendering, so we only auto-scroll when the
  // user hasn't scrolled up to read — this also preserves a restored scroll position on a pure
  // switch-back (where the `ready` frame would otherwise yank us to the bottom).
  stickBottom = (transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight) < 120;
  switch (type) {
    case "ready": {
      currentSession = data.session_id;
      updateDebugSession();
      // A brand-new chat learns its id here — register the active record under it.
      const wasNewChat = !!(cur && !cur.id);
      if (cur) { cur.id = data.session_id; sessions[data.session_id] = cur; }
      setEnabled(true);
      const inc = !!(data.resume && data.resume.incremental);
      // Full rebuild path: drop any cached content so the incoming `history` doesn't duplicate it.
      // Incremental: keep the cached pane and let the missed-tail replay patch it in place.
      if (!inc) clearActivePane();
      // Restore the last-known context-window meter (persisted) so it's right before the next turn.
      setContextWindow(data.context_window, null);
      // Seed the auto-approve toggle from the server (per-chat, persisted) so it reflects THIS chat
      // on connect/reload/switch. Unconditional (outside the !inc rebuild branch) — a chat switch
      // that resumes incrementally must still re-point the button at the active chat's state.
      applyAutoApprove(!!data.auto_approve);
      if (!inc) {
        // DEFER the catch-up note: on this full-rebuild path the `history` event arrives right
        // after `ready`, and renderHistory appends the restored transcript. Adding the note here
        // would strand it ABOVE that transcript (at the very top — the bug). Flag it instead and
        // let renderHistory drop it at the BOTTOM: the boundary between the restored history and
        // the live tail replay that follows.
        if (data.running) { if (cur) cur.pendingResumeNote = true; }
        // A brand-new chat shows the welcome card with suggestion chips (a `suggestions` event
        // follows `ready`). The plain note is only a FALLBACK for when no chips arrive — defer it
        // briefly so the chips, if any, supersede it.
        else if (!data.resumed) scheduleReadyNote();
      }
      // Refresh the sidebar list only when it must change — a brand-new chat has to appear in it.
      // Switching to a chat already in the list leaves its content identical (only the active-row
      // highlight moves, done synchronously in switchTo), so refetching + rebuilding the whole list
      // here just blanked and repainted it for a frame: the flicker on every switch. Title/timestamp
      // refreshes still ride in via `session_saved` and `done`.
      if (wasNewChat) loadSessions();
      if (data.running) { if (cur) cur.running = true; resumeWorking(data.running_elapsed_ms); }  // re-seed elapsed from the server
      // Not running per the SERVER (authoritative). The turn may have FINISHED while this chat was
      // detached, in which case activate() optimistically restarted the spinner from the now-stale
      // cached `running` flag — and it would tick on at inflated WALL-CLOCK elapsed (incl. the time
      // spent in other chats), e.g. "3m" for a turn that actually ran 30s. Stop it: the buffered tail
      // (incremental) or the history rebuild (full) restores the finished transcript.
      else { if (cur) cur.running = false; stopWorking(); }
      break;
    }
    case "history": renderHistory(data.items || []); break;
    case "welcome": renderWelcome(data); break;
    case "suggestions": renderSuggestions(data.chips || []); break;
    // The backend persisted this chat at the START of the turn — refresh the sidebar NOW so a
    // brand-new chat (e.g. one started by clicking an option chip) appears immediately instead of
    // only when the turn finishes (`done`, which also calls loadSessions, possibly tens of seconds later).
    case "session_saved": loadSessions(); break;
    case "assistant_text":
      removeWelcomeCard();                          // the conversation has started — clear the chips
      // If this step streamed deltas, finalize that live bubble with the authoritative text
      // (re-render markdown + code blocks). Otherwise (non-streaming provider) add a fresh bubble.
      if (!finalizeStreamBubble(data.text)) addBubble("assistant", data.text);
      noteNewMessage();                             // tally it if the user is scrolled up reading history
      if (!workingEl.hidden) resumeThinking();      // between steps: back to generic cycling
      break;
    // A token-by-token fragment of the agent's reply, streamed live as it generates. Append it to
    // the live assistant bubble (created on the first delta); the step's `assistant_text` above
    // finalizes it. NON_TURN_EVENT (no seq) — purely a perceived-latency win, never buffered.
    case "assistant_delta":
      removeWelcomeCard();
      appendStreamDelta(data.text || "");
      if (!workingEl.hidden) resumeThinking();
      break;
    // suggest_next_steps is a UI-only tool: it has no command/phase and no technical action row —
    // its result is the {label,prompt} chip list, drawn as floating buttons when the tool_result
    // arrives (renderToolResultCards → renderAgentSuggestions). So skip the action-row + phase here.
    case "tool_call": if (data.name === "suggest_next_steps") break; startTool(data); setWorkTool(data.name); advancePhase(data.name, data.input); break;
    // Clear the welcome card only when a real turn is running — NOT for the background environment
    // pre-probe's read-only `command` events (which fire before any user message), so the start-of-
    // chat welcome/capabilities card stays visible instead of being wiped the moment the probe runs.
    case "command": if (cur && cur.running) removeWelcomeCard(); onCommand(data); setWorkActivity(data.text || (data.argv || []).join(" ")); break;
    case "output": appendConsole(data.line); break;
    case "tool_result": finishTool(data); resumeThinking(); break;
    case "results_card": renderResultsCard(data.card); break;
    case "approval_request": if (addApprovalCard(data)) noteNewMessage(); if (cur) cur.running = false; clearPhaseActive(); stopWorking(); setEnabled(true); break;  // now waiting on the user: they can click Approve/Decline OR type a message to steer (which declines + redirects); tally only if a NEW card rendered (a reconnect re-emit dedups)
    case "error": resetStreamBubble(); addBubble("error", data.message); noteNewMessage(); if (cur) cur.running = false; clearPhaseActive(); stopWorking(); break;
    case "cancelled": resetStreamBubble(); addNote("⏹ " + (data.message || "run cancelled")); noteNewMessage(); if (cur) cur.running = false; clearPhaseActive(); stopWorking(); break;  // a `done` follows and re-enables input
    case "usage": onUsage(data); break;
    case "resource_stats": renderResourceStats(data); break;
    case "done": resetStreamBubble(); setEnabled(true); activeConsole = null; if (cur) cur.running = false; clearPhaseActive(); appendTurnTokens(); clearResourceStats(); if (cur) cur.resourceRunEnded = true; loadSessions(); loadHistory(); stopWorking(); break;
    case "pong": break;
  }
  // Advance this chat's resume cursor for every turn event we rendered (live or replayed); the
  // next reconnect sends it as ?after_seq so we patch only what's new.
  if (seq != null && cur) cur.lastSeq = Math.max(cur.lastSeq, seq);
  scroll();
}

// ---- recent-chats sidebar -----------------------------------------------

async function loadSessions() {
  try {
    const r = await fetch("/api/sessions");
    const j = await r.json();
    renderSidebar(j.sessions || []);
  } catch (e) { /* offline — keep whatever's shown */ }
}

// Chats are grouped into one folder per Kubernetes namespace; un-namespaced chats live in a
// "no_namespace" folder until an approved plan assigns one. We persist the set of COLLAPSED
// folders (not the expanded ones) so a brand-new folder defaults to expanded automatically —
// anything not in the set is open. Mirrors the localStorage try/catch used for theme/debug.
const NO_NAMESPACE = "no_namespace";
function loadCollapsedFolders() {
  try { return new Set(JSON.parse(localStorage.getItem("llmd-folders-collapsed") || "[]")); }
  catch (e) { return new Set(); }
}
function saveCollapsedFolders(set) {
  try { localStorage.setItem("llmd-folders-collapsed", JSON.stringify([...set])); } catch (e) {}
}
let collapsedFolders = loadCollapsedFolders();

function renderSidebar(sessions) {
  convList.innerHTML = "";
  if (!sessions.length) {
    convList.appendChild(el("div", "conv-empty", "No conversations yet."));
    return;
  }
  // Group by namespace, preserving the backend's newest-first order within each group. A Map
  // keeps first-insertion order, and the first chat seen for a namespace is its most recent —
  // so folders end up ordered by most-recent activity (the freshest folder on top).
  const groups = new Map();
  for (const s of sessions) {
    const ns = s.namespace || NO_NAMESPACE;
    if (!groups.has(ns)) groups.set(ns, []);
    groups.get(ns).push(s);
  }
  for (const [ns, items] of groups) {
    convList.appendChild(renderFolder(ns, items));
  }
}

function renderFolder(ns, items) {
  const collapsed = collapsedFolders.has(ns);
  const folder = el("div", "conv-folder" + (collapsed ? " collapsed" : ""));
  const head = el("div", "conv-folder-head");
  head.title = ns;
  head.appendChild(el("span", "conv-folder-caret", "▾"));   // CSS rotates it when collapsed
  head.appendChild(el("span", "conv-folder-name", ns));
  head.appendChild(el("span", "conv-folder-count", String(items.length)));
  const del = el("button", "conv-folder-del", "×");
  del.type = "button";
  del.title = "Delete this folder and all its chats";
  del.onclick = (e) => { e.stopPropagation(); deleteFolder(ns, items); };   // don't toggle the fold
  head.appendChild(del);
  head.onclick = () => {
    if (collapsedFolders.has(ns)) collapsedFolders.delete(ns);
    else collapsedFolders.add(ns);
    saveCollapsedFolders(collapsedFolders);
    folder.classList.toggle("collapsed");        // pure CSS show/hide — no refetch
  };
  folder.appendChild(head);
  const body = el("div", "conv-folder-body");
  for (const s of items) body.appendChild(renderConvRow(s));
  folder.appendChild(body);
  return folder;
}

function renderConvRow(s) {
  const row = el("div", "conv" + (s.id === currentSession ? " active" : ""));
  if (s.id) row.dataset.sid = s.id;   // lets setActiveConvRow() move the highlight without a list rebuild
  convTitles[s.id] = s.title || "";
  if (s.id === currentSession) setHeaderTitle(s.title);   // keep the header in sync with the active chat
  row.title = s.title || "New chat";
  const main = el("div", "conv-main");
  main.appendChild(el("div", "conv-title", s.title || "New chat"));
  main.appendChild(el("div", "conv-time", relTime(s.updated_at)));
  const del = el("button", "conv-del", "×");
  del.type = "button";
  del.title = "Delete conversation";
  del.onclick = (e) => { e.stopPropagation(); deleteSession(s.id); };
  row.appendChild(main);
  row.appendChild(del);
  row.onclick = () => openSession(s.id);
  return row;
}

// Move the active-row highlight in place — instant and rebuild-free. renderSidebar() is no longer
// called on a plain switch (re-rendering the whole list blanked it for a frame = the per-switch
// flicker), so the highlight that used to come "for free" from a full re-render is moved here.
function setActiveConvRow(sid) {
  if (!convList) return;
  convList.querySelectorAll(".conv.active").forEach((r) => r.classList.remove("active"));
  if (!sid) return;
  const sel = window.CSS && CSS.escape ? CSS.escape(sid) : sid;
  const row = convList.querySelector('.conv[data-sid="' + sel + '"]');
  if (row) row.classList.add("active");
}

async function deleteSession(sid) {
  if (!confirm("Delete this conversation?")) return;
  try { await fetch(`/api/sessions/${encodeURIComponent(sid)}`, { method: "DELETE" }); } catch (e) {}
  if (sid === currentSession) newChat();   // start fresh if we deleted the open one
  else loadSessions();
}

// Remove a whole folder — every chat in one namespace at once. The "no_namespace" folder
// deletes the un-namespaced chats (the backend maps that sentinel to "namespace unset").
async function deleteFolder(ns, items) {
  const where = ns === NO_NAMESPACE ? "with no namespace" : `in namespace "${ns}"`;
  if (!confirm(`Delete all ${items.length} chat(s) ${where}? This can't be undone.`)) return;
  try { await fetch(`/api/namespaces/${encodeURIComponent(ns)}`, { method: "DELETE" }); } catch (e) {}
  collapsedFolders.delete(ns);                 // the folder's gone — forget its collapse state
  saveCollapsedFolders(collapsedFolders);
  if (items.some((s) => s.id === currentSession)) newChat();   // we deleted the open chat → start fresh
  else loadSessions();
}

function relTime(ts) {
  if (!ts) return "";
  const then = new Date(ts * 1000);
  const diff = (Date.now() - then.getTime()) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return Math.floor(diff / 60) + "m ago";
  if (diff < 86400) return Math.floor(diff / 3600) + "h ago";
  if (diff < 604800) return Math.floor(diff / 86400) + "d ago";
  return then.toLocaleDateString();
}

// ---- stored results browser + trends ------------------------------------
// Read-only views of what the agent persisted via the result_history tool. The
// backend returns facts only (values + the metric's better-direction); we render
// them — the regression verdict is the agent's job in chat.

async function loadHistory() {
  try {
    const r = await fetch("/api/history");
    const j = await r.json();
    renderHistory_(j.records || []);
    populateTrendMetrics(j.metrics || []);
    if (trendMetric && trendMetric.value) loadTrend(trendMetric.value);
  } catch (e) { /* offline — keep whatever's shown */ }
}

function populateTrendMetrics(metrics) {
  if (!trendMetric || !metrics.length) return;
  // Reconcile against the CURRENT options rather than a one-shot flag: a later run can introduce a
  // metric absent from history at first load, and the user must be able to trend it without a page
  // reload. Append only metrics not already present; never disturb the current selection.
  const have = new Set(Array.from(trendMetric.options).map((o) => o.value));
  for (const m of metrics) {
    if (have.has(m)) continue;
    const opt = document.createElement("option");
    opt.value = m;
    opt.textContent = m;
    trendMetric.appendChild(opt);
  }
}

function renderHistory_(records) {
  if (!historyList) return;
  historyList.innerHTML = "";
  if (!records.length) {
    historyList.appendChild(el("div", "history-empty", "No stored results yet."));
    return;
  }
  for (const rec of records) {
    const row = el("div", "history-row");
    row.title = [rec.spec, rec.harness, rec.workload].filter(Boolean).join(" · ") || rec.run_uid || "";
    const top = el("div", "history-title", rec.label || rec.model || rec.run_uid || rec.id);
    row.appendChild(top);
    const meta = el("div", "history-meta");
    if (rec.model) meta.appendChild(el("span", "history-model", rec.model));
    meta.appendChild(el("span", "history-time", relTime(rec.stored_at)));
    row.appendChild(meta);
    if (rec.tags && rec.tags.length) {
      const tagWrap = el("div", "history-tags");
      for (const t of rec.tags) tagWrap.appendChild(el("span", "history-tag", t));
      row.appendChild(tagWrap);
    }
    // Reproducibility affordances: a stored record with a provenance bundle gets the same
    // Reproduce + Export report-card actions as the live report card (wired to its OWN session).
    if (rec.bundle_id && rec.session_id) {
      row.appendChild(reportActions(rec.bundle_id, rec.session_id));
    }
    historyList.appendChild(row);
  }
}

async function loadTrend(metric) {
  if (!trendView) return;
  if (!metric) { trendView.innerHTML = ""; return; }
  try {
    const r = await fetch(`/api/history/trend?metric=${encodeURIComponent(metric)}`);
    const t = await r.json();
    renderTrend(t);
  } catch (e) { trendView.innerHTML = ""; }
}

function renderTrend(t) {
  trendView.innerHTML = "";
  if (t.error) { trendView.appendChild(el("div", "history-empty", t.error)); return; }
  const points = t.points || [];
  if (points.length < 2) {
    trendView.appendChild(el("div", "history-empty", `Not enough stored results to trend ${t.metric} yet.`));
    return;
  }
  const wrap = el("div", "trend-card");
  const units = t.units ? ` (${t.units})` : "";
  const better = t.better === "lower" ? "lower is better" : "higher is better";
  wrap.appendChild(el("div", "trend-title", `${t.metric}${units} — ${better}`));
  wrap.appendChild(sparkline(points, t.better));
  // Factual first→last delta; the chat agent gives the verdict, not the UI.
  const d = t.first_to_last || {};
  if (d.delta_pct != null) {
    const improved = (t.better === "lower") ? (d.delta_pct < 0) : (d.delta_pct > 0);
    const cls = d.delta_pct === 0 ? "flat" : (improved ? "good" : "bad");
    const sign = d.delta_pct > 0 ? "+" : "";
    wrap.appendChild(el("div", "trend-delta " + cls, `first → last: ${sign}${d.delta_pct}%`));
  }
  trendView.appendChild(wrap);
}

// Tiny inline SVG sparkline of the value series (oldest → newest).
function sparkline(points, better) {
  const W = 220, H = 46, pad = 4;
  const vals = points.map((p) => p.value);
  const min = Math.min(...vals), max = Math.max(...vals);
  const span = (max - min) || 1;
  const n = points.length;
  const x = (i) => pad + (i * (W - 2 * pad)) / (n - 1);
  const y = (v) => H - pad - ((v - min) / span) * (H - 2 * pad);
  const svgNS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(svgNS, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("class", "spark");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `${n} points trend`);
  const path = document.createElementNS(svgNS, "polyline");
  path.setAttribute("points", points.map((p, i) => `${x(i).toFixed(1)},${y(p.value).toFixed(1)}`).join(" "));
  path.setAttribute("class", "spark-line");
  svg.appendChild(path);
  // Mark the latest point.
  const last = document.createElementNS(svgNS, "circle");
  last.setAttribute("cx", x(n - 1).toFixed(1));
  last.setAttribute("cy", y(points[n - 1].value).toFixed(1));
  last.setAttribute("r", "2.6");
  last.setAttribute("class", "spark-dot");
  svg.appendChild(last);
  return svg;
}

if (trendMetric) trendMetric.addEventListener("change", () => loadTrend(trendMetric.value));
if (historyRefresh) historyRefresh.addEventListener("click", loadHistory);

// Results-trends collapse: the panel is collapsed by default and expands UPWARD above the
// always-visible "Results" bar pinned at the bottom of the sidebar. State persists across
// reloads like theme/debug (mirrors that localStorage try/catch).
function setResultsOpen(open) {
  if (sidebarEl) sidebarEl.classList.toggle("results-open", open);
  if (resultsToggle) resultsToggle.setAttribute("aria-expanded", open ? "true" : "false");
  try { localStorage.setItem("llmd-results-open", open ? "on" : "off"); } catch (e) {}
}
(function initResultsOpen() {
  let open = false;
  try { open = localStorage.getItem("llmd-results-open") === "on"; } catch (e) {}   // default: collapsed
  setResultsOpen(open);
})();
if (resultsToggle) {
  resultsToggle.addEventListener("click", () =>
    setResultsOpen(!(sidebarEl && sidebarEl.classList.contains("results-open"))));
}

// ---- rendering ----------------------------------------------------------

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

// ---- minimal, XSS-safe markdown -> HTML for assistant bubbles ------------
// We escape the text FIRST, then apply a bounded set of transforms, so the only
// HTML tags that ever reach the DOM are the ones we generate here — never raw
// markup from the model.
function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// Inline spans on an already-escaped string: `code`, [text](url), **bold**, *italic*/_italic_.
function mdInline(s) {
  const codes = [];
  s = s.replace(/`([^`]+)`/g, (_, c) => { codes.push(c); return "\uE000" + (codes.length - 1) + "\uE000"; });
  s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, text, url) => {
    if (!/^(https?:\/\/|\/|#)/i.test(url)) return m;      // only safe schemes / relative
    return `<a href="${url.replace(/"/g, "%22")}" target="_blank" rel="noopener noreferrer">${text}</a>`;
  });
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
  s = s.replace(/(^|[^\w])_([^_\s][^_]*)_/g, "$1<em>$2</em>");
  return s.replace(/\uE000(\d+)\uE000/g, (_, i) => `<code>${codes[+i]}</code>`);
}

// ---- GFM tables ----------------------------------------------------------
// A table is a header row of `|`-separated cells, a delimiter row (cells of only
// `-`/`:`/spaces, with at least one `|`), then zero+ body rows. We run on the
// already-escaped string, so cell text is safe; `|`/`\|` aren't escaped, so we
// split on UNescaped pipes (respecting inline-code spans and `\|` escapes).
function splitTableRow(row) {
  row = row.trim().replace(/^\|/, "").replace(/\|$/, "");
  // Only backticks that form a MATCHED pair within the row open a code span that protects pipes.
  // A single stray/unmatched backtick must NOT toggle code mode — otherwise it would stay "open"
  // for the rest of the row and swallow every remaining `|`, collapsing the row into one cell.
  const ticks = [];
  for (let k = 0; k < row.length; k++) if (row[k] === "`") ticks.push(k);
  const paired = new Set();
  for (let k = 0; k + 1 < ticks.length; k += 2) { paired.add(ticks[k]); paired.add(ticks[k + 1]); }
  const cells = []; let cur = "", inCode = false;
  for (let k = 0; k < row.length; k++) {
    const ch = row[k];
    if (ch === "\\" && row[k + 1] === "|") { cur += "|"; k++; }
    else if (ch === "`" && paired.has(k)) { inCode = !inCode; cur += ch; }
    else if (ch === "|" && !inCode) { cells.push(cur); cur = ""; }
    else cur += ch;
  }
  cells.push(cur);
  return cells.map((c) => c.trim());
}
function isTableDelim(line) {
  if (!line || !line.includes("|")) return false;            // require a pipe (rules out a `---` rule)
  const cells = splitTableRow(line);
  return cells.length >= 1 && cells.every((c) => /^:?-+:?$/.test(c));
}
// A table starts where line i is a header (has a pipe, not a fence) and line i+1 is a delimiter.
function isTableStart(lines, i) {
  return i + 1 < lines.length && lines[i].includes("|") &&
         !/^```/.test(lines[i]) && isTableDelim(lines[i + 1]);
}
function tableCellAlign(delim) {                              // ":-" left \u00B7 "-:" right \u00B7 ":-:" center
  const l = delim.startsWith(":"), r = delim.endsWith(":");
  return l && r ? ' style="text-align:center"' : r ? ' style="text-align:right"' : "";
}

const _MD_SPECIAL = [/^```/, /^(#{1,3})\s+/, /^\s*[-*]\s+/, /^\s*\d+\.\s+/, /^\s*$/];

function renderMarkdown(text) {
  const lines = escapeHtml(text).split("\n");
  let html = "", i = 0, listType = null;
  const closeList = () => { if (listType) { html += `</${listType}>`; listType = null; } };
  while (i < lines.length) {
    const line = lines[i];
    let m;
    if (/^```/.test(line)) {                                  // fenced code block
      closeList();
      const buf = []; i++;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) { buf.push(lines[i]); i++; }
      i++;                                                    // skip the closing fence
      html += `<pre class="md-code"><code>${buf.join("\n")}</code></pre>`;
    } else if ((m = line.match(/^(#{1,3})\s+(.*)$/))) {       // heading (#..###)
      closeList();
      const lvl = m[1].length + 2;
      html += `<h${lvl} class="md-h">${mdInline(m[2])}</h${lvl}>`; i++;
    } else if ((m = line.match(/^\s*[-*]\s+(.*)$/))) {        // unordered list item
      if (listType !== "ul") { closeList(); html += "<ul>"; listType = "ul"; }
      html += `<li>${mdInline(m[1])}</li>`; i++;
    } else if ((m = line.match(/^\s*(\d+)\.\s+(.*)$/))) {     // ordered list item
      // Start each run at its explicit number, so items split by blank lines (1. … 2. …)
      // keep their numbering instead of every block restarting at 1.
      if (listType !== "ol") { closeList(); html += `<ol start="${m[1]}">`; listType = "ol"; }
      html += `<li>${mdInline(m[2])}</li>`; i++;
    } else if (/^\s*$/.test(line)) {                          // blank -> block break
      closeList(); i++;
    } else if (isTableStart(lines, i)) {                      // GFM table
      closeList();
      const heads = splitTableRow(lines[i]);
      const aligns = splitTableRow(lines[i + 1]).map(tableCellAlign);
      i += 2;
      let body = "";
      while (i < lines.length && lines[i].includes("|") && !/^\s*$/.test(lines[i]) && !isTableStart(lines, i)) {
        const cells = splitTableRow(lines[i]); i++;
        body += "<tr>" + heads.map((_, c) => `<td${aligns[c] || ""}>${mdInline(cells[c] || "")}</td>`).join("") + "</tr>";
      }
      const head = "<tr>" + heads.map((h, c) => `<th${aligns[c] || ""}>${mdInline(h)}</th>`).join("") + "</tr>";
      html += `<table class="md-table"><thead>${head}</thead><tbody>${body}</tbody></table>`;
    } else {                                                  // paragraph (joins soft-wrapped lines)
      closeList();
      const para = [line]; i++;
      while (i < lines.length && !_MD_SPECIAL.some((re) => re.test(lines[i])) && !isTableStart(lines, i)) { para.push(lines[i]); i++; }
      html += `<p>${mdInline(para.join("<br>"))}</p>`;
    }
  }
  closeList();
  return html;
}

// The assistant/report/provenance avatar: the real llm-d 3-hexagon mesh (same shape as the
// sidebar brand logo), painted as an inline SVG into the .who box so it auto-themes (its strokes
// use the brand-purple via the .logo CSS). Replaces the single masked hexagon for a crisper,
// on-brand mark. The user role keeps a plain (hidden) label; everything else gets the mesh.
function meshAvatarSvg() {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 30 32");
  svg.setAttribute("width", "22");
  svg.setAttribute("height", "23");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "llm-d");
  const g = document.createElementNS(SVG_NS, "g");
  g.setAttribute("fill", "none");
  g.setAttribute("stroke-width", "2.6");
  g.setAttribute("stroke-linejoin", "round");
  g.setAttribute("stroke-linecap", "round");
  const paths = [
    ["hx-p", "M15 2.5 22.36 6.75 22.36 15.25 15 19.5 7.64 15.25 7.64 6.75Z"],
    ["hx-g", "M9.5 12 16.86 16.25 16.86 24.75 9.5 29 2.14 24.75 2.14 16.25Z"],
    ["hx-p", "M20.5 12 27.86 16.25 27.86 24.75 20.5 29 13.14 24.75 13.14 16.25Z"],
  ];
  for (const [cls, d] of paths) {
    const p = document.createElementNS(SVG_NS, "path");
    p.setAttribute("class", cls);
    p.setAttribute("d", d);
    g.appendChild(p);
  }
  svg.appendChild(g);
  return svg;
}

// Build the .who avatar slot for a message role. Assistant/report/provenance/error → the 3-hex
// mesh logo; user → the (CSS-hidden) "you" label. Shared by every bubble/card builder so the
// avatar is identical everywhere.
function whoEl(role) {
  if (role === "user") return el("div", "who", "you");
  const who = el("div", "who logo");   // .logo gives the hex strokes their brand colours
  who.appendChild(meshAvatarSvg());
  return who;
}

function addBubble(role, text) {
  const wrap = el("div", `msg ${role}`);
  wrap.appendChild(whoEl(role));
  if (role === "assistant") {
    // The agent writes markdown; render it. User/error text stays literal (so a user's
    // own `**` is never interpreted and errors show raw).
    const bubble = el("div", "bubble markdown");
    bubble.innerHTML = renderMarkdown(text || "");
    enhanceCodeBlocks(bubble);
    wrap.appendChild(bubble);
  } else {
    wrap.appendChild(el("div", "bubble", text || ""));
  }
  activePane.appendChild(wrap);
}

function addNote(text) { addBubble("assistant", text); }

// ---- live streaming assistant bubble -------------------------------------
// The agent streams its reply token-by-token via `assistant_delta` events (see app/agent/events.py
// + app/llm/agent_sdk_provider.py). We render those into ONE live bubble as they arrive; the step's
// final `assistant_text` then finalizes it (authoritative re-render). `streamBubble` is the live
// <div.bubble> or null between steps; `streamText` accumulates the raw markdown so each delta
// re-renders the whole block (markdown isn't append-safe — a half-open `**` or table needs the
// full source). Deltas are unbuffered/seqless, so a mid-turn reconnect just rebuilds from history.
let streamBubble = null;
let streamText = "";

function appendStreamDelta(text) {
  if (!streamBubble) {
    const wrap = el("div", "msg assistant");
    wrap.appendChild(whoEl("assistant"));
    streamBubble = el("div", "bubble markdown");
    wrap.appendChild(streamBubble);
    activePane.appendChild(wrap);
    streamText = "";
  }
  streamText += text;
  streamBubble.innerHTML = renderMarkdown(streamText);
}

// Finalize the live streaming bubble with the authoritative full text from `assistant_text`
// (re-render markdown + wire up code-block copy buttons). Returns true if a live bubble was open
// (and finalized), false if there was nothing to finalize — so the caller adds a normal bubble.
function finalizeStreamBubble(text) {
  if (!streamBubble) return false;
  streamBubble.innerHTML = renderMarkdown(text || streamText || "");
  enhanceCodeBlocks(streamBubble);
  streamBubble = null;
  streamText = "";
  return true;
}

// Drop the live-bubble reference (leaving any DOM in place) so the NEXT delta starts a fresh
// bubble. Called when the pane is cleared/rebuilt or a turn ends — never appends to a stale node.
function resetStreamBubble() { streamBubble = null; streamText = ""; }

// ---- start-of-chat welcome card + suggestion chips -----------------------
// On a brand-new chat the server emits a DETERMINISTIC `welcome` event (heading + capability
// bullets + nudge — built by the backend, NOT the LLM, so it's consistent every time and costs
// no tokens) right after `ready`, then a `suggestions` event with the chips. Both render into ONE
// welcome card; clicking a chip sends its prompt. The plain "Session ready…" note is only a
// fallback shown when neither arrives (or they arrive late).

// Defer the plain note briefly so a `welcome`/`suggestions` event (which follows `ready`) can
// supersede it. If a card renders first, the timer is cancelled; otherwise the note shows.
function scheduleReadyNote() {
  if (readyNoteTimer) clearTimeout(readyNoteTimer);
  readyNoteTimer = setTimeout(() => {
    readyNoteTimer = null;
    if (!welcomeCard) addNote("Session ready. What would you like to benchmark?");
  }, 400);
}

// Get (or lazily create) the single start-of-chat welcome card. Lets the deterministic `welcome`
// event and the `suggestions` event compose into ONE card regardless of arrival order.
function ensureWelcomeCard() {
  if (readyNoteTimer) { clearTimeout(readyNoteTimer); readyNoteTimer = null; }  // a card wins over the note
  if (!welcomeCard) {
    welcomeCard = el("div", "welcome-card");
    activePane.appendChild(welcomeCard);
  }
  return welcomeCard;
}

// Deterministic welcome (B2): render the backend's heading + capability bullets + nudge. Built by
// code from knowledge/welcome.md, so the greeting is identical every fresh chat. Chips (if any)
// are appended below it by renderSuggestions.
function renderWelcome(data) {
  if (!data || !Array.isArray(data.bullets) || !data.bullets.length) return;
  const card = ensureWelcomeCard();
  if (card.querySelector(".welcome-intro")) return;   // already rendered (idempotent)
  const intro = el("div", "welcome-intro");
  if (data.heading) intro.appendChild(el("div", "welcome-heading", data.heading));
  const list = el("ul", "welcome-caps");
  for (const b of data.bullets) { if (b) list.appendChild(el("li", null, String(b))); }
  intro.appendChild(list);
  if (data.nudge) intro.appendChild(el("div", "welcome-nudge", String(data.nudge)));
  card.insertBefore(intro, card.firstChild);          // intro stays above the chips
  scroll();
}

function renderSuggestions(chips) {
  if (!Array.isArray(chips) || !chips.length) return;
  const card = ensureWelcomeCard();
  if (card.querySelector(".welcome-chips")) return;   // chips already rendered (idempotent)
  // A heading only when the deterministic welcome didn't already supply one (fallback path).
  if (!card.querySelector(".welcome-heading")) {
    card.appendChild(el("div", "welcome-heading",
      "Hi! I can help you run a benchmark — try one of these, or just describe your use case:"));
  }
  // Prominent entry to the guided builder, for users who'd rather click through choices than type.
  const build = el("button", "welcome-build", "✨ Design a benchmark");
  build.type = "button";
  build.onclick = openBuilder;
  card.appendChild(build);
  const wrap = el("div", "welcome-chips");
  for (const chip of chips) {
    if (!chip || !chip.label || !chip.prompt) continue;
    const btn = el("button", "chip", chip.label);
    btn.type = "button";
    btn.onclick = () => { sendUserMessage(chip.prompt); };   // sendUserMessage removes the card
    wrap.appendChild(btn);
  }
  card.appendChild(wrap);
  scroll();
}

function removeWelcomeCard() {
  if (readyNoteTimer) { clearTimeout(readyNoteTimer); readyNoteTimer = null; }
  if (welcomeCard) { welcomeCard.remove(); welcomeCard = null; }
}

// ---- split view: live resource side panel (backend-streamed during a run) -
// The live resource view is chat-ADJACENT, not inline: each `resource_stats` event opens a
// right-hand panel (the chat column narrows alongside it) and updates it in place; `done` collapses
// it back to full width. One shared panel reflects the ACTIVE chat's run; the per-chat snapshot
// (resourceData/resourceActive) is re-rendered on switch so the front chat's run is always shown.
// Zero agent/LLM cost — purely a backend-pushed view.

// A `resource_stats` event for the active chat: stash it, accumulate per-pod history (the raw
// table only shows the latest tick — we keep a rolling series for the sparklines), reopen split.
function renderResourceStats(data) {
  resourceData = data;
  resourceActive = true;
  // A new run's FIRST tick after a previous run finished (`done` sets resourceRunEnded) starts a
  // FRESH per-pod history, so the sparkline trends don't graft the prior run's pods/samples onto
  // the new run. Keyed off the `done`-set flag, NOT the resourceActive transition — a manual
  // mid-run collapse also flips resourceActive and must never wipe the running run's history.
  if (cur && cur.resourceRunEnded) { cur.resourceHistory = {}; cur.resourceRunEnded = false; }
  if (cur) { cur.resourceData = data; cur.resourceActive = true; }
  accumulateResourceHistory(data);
  renderResourceSide();
}

// kubectl-top values are unit-suffixed strings ("250m", "1"; "45Mi", "1Gi", or plain bytes).
// Normalize to millicores / MiB so the sparklines plot on a stable scale. null on unparseable.
function parseCpuMillicores(s) {
  if (s == null) return null;
  const m = String(s).trim().match(/^([\d.]+)\s*([a-z]*)$/i);
  if (!m) return null;
  const v = parseFloat(m[1]);
  if (!isFinite(v)) return null;
  return m[2].toLowerCase() === "m" ? v : v * 1000;   // bare value = whole cores
}
function parseMemMiB(s) {
  if (s == null) return null;
  const m = String(s).trim().match(/^([\d.]+)\s*([kmgtp]i?)?b?$/i);
  if (!m) return null;
  const v = parseFloat(m[1]);
  if (!isFinite(v)) return null;
  const scale = { "": 1 / (1024 * 1024), ki: 1 / 1024, mi: 1, gi: 1024, ti: 1024 * 1024,
                  k: 1e3 / (1024 * 1024), m: 1e6 / (1024 * 1024), g: 1e9 / (1024 * 1024), t: 1e12 / (1024 * 1024) };
  const u = (m[2] || "").toLowerCase();
  return v * (scale[u] != null ? scale[u] : 1);
}

// Append the latest tick's per-pod CPU/mem to the active chat's rolling history (cap 60 samples).
function accumulateResourceHistory(data) {
  if (!cur || !data || data.available === false || !Array.isArray(data.rows)) return;
  const hist = cur.resourceHistory || (cur.resourceHistory = {});
  for (const row of data.rows) {
    const name = row && row.name;
    if (!name) continue;
    const series = hist[name] || (hist[name] = []);
    series.push({ cpu: parseCpuMillicores(row["cpu(cores)"]), mem: parseMemMiB(row["memory(bytes)"]) });
    if (series.length > 60) series.shift();
  }
}

// A tiny sparkline of one numeric key ("cpu"/"mem") of a pod's sample series.
function resSpark(series, key) {
  const W = 92, H = 22, pad = 2;
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, class: "res-spark", "aria-hidden": "true" });
  const vals = series.map((s) => s[key]).filter((v) => typeof v === "number" && isFinite(v));
  if (vals.length < 2) return svg;
  const min = Math.min(...vals), max = Math.max(...vals), span = (max - min) || 1, n = vals.length;
  const x = (i) => pad + (i * (W - 2 * pad)) / (n - 1);
  const y = (v) => H - pad - ((v - min) / span) * (H - 2 * pad);
  svg.appendChild(svgEl("polyline", {
    points: vals.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" "), class: "res-spark-line" }));
  return svg;
}

// Per-pod CPU + mem trend block under the live table (reads the active chat's accumulated history).
function renderResourceTrends(body) {
  const hist = cur && cur.resourceHistory;
  if (!hist) return;
  const pods = Object.keys(hist).filter((p) => (hist[p] || []).length >= 2);
  if (!pods.length) return;
  body.appendChild(el("div", "resource-head resource-trend-head", "trend (per pod)"));
  for (const pod of pods) {
    const series = hist[pod];
    const row = el("div", "resource-trend-row");
    row.appendChild(el("div", "resource-trend-name", pod));
    const last = series[series.length - 1] || {};
    const cpu = el("div", "resource-trend-metric");
    cpu.appendChild(el("span", "resource-trend-lbl", "cpu"));
    cpu.appendChild(resSpark(series, "cpu"));
    cpu.appendChild(el("span", "resource-trend-cur", last.cpu != null ? `${fmtNum(last.cpu)}m` : "—"));
    row.appendChild(cpu);
    const mem = el("div", "resource-trend-metric");
    mem.appendChild(el("span", "resource-trend-lbl", "mem"));
    mem.appendChild(resSpark(series, "mem"));
    mem.appendChild(el("span", "resource-trend-cur", last.mem != null ? `${fmtNum(last.mem)}Mi` : "—"));
    row.appendChild(mem);
    body.appendChild(row);
  }
}

// Render the shared #resource-side panel from the active chat's snapshot and toggle the split layout.
// No snapshot (or not active) → collapse the split and hide the panel; degrades gracefully.
function renderResourceSide() {
  if (!resourceSide) return;
  if (!resourceActive || !resourceData) {
    document.body.classList.remove("split");
    resourceSide.hidden = true;
    if (resourceSideBody) resourceSideBody.innerHTML = "";
    return;
  }
  resourceSide.hidden = false;
  document.body.classList.add("split");
  const body = resourceSideBody;
  body.innerHTML = "";
  const data = resourceData;

  // Grafana slot: a button ABOVE the metrics that opens the operator's dashboard in a modal overlay
  // (replaces the old always-on inline iframe — the embed now loads lazily, only when asked). Shown
  // only when the backend supplied a valid http(s) dashboard_url (GRAFANA_DASHBOARD_URL configured);
  // run-scoped, since this panel only exists during a run. The agent's own kubectl-top view renders
  // below it. See openGrafanaModal for the X-Frame-Options "open in new tab" fallback.
  if (data.dashboard_url && /^https?:\/\//i.test(data.dashboard_url)) {
    const btn = el("button", "resource-dash-btn", "📊 Open Grafana");
    btn.type = "button";
    btn.title = "Open the live Grafana dashboard";
    btn.addEventListener("click", () => openGrafanaModal(data.dashboard_url));
    body.appendChild(btn);
  }

  if (data.available === false) {
    body.appendChild(el("div", "resource-note", data.note || "live resource stats unavailable"));
    // No actionable control here: this panel is shown ONLY during a run, and a mid-run install
    // button historically collided with the single-turn-in-flight guard (a 2nd message mid-run is
    // now STEERED into the running turn, not run as its own action — so a button still wouldn't fit).
    // Live stats need the in-cluster metrics-server, which the agent now PROACTIVELY offers to
    // install BEFORE the run — driven by a deterministic probe fact (app/tools/probe.py
    // `metrics_server`) + a HARD_RULE (app/agent/prompt.py). A passive hint is enough here.
    body.appendChild(el("div", "resource-note resource-note-hint",
      "Live CPU/memory needs the in-cluster metrics-server — the assistant offers to install it " +
      "before a run."));
    return;
  }
  const rows = data.rows || [];
  body.appendChild(el("div", "resource-head", `live resource usage${data.namespace ? " · " + data.namespace : ""}`));
  if (!rows.length) {
    body.appendChild(el("div", "resource-note", "no pods reporting yet"));
    return;
  }
  const table = el("table", "resource-table");
  const thead = el("tr");
  for (const h of ["pod", "cpu", "memory"]) thead.appendChild(el("th", null, h));
  table.appendChild(thead);
  for (const r of rows) {
    const tr = el("tr");
    tr.appendChild(el("td", "resource-name", r["name"] || ""));
    tr.appendChild(el("td", null, r["cpu(cores)"] || ""));
    tr.appendChild(el("td", null, r["memory(bytes)"] || ""));
    table.appendChild(tr);
  }
  body.appendChild(table);
  renderResourceTrends(body);
}

// On `done` (run finished): collapse the split view back to full width for the active chat.
function clearResourceStats() {
  resourceActive = false;
  if (cur) cur.resourceActive = false;
  renderResourceSide();
}

// ---- run progress stepper (workflow phase rail) --------------------------
// A slim phased rail under the header that makes the agent's long benchmark workflow legible to
// a non-expert: which phase is it in, what's done, what's still ahead. Driven ENTIRELY from the
// tool_call stream (no backend/LLM cost) — every one of the agent's tools maps to a phase below.
// State lives on the per-chat record (phaseReached = furthest phase, phaseActive = the one
// running now), so switching chats restores each chat's own progress. Monotonic within a chat:
// a re-run advances `active` but the rail keeps the furthest milestone marked done.
const RUN_PHASES = [
  { key: "preflight", label: "Pre-flight" },
  { key: "plan", label: "Plan" },
  { key: "setup", label: "Setup" },
  { key: "configure", label: "Configure" },
  { key: "deploy", label: "Deploy" },
  { key: "benchmark", label: "Benchmark" },
  { key: "analyze", label: "Analyze" },
];
// Tool name -> phase index for the progress rail. Tools NOT in this map deliberately don't move
// the rail (phaseForTool returns -1 → advancePhase no-ops): the meta / UX / alongside-any-phase
// tools — observe_run_metrics, run_shell, cancel_run, export_run_bundle,
// reproduce_run.
const TOOL_PHASE = {
  probe_environment: 0, list_catalog: 0, inspect_workload_profile: 0, estimate_run_duration: 0,
  advise_accelerators: 0, check_capacity: 0, discover_stack: 0,
  check_endpoint_readiness: 0, read_knowledge: 0, search_knowledge: 0, read_repo_doc: 0, fetch_key_docs: 0,
  propose_session_plan: 1,
  ensure_repos: 2, run_setup: 2, provision_hf_secret: 2,
  write_and_validate_config: 3, convert_guide_to_scenario: 3, generate_doe_experiment: 3,
  orchestrate_benchmark_run: 5, orchestrate_sweep: 5,
  locate_and_parse_report: 6, analyze_results: 6, compare_reports: 6, compare_harness_runs: 6,
  aggregate_runs: 6, result_history: 6,
};
// execute_llmdbenchmark spans several phases; disambiguate by its `subcommand` argument.
// teardown (-1) is cleanup — it deliberately doesn't move the rail backward.
const EXECUTE_SUBCMD_PHASE = { plan: 3, standup: 4, smoketest: 4, run: 5, experiment: 5, results: 6, teardown: -1 };

function phaseForTool(name, input) {
  if (name === "execute_llmdbenchmark") {
    const sub = input && input.subcommand;
    return (sub && sub in EXECUTE_SUBCMD_PHASE) ? EXECUTE_SUBCMD_PHASE[sub] : 4;
  }
  return (name in TOOL_PHASE) ? TOOL_PHASE[name] : -1;
}

// A tool started → mark its phase active (and the furthest reached). No-op for off-rail tools.
function advancePhase(name, input) {
  if (!cur) return;
  const i = phaseForTool(name, input);
  if (i < 0) return;
  if (i > cur.phaseReached) cur.phaseReached = i;
  cur.phaseActive = i;
  renderRunSteps();
}

// Turn ended / paused on approval / errored → nothing is actively running; drop the pulse but
// keep the furthest milestone so the rail still shows how far the chat got.
function clearPhaseActive() {
  if (cur) cur.phaseActive = -1;
  renderRunSteps();
}

// Render the shared rail from the active chat's record. Hidden until at least one phase reached.
function renderRunSteps() {
  if (!runSteps) return;
  const reached = cur ? cur.phaseReached : -1;
  const active = cur ? cur.phaseActive : -1;
  if (reached < 0) { runSteps.hidden = true; runSteps.innerHTML = ""; return; }
  runSteps.hidden = false;
  runSteps.innerHTML = "";
  RUN_PHASES.forEach((ph, i) => {
    let state = i < reached ? "done" : (i > reached ? "pending" : "done");
    if (i === active) state = "active";
    const step = el("div", "run-step " + state);
    step.setAttribute("role", "listitem");
    const dot = el("span", "run-step-dot", state === "done" ? "✓" : String(i + 1));
    step.appendChild(dot);
    step.appendChild(el("span", "run-step-label", ph.label));
    step.title = `${ph.label} — ${state}`;
    runSteps.appendChild(step);
    if (i < RUN_PHASES.length - 1) runSteps.appendChild(el("span", "run-step-sep", ""));
  });
}

// ---- deterministic structured results card (B2) --------------------------
// Emitted by the backend right after a report/analysis tool result, built from the VALIDATED
// Benchmark Report v0.2 summary + the analyzer's exact SLO/Pareto verdicts (not LLM prose), so
// the post-run summary looks the same every run. The agent's plain-language explanation still
// rides alongside it as a normal assistant bubble.
function metaRow(card, parent) {
  const meta = el("div", "results-meta");
  const add = (label, val) => { if (val != null && val !== "") meta.appendChild(el("span", "results-tag", `${label}: ${val}`)); };
  add("model", card.model);
  add("harness", card.harness);
  if (card.requests_total != null) add("requests", fmtNum(card.requests_total));
  if (card.success_rate_pct != null) add("success", fmtNum(card.success_rate_pct) + "%");
  if (card.duration != null) add("duration", card.duration);
  if (card.simulated) add("note", "SIMULATED");
  if (meta.childNodes.length) parent.appendChild(meta);
}

// ---- results visualizations (SVG; built from data already on the wire) ---
// Small dependency-free SVG widgets. Each takes already-validated numbers and returns an
// <svg>; no layout libs, no network. Shared by the results card (gauge) and the prominent
// analysis/comparison cards rendered from tool_results (scatter, delta bars).
const SVG_NS = "http://www.w3.org/2000/svg";
function svgEl(name, attrs) {
  const e = document.createElementNS(SVG_NS, name);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  return e;
}

// Radial (semicircle) goodput gauge: a colored arc from 0→pct over a muted track, % in the
// center. Arc drawn as a polyline of points (no arc-flag ambiguity); color steps by threshold.
function goodputGauge(pct) {
  pct = Math.max(0, Math.min(100, Number(pct) || 0));
  const W = 132, H = 78, cx = W / 2, cy = H - 12, r = 52;
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, class: "gauge", role: "img",
    "aria-label": `estimated goodput ${Math.round(pct)} percent` });
  // 180° (left) → 0° (right) over the top; y = cy - r·sinθ puts the apex up.
  const arc = (fromDeg, toDeg) => {
    const steps = 48, pts = [];
    for (let i = 0; i <= steps; i++) {
      const d = (fromDeg + (toDeg - fromDeg) * (i / steps)) * Math.PI / 180;
      pts.push(`${(cx + r * Math.cos(d)).toFixed(1)},${(cy - r * Math.sin(d)).toFixed(1)}`);
    }
    return pts.join(" ");
  };
  svg.appendChild(svgEl("polyline", { points: arc(180, 0), class: "gauge-track" }));
  const cls = pct >= 90 ? "good" : pct >= 70 ? "mid" : "low";
  svg.appendChild(svgEl("polyline", { points: arc(180, 180 - (pct / 100) * 180), class: "gauge-val " + cls }));
  const t = svgEl("text", { x: cx, y: cy - 6, "text-anchor": "middle", class: "gauge-text" });
  t.textContent = `${fmtNum(pct)}%`;
  svg.appendChild(t);
  return svg;
}

// An objective's axis caption: name + an arrow toward "better" + units, e.g. "throughput ↑ (tok/s)".
function objAxisLabel(m) {
  const arrow = m.direction === "higher" ? " ↑" : m.direction === "lower" ? " ↓" : "";
  return `${m.name}${arrow}${m.units ? ` (${m.units})` : ""}`;
}

// 2D Pareto scatter: each config is a point in (objective-x, objective-y) space; frontier points
// are accent-filled and joined by a line (the trade-off curve), dominated points muted, and
// SLO-infeasible points ringed. Linear axes auto-scaled with a small margin; foolproof (no libs).
function scatterPlot(points, xMeta, yMeta) {
  const W = 460, H = 300, padL = 56, padR = 18, padT = 16, padB = 48;
  const span = (vals) => {
    let lo = Math.min(...vals), hi = Math.max(...vals);
    if (lo === hi) { const d = Math.abs(lo) || 1; return [lo - d * 0.5, hi + d * 0.5]; }
    const m = (hi - lo) * 0.08; return [lo - m, hi + m];
  };
  const [xmin, xmax] = span(points.map((p) => p.x));
  const [ymin, ymax] = span(points.map((p) => p.y));
  const sx = (v) => padL + ((v - xmin) / (xmax - xmin)) * (W - padL - padR);
  const sy = (v) => (H - padB) - ((v - ymin) / (ymax - ymin)) * (H - padT - padB);
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, class: "scatter", role: "img",
    "aria-label": `Pareto scatter of ${xMeta.name} versus ${yMeta.name}` });
  svg.appendChild(svgEl("line", { x1: padL, y1: padT, x2: padL, y2: H - padB, class: "scatter-axis" }));
  svg.appendChild(svgEl("line", { x1: padL, y1: H - padB, x2: W - padR, y2: H - padB, class: "scatter-axis" }));
  const xlab = svgEl("text", { x: (padL + W - padR) / 2, y: H - 12, "text-anchor": "middle", class: "scatter-axlabel" });
  xlab.textContent = objAxisLabel(xMeta); svg.appendChild(xlab);
  const ymid = (padT + H - padB) / 2;
  const ylab = svgEl("text", { x: 15, y: ymid, "text-anchor": "middle", class: "scatter-axlabel",
    transform: `rotate(-90 15 ${ymid})` });
  ylab.textContent = objAxisLabel(yMeta); svg.appendChild(ylab);
  // Frontier trade-off line (sorted along x).
  const fr = points.filter((p) => p.frontier).slice().sort((a, b) => a.x - b.x);
  if (fr.length >= 2) {
    svg.appendChild(svgEl("polyline", {
      points: fr.map((p) => `${sx(p.x).toFixed(1)},${sy(p.y).toFixed(1)}`).join(" "), class: "scatter-frontier" }));
  }
  for (const p of points) {
    const cls = "scatter-pt" + (p.frontier ? " frontier" : "") + (p.feasible === false ? " infeasible" : "");
    const c = svgEl("circle", { cx: sx(p.x).toFixed(1), cy: sy(p.y).toFixed(1), r: p.frontier ? 5.5 : 4, class: cls });
    const title = svgEl("title", {});
    title.textContent = `${p.label}: ${xMeta.name} ${fmtNum(p.x)}, ${yMeta.name} ${fmtNum(p.y)}`;
    c.appendChild(title);
    svg.appendChild(c);
    const tx = svgEl("text", { x: (sx(p.x) + 7).toFixed(1), y: (sy(p.y) + 3).toFixed(1), class: "scatter-ptlabel" });
    tx.textContent = p.label;
    svg.appendChild(tx);
  }
  return svg;
}

// A small legend swatch+label for the scatter.
function legendItem(cls, label) {
  const item = el("span", "scatter-legend-item");
  item.appendChild(el("span", "scatter-legend-dot " + cls));
  item.appendChild(el("span", null, label));
  return item;
}

// A diverging mini-bar for a signed delta% vs a baseline, colored by whether it's an improvement
// (direction-aware: for a "lower is better" metric a negative delta is good). Fill grows from the
// center: left for a decrease, right for an increase. Returns [bar, value-label] in a wrapper.
function deltaBar(deltaPct, direction) {
  const improved = direction === "lower" ? deltaPct < 0 : direction === "higher" ? deltaPct > 0 : null;
  const tone = deltaPct === 0 ? "flat" : improved === true ? "good" : improved === false ? "bad" : "neutral";
  const wrap = el("div", "delta");
  const bar = el("div", "delta-bar");
  const fill = el("div", "delta-fill " + (deltaPct < 0 ? "neg " : "pos ") + tone);
  fill.style.width = (Math.min(100, Math.abs(deltaPct)) / 2).toFixed(1) + "%";
  bar.appendChild(fill);
  wrap.appendChild(bar);
  const sign = deltaPct > 0 ? "+" : "";
  wrap.appendChild(el("span", "delta-val " + tone, `${sign}${fmtNum(deltaPct)}%`));
  return wrap;
}

// Serialize a results card to a markdown summary (paste into a report / Slack / PR). Built from
// the card DATA, not the DOM, so it stays exact and stable.
function resultsCardMarkdown(card) {
  const lines = [`## ${card.kind === "sweep" ? "Sweep results" : "Benchmark results"}`];
  const meta = [];
  if (card.model) meta.push(`model: ${card.model}`);
  if (card.harness) meta.push(`harness: ${card.harness}`);
  if (card.requests_total != null) meta.push(`requests: ${fmtNum(card.requests_total)}`);
  if (card.success_rate_pct != null) meta.push(`success: ${fmtNum(card.success_rate_pct)}%`);
  if (card.duration != null) meta.push(`duration: ${card.duration}`);
  if (meta.length) lines.push("", meta.join(" · "));
  const metrics = Array.isArray(card.metrics) ? card.metrics : [];
  if (metrics.length) {
    lines.push("", "| metric | value | stat |", "|---|---|---|");
    for (const m of metrics) lines.push(`| ${m.label} | ${fmtNum(m.value)}${m.units ? " " + m.units : ""} | ${m.stat || ""} |`);
  }
  const slo = card.slo;
  if (slo && Array.isArray(slo.verdicts) && slo.verdicts.length) {
    lines.push("", `### SLO check${slo.overall_met != null ? (slo.overall_met ? " — all met ✓" : " — not all met ✗") : ""}`,
      "", "| metric | target | observed | met |", "|---|---|---|---|");
    for (const v of slo.verdicts) {
      const u = v.units ? " " + v.units : "";
      const dir = v.direction === "min" ? "≥ " : v.direction === "max" ? "≤ " : "";
      lines.push(`| ${v.metric || ""}${v.statistic ? " (" + v.statistic + ")" : ""} | ${v.target != null ? dir + fmtNum(v.target) + u : "—"} | ${v.observed != null ? fmtNum(v.observed) + u : "—"} | ${v.met === true ? "✓" : v.met === false ? "✗" : "n/a"} |`);
    }
    if (slo.goodput && slo.goodput.estimate_pct != null) lines.push("", `Estimated goodput: ~${fmtNum(slo.goodput.estimate_pct)}%`);
  }
  return lines.join("\n");
}

// Add a hover-reveal "Copy" button to a card that copies `text` to the clipboard.
function addCardCopy(root, text) {
  const btn = el("button", "card-copy", "Copy");
  btn.type = "button";
  btn.title = "Copy a markdown summary";
  btn.setAttribute("aria-label", "Copy a markdown summary");
  btn.addEventListener("click", () => copyText(text, btn));
  root.appendChild(btn);
}

function renderResultsCard(card) {
  if (!card || typeof card !== "object") return;
  removeWelcomeCard();
  const root = el("div", "results-card");
  addCardCopy(root, resultsCardMarkdown(card));
  const isBench = card.kind !== "sweep";
  const headRow = el("div", "results-head-row");
  headRow.appendChild(el("div", "results-head", isBench ? "Benchmark report" : "Sweep results"));
  // The card only exists because a Benchmark Report was parsed against its schema and validated.
  if (isBench) headRow.appendChild(el("span", "results-schema", `${card.schema_version || "v0.2"} · validated`));
  root.appendChild(headRow);
  metaRow(card, root);

  // Single-run metric table (from the validated report summary).
  const metrics = Array.isArray(card.metrics) ? card.metrics : [];
  if (metrics.length) {
    const table = el("table", "results-table");
    const head = el("tr");
    for (const h of ["metric", "value", "stat"]) head.appendChild(el("th", null, h));
    table.appendChild(head);
    for (const m of metrics) {
      const tr = el("tr");
      const nameTd = el("td", "results-name", m.label || "");
      tr.appendChild(nameTd);
      tr.appendChild(el("td", null, `${fmtNum(m.value)}${m.units ? " " + m.units : ""}`));
      tr.appendChild(el("td", "results-stat", m.stat || ""));
      tr.title = m.direction || "";
      table.appendChild(tr);
    }
    root.appendChild(table);
  }

  // SLO verdicts (from analyze_results) — exact, deterministic pass/fail per metric.
  const slo = card.slo;
  if (slo && Array.isArray(slo.verdicts) && slo.verdicts.length) {
    root.appendChild(el("div", "results-subhead",
      "SLO check" + (slo.overall_met != null ? (slo.overall_met ? " — all targets met ✓" : " — not all targets met ✗") : "")));
    // Goodput gauge — the proposal's key differentiator. A radial gauge of the estimated
    // fraction of requests meeting ALL SLOs, beside the binding constraint (first missed target).
    if (slo.goodput && slo.goodput.estimate_pct != null) {
      const gp = el("div", "results-goodput");
      gp.appendChild(goodputGauge(slo.goodput.estimate_pct));
      const note = el("div", "results-goodput-note");
      note.appendChild(el("div", "results-goodput-label", "Estimated goodput"));
      const missed = slo.verdicts.find((v) => v.met === false);
      if (missed) note.appendChild(el("div", "results-goodput-bind",
        `Limited by ${missed.metric || "an SLO target"}${missed.statistic ? " (" + missed.statistic + ")" : ""}`));
      else note.appendChild(el("div", "results-goodput-bind good", "All SLO targets met"));
      note.appendChild(el("div", "results-goodput-sub", "upper-bound estimate from reported percentiles"));
      gp.appendChild(note);
      root.appendChild(gp);
    }
    const table = el("table", "results-table");
    const head = el("tr");
    for (const h of ["metric", "target", "observed", "verdict"]) head.appendChild(el("th", null, h));
    table.appendChild(head);
    for (const v of slo.verdicts) {
      const tr = el("tr");
      const u = v.units ? " " + v.units : "";
      const dir = v.direction === "min" ? "≥ " : v.direction === "max" ? "≤ " : "";
      tr.appendChild(el("td", "results-name", `${v.metric || ""}${v.statistic ? " (" + v.statistic + ")" : ""}`));
      tr.appendChild(el("td", null, v.target != null ? dir + fmtNum(v.target) + u : "—"));
      tr.appendChild(el("td", null, v.observed != null ? fmtNum(v.observed) + u : "—"));
      tr.appendChild(el("td", v.met === true ? "slo-pass" : v.met === false ? "slo-fail" : "slo-na",
        v.met === true ? "✓ met" : v.met === false ? "✗ missed" : "n/a"));
      table.appendChild(tr);
    }
    root.appendChild(table);
  }

  // Sweep: per-run rows + the Pareto frontier (facts only — the agent picks the winner).
  if (card.kind === "sweep" && Array.isArray(card.runs) && card.runs.length) {
    const table = el("table", "results-table");
    const head = el("tr");
    for (const h of ["run", "model", "on frontier", "slo"]) head.appendChild(el("th", null, h));
    table.appendChild(head);
    const frontier = new Set(card.frontier || []);
    for (const r of card.runs) {
      const tr = el("tr");
      tr.appendChild(el("td", "results-name", r.label || ""));
      tr.appendChild(el("td", null, r.model || ""));
      tr.appendChild(el("td", null, frontier.has(r.label) ? "★" : ""));
      tr.appendChild(el("td", r.slo_met === true ? "slo-pass" : r.slo_met === false ? "slo-fail" : "slo-na",
        r.slo_met === true ? "✓" : r.slo_met === false ? "✗" : ""));
      table.appendChild(tr);
    }
    root.appendChild(table);
    if (card.objectives && card.objectives.length) {
      root.appendChild(el("div", "results-note", "Compared on: " + card.objectives.join(", ") + ". ★ = Pareto-optimal."));
    }
  }

  activePane.appendChild(root);
  scroll();
}

// ---- Pareto frontier scatter (from an analyze_results sweep tool_result) --
// Renders the sweep's configurations in objective space with the Pareto frontier highlighted —
// the proposal's "Pareto-optimal configurations" stretch goal, made visual. No-ops unless the
// result is a sweep with >=2 objectives present in >=2 runs (the analyzer already filters to
// comparable objectives, so we just take the first two and plot the runs that carry both).
function renderParetoCard(result) {
  if (!result || !result.analyzed) return;
  const pareto = result.pareto;
  if (!pareto || !Array.isArray(pareto.objectives) || pareto.objectives.length < 2) return;
  if (!Array.isArray(pareto.runs) || pareto.runs.length < 2) return;
  const xMeta = pareto.objectives[0], yMeta = pareto.objectives[1];
  if (!xMeta || !yMeta || !xMeta.name || !yMeta.name) return;
  // SLO feasibility per run comes from the top-level analyze runs (pareto rows don't carry it).
  const feasible = {};
  for (const run of (result.runs || [])) {
    if (run && run.label != null) feasible[run.label] = (run.slo || {}).overall_met;
  }
  const points = [];
  for (const run of pareto.runs) {
    const o = run.objectives || {};
    const x = o[xMeta.name], y = o[yMeta.name];
    if (typeof x !== "number" || typeof y !== "number") continue;
    points.push({ label: run.label || "", x, y, frontier: !!run.on_frontier, feasible: feasible[run.label] });
  }
  if (points.length < 2) return;
  removeWelcomeCard();
  const root = el("div", "results-card");
  root.appendChild(el("div", "results-head", "Pareto frontier — best trade-offs"));
  root.appendChild(el("div", "report-sub",
    `${points.length} configurations · ★ frontier = not dominated on any objective`));
  root.appendChild(scatterPlot(points, xMeta, yMeta));
  const legend = el("div", "scatter-legend");
  legend.appendChild(legendItem("frontier", "on frontier"));
  legend.appendChild(legendItem("dominated", "dominated"));
  if (Object.values(feasible).some((v) => v === false)) legend.appendChild(legendItem("infeasible", "misses SLO"));
  root.appendChild(legend);
  activePane.appendChild(root);
  scroll();
}

// ---- A/B comparison delta bars (from a compare_reports tool_result) -------
// compare_reports emits no results_card event, so its rich per-metric deltas vs a baseline were
// only ever visible as raw JSON. Render them as a table of direction-aware diverging bars: green
// when a run beats the baseline on that metric, red when it regresses.
function renderComparisonCard(result) {
  if (!result || !result.compared) return;
  const cmp = result.comparison;
  if (!cmp || !Array.isArray(cmp.metrics) || !cmp.metrics.length) return;
  const labels = Array.isArray(cmp.labels) ? cmp.labels : [];
  const baseline = cmp.baseline;
  const others = labels.filter((l) => l !== baseline);
  if (!others.length) return;
  removeWelcomeCard();
  const root = el("div", "results-card");
  root.appendChild(el("div", "results-head", "A/B comparison"));
  root.appendChild(el("div", "report-sub", `baseline: ${baseline} · Δ vs baseline — green better, red worse`));
  const table = el("table", "results-table compare-table");
  const head = el("tr");
  head.appendChild(el("th", null, "metric"));
  head.appendChild(el("th", null, `${baseline} (base)`));
  for (const o of others) head.appendChild(el("th", null, o));
  table.appendChild(head);
  for (const m of cmp.metrics) {
    const tr = el("tr");
    const u = m.units ? " " + m.units : "";
    tr.appendChild(el("td", "results-name", `${m.name}${m.stat && m.stat !== "value" ? " (" + m.stat + ")" : ""}`));
    tr.appendChild(el("td", null, m.baseline_value != null ? fmtNum(m.baseline_value) + u : "—"));
    const byLabel = {};
    for (const pr of (m.per_run || [])) byLabel[pr.label] = pr;
    for (const o of others) {
      const pr = byLabel[o];
      const td = el("td", "compare-cell");
      if (pr && pr.delta_pct != null) td.appendChild(deltaBar(pr.delta_pct, m.direction));
      else if (pr && pr.value != null) td.appendChild(document.createTextNode(fmtNum(pr.value) + u));
      else td.appendChild(document.createTextNode("—"));
      tr.appendChild(td);
    }
    table.appendChild(tr);
  }
  root.appendChild(table);
  if (cmp.headline) root.appendChild(el("div", "results-note", cmp.headline));
  activePane.appendChild(root);
  scroll();
}

// ---- cross-harness comparison table (from compare_harness_runs) -----------
// Different load generators measure differently, so the analyzer picks NO winner — we lay the
// shared metrics side by side per harness as facts, with that caveat stated.
function renderHarnessCompareCard(result) {
  if (!result || !result.compared) return;
  const cross = result.cross;
  if (!cross || !Array.isArray(cross.cross_metrics) || !cross.cross_metrics.length) return;
  const harnesses = Array.isArray(cross.harness_names) ? cross.harness_names : [];
  if (harnesses.length < 2) return;
  removeWelcomeCard();
  const root = el("div", "results-card");
  root.appendChild(el("div", "results-head", "Harness comparison"));
  root.appendChild(el("div", "report-sub",
    `${harnesses.join(" vs ")} · different load generators — values aren't directly comparable`));
  const table = el("table", "results-table");
  const head = el("tr");
  head.appendChild(el("th", null, "metric"));
  for (const h of harnesses) head.appendChild(el("th", null, h));
  table.appendChild(head);
  for (const m of cross.cross_metrics) {
    const tr = el("tr");
    tr.appendChild(el("td", "results-name", m.name || m.key || ""));
    const byH = {};
    for (const ph of (m.per_harness || [])) byH[ph.harness] = ph;
    for (const h of harnesses) {
      const ph = byH[h];
      tr.appendChild(el("td", null, ph && ph.value != null ? `${fmtNum(ph.value)}${ph.units ? " " + ph.units : ""}` : "—"));
    }
    table.appendChild(tr);
  }
  root.appendChild(table);
  activePane.appendChild(root);
  scroll();
}

// ---- the agent's "what next?" suggestion buttons (from suggest_next_steps) -
// Whenever the agent would offer next steps in prose ("want me to save this as a baseline?"), it
// instead CALLS suggest_next_steps with {label, prompt} options. We draw them as the SAME floating
// pills as the welcome chips (plain `.chip`, no arrow) under the agent's reply; clicking one sends
// its `prompt` as the user's next message — so a non-expert advances with one tap. Rendered from the
// tool RESULT, so it shows live AND replays on resume/reload (suggest_next_steps is a card tool).
function renderAgentSuggestions(r) {
  if (!r || !Array.isArray(r.suggestions) || !r.suggestions.length) return;
  const row = el("div", "next-steps");
  row.appendChild(el("div", "next-steps-label", "Suggested next steps"));
  const chips = el("div", "next-steps-chips");
  // The agent chooses how many to offer; defensively cap at the schema max (6) just in case.
  for (const s of r.suggestions.slice(0, 6)) {
    if (!s || !s.label || !s.prompt) continue;
    const btn = el("button", "chip", s.label);   // plain `.chip` → identical to the welcome chips
    btn.type = "button";
    btn.title = s.prompt;                          // hover shows the full message that will be sent
    btn.onclick = () => sendUserMessage(s.prompt);
    chips.appendChild(btn);
  }
  if (!chips.childNodes.length) return;
  row.appendChild(chips);
  activePane.appendChild(row);
  scroll();
}

// ---- pre-flight / status cards (from read-only diagnostic tool_results) ---
// The data-rich read-only tools (probe / capacity / readiness / accelerators / DoE / orchestrate)
// emit no results_card event, so without these their result would never surface in a friendly form.
// These render them as friendly status cards — exactly the "is my setup ready?" signal a
// non-expert needs. Each no-ops on a shape it can't draw (the panel then just shows the call's args).

// A small coloured status dot (state ∈ ok|warn|bad|na).
function statusDot(state) { return el("span", "status-dot status-dot-" + (state || "na")); }
// A status-grid cell: dot + label + detail.
function statusCell(state, label, detail) {
  const c = el("div", "status-cell");
  c.appendChild(statusDot(state));
  const t = el("div", "status-cell-txt");
  t.appendChild(el("div", "status-cell-label", label));
  if (detail != null) t.appendChild(el("div", "status-cell-detail", String(detail)));
  c.appendChild(t);
  return c;
}

// probe_environment → a compact at-a-glance status grid of whatever checks ran.
function renderEnvStatus(r) {
  if (!r || typeof r !== "object") return;
  const items = [];
  const cr = r.container_runtime;
  if (cr) items.push([cr.daemon_up ? "ok" : cr.present ? "warn" : "bad", (cr.type || "container") + " runtime",
    cr.daemon_up ? "running" : cr.present ? "daemon down" : "not found"]);
  if (r.tools && typeof r.tools === "object") {
    for (const [name, ok] of Object.entries(r.tools)) items.push([ok ? "ok" : "bad", name, ok ? "present" : "missing"]);
  }
  if (r.venv) items.push([r.venv.exists ? "ok" : "warn", "venv", r.venv.exists ? "ready" : "not built"]);
  if (r.repos && typeof r.repos === "object") {
    const names = Object.keys(r.repos);
    const present = names.filter((n) => r.repos[n] && r.repos[n].present).length;
    if (names.length) items.push([present === names.length ? "ok" : present ? "warn" : "bad", "repos", `${present}/${names.length} present`]);
  }
  if (r.kube_context) items.push([r.kube_context.available ? "ok" : "na", "kube context", r.kube_context.context || "none"]);
  if (r.cluster_info) items.push([r.cluster_info.reachable ? "ok" : "bad", "cluster",
    r.cluster_info.reachable ? "reachable" : (r.cluster_info.timed_out ? "timed out" : "unreachable")]);
  if (r.kind_clusters) {
    const cs = r.kind_clusters.clusters || [];
    items.push([cs.length ? "ok" : "na", "kind clusters", cs.length ? cs.join(", ") : "none"]);
  }
  if (r.namespaces && r.namespaces.available) items.push(["ok", "namespaces", String((r.namespaces.namespaces || []).length)]);
  if (r.stack && r.stack.checked) items.push([r.stack.exists ? "ok" : "na", "llm-d stack",
    r.stack.exists ? `${r.stack.ready_count || 0}/${r.stack.pod_count || 0} pods ready` : "not deployed"]);
  if (!items.length) return;
  removeWelcomeCard();
  const root = el("div", "results-card status-card");
  root.appendChild(el("div", "results-head", "Environment status"));
  const grid = el("div", "status-grid");
  for (const [state, label, detail] of items) grid.appendChild(statusCell(state, label, detail));
  root.appendChild(grid);
  activePane.appendChild(root);
  scroll();
}

// check_capacity → feasibility verdict + the planner's error/warning diagnostics.
function renderCapacityCard(r) {
  if (!r || r.ran !== true) return;
  removeWelcomeCard();
  const root = el("div", "results-card");
  const feasible = r.feasible;
  root.appendChild(el("div", "results-head",
    "Capacity pre-flight" + (feasible === true ? " — feasible ✓" : feasible === false ? " — not feasible ✗" : "")));
  const sub = [];
  if (r.spec) sub.push(r.spec);
  if (r.gated === true) sub.push(r.authorized ? "gated model · authorized ✓" : "gated model · not authorized ✗");
  if (sub.length) root.appendChild(el("div", "report-sub", sub.join(" · ")));
  const addList = (title, arr, cls) => {
    if (!Array.isArray(arr) || !arr.length) return;
    root.appendChild(el("div", "results-subhead", title));
    const ul = el("ul", "diag-list " + cls);
    for (const line of arr.slice(0, 12)) ul.appendChild(el("li", null, String(line)));
    root.appendChild(ul);
  };
  addList("Blocking issues", r.errors, "diag-bad");
  addList("Warnings", r.warnings, "diag-warn");
  if (feasible === true && !(r.errors || []).length && !(r.warnings || []).length) {
    root.appendChild(el("div", "results-note", "No blocking issues found — the configuration fits."));
  }
  activePane.appendChild(root);
  scroll();
}

// check_endpoint_readiness → a status grid (services/gateway/serving pods + health probes).
function renderReadinessCard(r) {
  if (!r || typeof r !== "object" || typeof r.ready !== "boolean") return;
  removeWelcomeCard();
  const root = el("div", "results-card");
  root.appendChild(el("div", "results-head", "Endpoint readiness — " + (r.ready ? "ready ✓" : "not ready ✗")));
  const sub = [r.namespace, r.detail].filter(Boolean).join(" · ");
  if (sub) root.appendChild(el("div", "report-sub", sub));
  const ready = r.ready_endpoints || [], notReady = r.not_ready_endpoints || [];
  const grid = el("div", "status-grid");
  if (ready.length || notReady.length) {
    grid.appendChild(statusCell(ready.length ? "ok" : "na", "ready services", String(ready.length)));
    grid.appendChild(statusCell(notReady.length ? "bad" : "ok", "not ready", String(notReady.length)));
  }
  const g = r.gateway;
  if (g) grid.appendChild(statusCell(g.control_plane_ready ? "ok" : "warn", "gateway",
    g.control_plane_ready ? "programmed" : (g.not_ready_reason || "not ready")));
  const sr = r.serving_readiness;
  if (sr && Array.isArray(sr.pods)) {
    const readyPods = sr.pods.filter((p) => p.ready_condition === "True").length;
    grid.appendChild(statusCell(readyPods === sr.pods.length ? "ok" : "warn", "serving pods", `${readyPods}/${sr.pods.length} ready`));
    if (sr.health_reachable != null) grid.appendChild(statusCell(sr.health_reachable ? "ok" : "bad", "/health", sr.health_reachable ? "reachable" : "unreachable"));
    if (sr.models_reachable != null) grid.appendChild(statusCell(sr.models_reachable ? "ok" : "bad", "/v1/models", sr.models_reachable ? "reachable" : "unreachable"));
  }
  if (grid.childNodes.length) root.appendChild(grid);
  if (notReady.length) {
    root.appendChild(el("div", "results-subhead", "Not-ready services"));
    const ul = el("ul", "diag-list diag-warn");
    for (const e of notReady) ul.appendChild(el("li", null, `${e.service}: ${e.ready_addresses || 0} ready / ${e.not_ready_addresses || 0} not ready`));
    root.appendChild(ul);
  }
  activePane.appendChild(root);
  scroll();
}

// advise_accelerators → CPU-only vs accelerated verdict + a per-node table.
function renderAcceleratorCard(r) {
  if (!r || r.available !== true) return;
  removeWelcomeCard();
  const root = el("div", "results-card");
  root.appendChild(el("div", "results-head", "Accelerators — " + (r.any_accelerator === true ? "available ✓" : "CPU-only")));
  const res = r.advertised_resources || [];
  if (res.length) root.appendChild(el("div", "report-sub", "advertised: " + res.join(", ")));
  const nodes = r.nodes || [];
  if (nodes.length) {
    const table = el("table", "results-table");
    const head = el("tr");
    for (const h of ["node", "accelerators", "cpu"]) head.appendChild(el("th", null, h));
    table.appendChild(head);
    for (const n of nodes) {
      const tr = el("tr");
      tr.appendChild(el("td", "results-name", n.name || ""));
      const accs = n.accelerators
        ? Object.entries(n.accelerators).filter(([, v]) => v != null).map(([k, v]) => `${k}: ${v}`).join(", ")
        : "";
      tr.appendChild(el("td", null, accs || (n.cpu_only ? "none (CPU-only)" : "—")));
      const cap = n.allocatable || n.capacity || {};
      tr.appendChild(el("td", null, cap.cpu != null ? String(cap.cpu) : "—"));
      table.appendChild(tr);
    }
    root.appendChild(table);
  }
  activePane.appendChild(root);
  scroll();
}

// generate_doe_experiment → the treatment matrix (setup × run) as tables.
function renderDoeCard(r) {
  if (!r || r.generated !== true) return;
  removeWelcomeCard();
  const root = el("div", "results-card");
  const total = r.total_matrix || 0;
  root.appendChild(el("div", "results-head", `DoE experiment — ${total} treatment${total === 1 ? "" : "s"}`));
  const sub = [];
  if (r.experiment_name) sub.push(r.experiment_name);
  sub.push(`${r.n_setup_treatments || 0} setup × ${r.n_run_treatments || 0} run`);
  root.appendChild(el("div", "report-sub", sub.join(" · ")));
  const renderTreatments = (title, arr) => {
    if (!Array.isArray(arr) || !arr.length) return;
    const keys = [];
    for (const t of arr) for (const k of Object.keys(t)) if (k !== "name" && !keys.includes(k)) keys.push(k);
    root.appendChild(el("div", "results-subhead", title));
    const table = el("table", "results-table");
    const head = el("tr");
    head.appendChild(el("th", null, "name"));
    for (const k of keys) head.appendChild(el("th", null, k));
    table.appendChild(head);
    for (const t of arr) {
      const tr = el("tr");
      tr.appendChild(el("td", "results-name", t.name || ""));
      for (const k of keys) tr.appendChild(el("td", null, t[k] != null ? String(t[k]) : "—"));
      table.appendChild(tr);
    }
    root.appendChild(table);
  };
  renderTreatments("Setup treatments", r.setup_treatments);
  renderTreatments("Run treatments", r.run_treatments);
  activePane.appendChild(root);
  scroll();
}

// orchestrate_benchmark_run → outcome + the per-attempt timeline with fault classification.
function renderOrchestrateCard(r) {
  if (!r || typeof r !== "object") return;
  if (r.submitted === false && r.ready === false) {
    removeWelcomeCard();
    const root = el("div", "results-card");
    root.appendChild(el("div", "results-head", "Run not submitted — endpoint not ready"));
    if (r.note) root.appendChild(el("div", "results-note", String(r.note)));
    activePane.appendChild(root);
    scroll();
    return;
  }
  if (typeof r.succeeded !== "boolean") return;   // unwatched submit / other shape
  removeWelcomeCard();
  const root = el("div", "results-card");
  const dead = r.dead_lettered === true;
  root.appendChild(el("div", "results-head",
    "Orchestrated run — " + (r.succeeded ? "succeeded ✓" : dead ? "dead-lettered ✗" : "failed ✗")));
  const sub = [r.namespace, r.run_id].filter(Boolean).join(" · ");
  if (sub) root.appendChild(el("div", "report-sub", sub));
  const attempts = r.attempts || [];
  if (attempts.length) {
    root.appendChild(el("div", "results-subhead", `${attempts.length} attempt${attempts.length === 1 ? "" : "s"}`));
    const table = el("table", "results-table");
    const head = el("tr");
    for (const h of ["#", "phase", "reason", "fault"]) head.appendChild(el("th", null, h));
    table.appendChild(head);
    attempts.forEach((a, i) => {
      const tr = el("tr");
      const ph = a.phase || "";
      tr.appendChild(el("td", "results-name", String(i + 1)));
      tr.appendChild(el("td", ph === "succeeded" ? "slo-pass" : ph === "failed" ? "slo-fail" : "slo-na", ph));
      tr.appendChild(el("td", null, a.reason || ""));
      tr.appendChild(el("td", null, a.failure && a.failure.kind ? a.failure.kind : ""));
      table.appendChild(tr);
    });
    root.appendChild(table);
  }
  if (r.final_failure && r.final_failure.kind) {
    root.appendChild(el("div", "results-subhead", "Final fault: " + r.final_failure.kind));
    if (r.final_failure.message) root.appendChild(el("div", "results-note", String(r.final_failure.message)));
  }
  activePane.appendChild(root);
  scroll();
}

function renderHistory(items) {
  for (const it of items) {
    if (it.role === "user") addBubble("user", it.text);
    else if (it.role === "assistant") addBubble("assistant", it.text);
    // Rebuild the run-progress stepper from the replayed tool calls, exactly as the live
    // tool_call stream does (advancePhase). Without this, a full-history restore — pane evicted,
    // page reload, or the resume cursor fell past the live buffer — leaves the rail blank even
    // though the chat clearly reached a phase: clearActivePane() reset phaseReached to -1 just
    // before this replay, and only advancePhase re-derives it. (Cache-hit switches keep the rail
    // because the record's phaseReached survives; this is the missing other half.)
    // suggest_next_steps is a UI-only tool (no technical action row / phase) — mirror the live
    // path: skip its row so only the chips replay (rendered from its tool_result below).
    else if (it.role === "tool_call" && it.name === "suggest_next_steps") { /* no action row */ }
    else if (it.role === "tool_call") { addHistoryTool(it); advancePhase(it.name, it.input); }
    // A persisted card-rendering tool result, interleaved by the server right after its tool_call:
    // re-draw the report summary + clickable charts (and other rich cards) exactly as the live
    // `tool_result` event does, so they SURVIVE a chat switch / reload — not just the live run.
    else if (it.role === "tool_result") { renderToolResultCards(it); }
    // The deterministic analyzer card, re-derived server-side from the same result and emitted
    // right after its tool_result — mirrors the live `results_card` event.
    else if (it.role === "results_card") renderResultsCard(it.card);
    // Executed commands are interleaved into `items` by the server in their original transcript
    // position (right after the tool call that ran them), so they restore inline in the chat —
    // hidden until debug view is on, exactly like a live run (see addInlineCommand).
    else if (it.role === "command") addInlineCommand(it);
    else if (it.role === "approval_decision") addDecisionCard(it);
    // A still-PENDING gate the turn is parked on (persisted in-flight): restore it as a LIVE,
    // clickable card in its transcript position. Registering it in cur.pendingApprovals lets the
    // server's subsequent reemit_pending be de-duped (addApprovalCard skips a known request_id),
    // so it survives a chat switch / pane eviction without double-rendering.
    else if (it.role === "approval_request") addApprovalCard(it);
  }
  // The replay above re-lit the furthest phase AND left "active" on the last tool's phase. This is
  // a restore of PAST events, so drop the pulse unless the server says this chat is still running
  // (then the live tail re-lights the current phase) — mirrors done/error/approval, which clear the
  // active pulse but keep the furthest milestone marked done.
  if (!cur || !cur.running) clearPhaseActive();
  // Now that the restored transcript is in place, drop the deferred "catching up to live" note at
  // the BOTTOM (set in `ready` on a full rebuild of a still-running chat). It marks the seam before
  // the live tail replay that follows — not stranded at the top above the rebuilt history.
  if (cur && cur.pendingResumeNote) {
    cur.pendingResumeNote = false;
    addNote("⏳ Picking up a benchmark already running in this chat — catching up to live…");
  }
  scroll();
}

function addHistoryTool(it) {
  const d = el("details", "tool");
  const sum = el("summary");
  sum.appendChild(el("span", "tname", it.name || "tool"));
  // Backend-derived mode + the PERSISTED run duration (seconds), so a replayed/reloaded action row
  // shows the same time badge a live run does — not just the badge.
  sum.appendChild(toolMetaSpan(!!it.mutating, fmtDurShort(typeof it.duration_s === "number" ? it.duration_s : null)));
  d.appendChild(sum);
  const body = el("div", "body");
  if (it.input && Object.keys(it.input).length) body.appendChild(prettyJson(it.input));
  d.appendChild(body);
  activePane.appendChild(d);
}

function startTool(data) {
  const d = el("details", "tool");
  d.open = true;
  d._t0 = Date.now();              // start time → run duration shown when the tool finishes
  activeToolMutating = false;      // reset; onCommand flips it if a mutating command streams under it
  const sum = el("summary");
  sum.appendChild(el("span", "tname", data.name));
  sum.appendChild(el("span", "tool-status", "running…"));
  d.appendChild(sum);
  const body = el("div", "body");
  if (data.input && Object.keys(data.input).length) {
    body.appendChild(prettyJson(data.input));
  }
  d.appendChild(body);
  activePane.appendChild(d);
  toolEls[data.id] = d;
  // commands stream into a console under this tool
  activeConsole = el("pre", "console");
  body.appendChild(activeConsole);
}

function consoleLine(text, cls) {
  if (!activeConsole) return;
  if (activeConsole.childNodes.length) activeConsole.appendChild(document.createTextNode("\n"));
  activeConsole.appendChild(cls ? el("span", cls, text) : document.createTextNode(text));
  activeConsole.scrollTop = activeConsole.scrollHeight;
}

function appendConsole(line) { consoleLine(line, null); }

// A command the agent actually executed — stream it into the running tool's console (so even
// silent read-only probes stay visible there) AND drop an inline command row into the transcript
// at this exact point in execution order. The inline row is the debug view: CSS-hidden until
// debug mode is on, so toggling the >_ button reveals/hides the command trail in place.
function onCommand(data) {
  if (data.mode && data.mode !== "read_only") activeToolMutating = true;   // → MUTATING badge on the tool
  consoleLine("$ " + (data.text || (data.argv || []).join(" ")), "cmd-line");
  addInlineCommand(data);
}

// Append one executed command to the active transcript, in the position it ran. Same shape for a
// live `command` event and a replayed `command` history item, so both render identically. Stays
// display:none unless html[data-debug="on"] (see the inline-command CSS).
function addInlineCommand(data) {
  if (!activePane) return;
  const mutating = data.mode && data.mode !== "read_only";
  const row = el("div", "cmd-inline");
  row.appendChild(el("span", "badge " + (mutating ? "mut" : "ro"), mutating ? "mutating" : "read-only"));
  row.appendChild(el("span", "cmd-text", data.text || (data.argv || []).join(" ")));
  row.appendChild(el("span", "cmd-tag", data.auto_run ? "auto" : "approved"));
  activePane.appendChild(row);
}

function finishTool(data) {
  const d = toolEls[data.id];
  if (d) {
    const status = d.querySelector("summary .tool-status");
    const meta = toolMetaSpan(activeToolMutating, fmtDurShort(d._t0 ? (Date.now() - d._t0) / 1000 : null));
    if (status) status.replaceWith(meta);
    else d.querySelector("summary").appendChild(meta);
    d.open = false;
  }
  renderToolResultCards(data);
  activeConsole = null;
  activeToolMutating = false;
}

// Draw the prominent, friendly card(s) for a tool result. Shared by the LIVE `tool_result`
// event (finishTool) and the history REPLAY of a persisted card result (renderHistory), so a
// resumed/reloaded chat rebuilds the report summary + clickable charts identically. The tool
// panel itself stays minimal — just the tool name + its input args (the file/path it read) — so
// the raw result payload is NEVER dumped into the transcript (it was a wall of text for the plain
// read/info tools, and redundant under the friendly cards). Each card renderer no-ops on a shape
// it can't draw; an undrawable result simply leaves the panel showing its args, same as history.
function renderToolResultCards(data) {
  const r = data.result;
  if (data.name === "locate_and_parse_report" && r && r.summary) {
    renderReportSummary(r);                 // the summary IS the friendly view
  } else if (data.name === "analyze_results") { renderParetoCard(r); }  // sweep scatter (next-step buttons now come from the agent's suggest_next_steps)
  else if (data.name === "compare_reports") renderComparisonCard(r);   // A/B delta bars
  else if (data.name === "compare_harness_runs") renderHarnessCompareCard(r);
  else if (data.name === "probe_environment") renderEnvStatus(r);      // host/cluster status
  else if (data.name === "check_capacity") renderCapacityCard(r);      // capacity pre-flight
  else if (data.name === "check_endpoint_readiness") renderReadinessCard(r);
  else if (data.name === "advise_accelerators") renderAcceleratorCard(r);
  else if (data.name === "generate_doe_experiment") renderDoeCard(r);  // sweep matrix
  else if (data.name === "orchestrate_benchmark_run") renderOrchestrateCard(r);
  else if (data.name === "export_run_bundle") renderReproducibilityCard(r);  // provenance bundle
  else if (data.name === "suggest_next_steps") renderAgentSuggestions(r);  // the agent's "what next?" buttons
}

// ---- report number formatting -------------------------------------------
// The report payload carries raw floats (e.g. 0.00301993 s, 523.994 tok/s). These
// turn them into the human-friendly forms the assistant uses in prose (3.0 ms, 524 tok/s).

// Round a number to a sensible number of significant digits for display; pass
// through anything non-numeric untouched.
function fmtNum(v) {
  if (typeof v !== "number" || !isFinite(v)) return v == null ? null : String(v);
  const a = Math.abs(v);
  if (a === 0) return "0";
  if (a >= 1000) return Math.round(v).toLocaleString("en-US");
  if (a >= 100) return v.toFixed(0);
  if (a >= 10) return v.toFixed(1);
  if (a >= 1) return v.toFixed(2);
  if (a >= 0.001) return v.toFixed(3);
  return v.toExponential(1);
}

// Pick a single display unit + scale for a metric from its `units` field. Sub-second
// latency reads better in ms; token/request rates get short labels. Returns {unit, scale}.
function statUnit(stat) {
  const u = (stat && stat.units) || "";
  if (u === "s" || u === "sec" || u === "seconds") {
    if (typeof stat.mean === "number" && Math.abs(stat.mean) < 1) return { unit: "ms", scale: 1000 };
    return { unit: "s", scale: 1 };
  }
  if (u === "ms") return { unit: "ms", scale: 1 };
  if (u.indexOf("token") !== -1) return { unit: "tok/s", scale: 1 };
  if (u.indexOf("quer") !== -1 || u.indexOf("req") !== -1) return { unit: "req/s", scale: 1 };
  if (u === "percent" || u === "%") return { unit: "%", scale: 1 };
  return { unit: u, scale: 1 };
}

// Format one value (`stat[key]`) of a `_stat` object with its unit, e.g. "3.0 ms".
// Returns null when the value is absent so callers can skip the tile/cell cleanly.
function fmtStat(stat, key) {
  if (!stat || typeof stat !== "object") return null;
  const v = stat[key];
  if (typeof v !== "number" || !isFinite(v)) return null;
  const { unit, scale } = statUnit(stat);
  const n = fmtNum(v * scale);
  if (unit === "%") return `${n}%`;
  return unit ? `${n} ${unit}` : n;
}

// Parse the common ISO-8601 duration the harness emits ("PT40.151280278S", "PT1M5S")
// into a readable "40.2s" / "1m 5s"; fall back to the raw string on no match.
function fmtDuration(iso) {
  if (typeof iso !== "string") return null;
  const m = iso.match(/^P(?:T)?(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?$/i);
  if (!m || (!m[1] && !m[2] && !m[3])) return iso;
  const h = m[1] ? parseInt(m[1], 10) : 0;
  const min = m[2] ? parseInt(m[2], 10) : 0;
  const sec = m[3] ? parseFloat(m[3]) : 0;
  const parts = [];
  if (h) parts.push(`${h}h`);
  if (min) parts.push(`${min}m`);
  if (sec || !parts.length) parts.push(`${Number.isInteger(sec) ? sec : sec.toFixed(1)}s`);
  return parts.join(" ");
}

function renderReportSummary(result) {
  const s = result.summary;
  const L = s.latency || {}, T = s.throughput || {}, SM = s.standard_metrics || {};
  const wrap = el("div", "msg report");
  wrap.appendChild(whoEl("report"));
  const bubble = el("div", "bubble");
  bubble.appendChild(el("strong", null, `Benchmark results — ${s.model || "model"}`));

  // Context subline: which harness, the load it drove, and how long it ran.
  const sub = [];
  if (s.harness) sub.push(s.harness);
  if (s.load && s.load.rate_qps != null) sub.push(`${fmtNum(s.load.rate_qps)} QPS`);
  if (s.load && typeof s.load.concurrency === "number" && isFinite(s.load.concurrency)) sub.push(`concurrency ${s.load.concurrency}`);
  const dur = fmtDuration(s.duration);
  if (dur) sub.push(dur);
  if (sub.length) bubble.appendChild(el("div", "report-sub", sub.join(" · ")));

  // Headline tiles — the same metrics the assistant calls out in its written summary.
  const grid = el("div", "summary-grid");
  const add = (k, v) => { if (v == null) return; const c = el("div", "stat"); c.appendChild(el("div", "k", k)); c.appendChild(el("div", "v", v)); grid.appendChild(c); };
  add("requests", s.requests_total != null
    ? (s.requests_failures ? `${s.requests_total} (${s.requests_failures} failed)` : `${s.requests_total}`)
    : null);
  add("success %", s.success_rate_pct != null ? `${fmtNum(s.success_rate_pct)}%` : null);
  add("TTFT mean", fmtStat(L.ttft, "mean"));
  add("TTFT p99", fmtStat(L.ttft, "p99"));
  add("latency mean", fmtStat(L.request_latency, "mean"));
  add("latency p99", fmtStat(L.request_latency, "p99"));
  add("per-token (TPOT)", fmtStat(L.tpot, "mean"));
  add("total tok/s", fmtStat(T.total_token_rate, "mean"));
  add("output tok/s", fmtStat(T.output_token_rate, "mean"));
  add("req/s", fmtStat(T.request_rate, "mean"));
  // §3.4 resource/serving metrics — only present when the harness emitted them.
  const addStd = (key, label) => { const mt = SM[key]; if (mt && mt.value) add(label, fmtStat(mt.value, "mean")); };
  addStd("kv_cache_hit_rate", "KV-cache hit");
  addStd("gpu_utilization", "GPU util");
  addStd("schedule_delay", "schedule delay");
  bubble.appendChild(grid);

  renderPercentileTable(bubble, L, T);
  renderReportCharts(bubble, result.charts);

  // Reproducibility footer: a one-click ask to capture a provenance bundle (repo SHAs + exact
  // config) so this run can be regenerated/shared. If the result already carries a bundle_id
  // (e.g. the agent already exported one), show the live Reproduce + Export affordances instead.
  bubble.appendChild(reportActions(result.bundle_id, currentSession));

  wrap.appendChild(bubble);
  activePane.appendChild(wrap);
}

// Build the .report-actions footer row. With a bundle_id present we offer Reproduce (sends a
// canned user message that prompts the agent to call reproduce_run — NOT a direct mutation) plus
// Export report card (opens the self-contained HTML download). Without one, a single "Save
// provenance bundle" ask that prompts the agent to export one. Reused by the report card and the
// results sidebar.
function reportActions(bundleId, sessionId) {
  const row = el("div", "report-actions");
  if (bundleId && sessionId) {
    const rep = el("button", "report-action", "↻ Reproduce this run");
    rep.type = "button";
    rep.addEventListener("click", () =>
      sendUserMessage(`Reproduce this run from its provenance bundle ${bundleId}`));
    row.appendChild(rep);
    const exp = el("button", "report-action", "⬇ Export report card");
    exp.type = "button";
    exp.addEventListener("click", () =>
      window.open(`/api/sessions/${encodeURIComponent(sessionId)}/bundle/${encodeURIComponent(bundleId)}/report-card.html`, "_blank"));
    row.appendChild(exp);
  } else {
    const save = el("button", "report-action", "🔖 Save provenance bundle");
    save.type = "button";
    save.addEventListener("click", () =>
      sendUserMessage("Capture a reproducibility provenance bundle for this run so it can be regenerated and shared."));
    row.appendChild(save);
  }
  return row;
}

// The export_run_bundle tool result card: the bundle id, a loud dirty banner when a repo was
// dirty, the copy-paste regenerate command (with a Copy button), and the Reproduce + Export
// affordances wired to the new backend routes.
function renderReproducibilityCard(r) {
  if (!r || !r.exported || !r.bundle_id) return;
  const wrap = el("div", "msg report");
  wrap.appendChild(whoEl("provenance"));
  const bubble = el("div", "bubble");
  bubble.appendChild(el("strong", null, "Provenance bundle captured"));
  bubble.appendChild(el("div", "report-sub", `bundle ${r.bundle_id}`));

  if (r.dirty) {
    bubble.appendChild(el("div", "prov-dirty-banner",
      "⚠ A repo had uncommitted changes when this run was captured — an exact re-run needs the same working tree."));
  }
  // Repo SHAs (+ unavailable flags) as compact chips.
  if (r.repos) {
    const chips = el("div", "prov-repos");
    for (const [name, st] of Object.entries(r.repos)) {
      const sha = (st && st.unavailable) ? "(unavailable)" : ((st && st.sha) || "?");
      const c = el("span", "prov-chip" + (st && (st.dirty || st.unavailable) ? " prov-dirty" : ""),
        `${name} @ ${sha}${st && st.dirty ? " · dirty" : ""}`);
      chips.appendChild(c);
    }
    bubble.appendChild(chips);
  }
  // The copy-paste regenerate command.
  if (r.regenerate_command) {
    const pre = el("pre", "prov-cmd");
    pre.textContent = r.regenerate_command;
    bubble.appendChild(pre);
    wrapWithCopy(pre);
  }
  bubble.appendChild(reportActions(r.bundle_id, currentSession));
  wrap.appendChild(bubble);
  activePane.appendChild(wrap);
}

// Collapsible drill-down: the full percentile ladder for every latency/throughput metric
// the report carries. One unit per row (header), bare numbers in the cells.
function renderPercentileTable(bubble, L, T) {
  const rows = [
    ["TTFT", L.ttft], ["TPOT", L.tpot], ["ITL", L.itl], ["request latency", L.request_latency],
    ["total tok/s", T.total_token_rate], ["output tok/s", T.output_token_rate], ["request rate", T.request_rate],
  ].filter(([, st]) => st && typeof st === "object");
  if (!rows.length) return;

  const cols = ["mean", "p50", "p90", "p95", "p99", "p99p9"];
  const colLabels = ["mean", "p50", "p90", "p95", "p99", "p99.9"];
  const det = el("details", "pctl");
  det.appendChild(el("summary", null, "All percentiles"));
  const tbl = el("table", "pctl-table");
  const head = el("tr");
  head.appendChild(el("th", null, "metric"));
  colLabels.forEach((c) => head.appendChild(el("th", null, c)));
  tbl.appendChild(head);
  rows.forEach(([name, st]) => {
    const { unit, scale } = statUnit(st);
    const tr = el("tr");
    tr.appendChild(el("th", null, unit ? `${name} (${unit})` : name));
    cols.forEach((k) => {
      const v = st[k];
      tr.appendChild(el("td", null, (typeof v === "number" && isFinite(v)) ? fmtNum(v * scale) : "—"));
    });
    tbl.appendChild(tr);
  });
  det.appendChild(tbl);
  bubble.appendChild(det);
}

// Render the per-run chart images the harness produced (served by the backend artifact
// route). `charts` is locate_and_parse_report's list of {title, session_id, path}; absent
// on the CPU-sim quickstart / guidellm, in which case we show nothing. Each thumbnail is
// click/keyboard-activatable and opens the full-size plot in a lightbox.
function renderReportCharts(bubble, charts) {
  if (!Array.isArray(charts) || charts.length === 0) return;
  const wrap = el("div", "charts");
  charts.forEach((c) => {
    const sid = c.session_id || currentSession;
    if (!sid || !c.path) return;
    const fig = el("figure", "chart");
    const img = document.createElement("img");
    img.loading = "lazy";
    img.alt = c.title || "benchmark chart";
    img.src = `/api/sessions/${encodeURIComponent(sid)}/artifact?path=${encodeURIComponent(c.path)}`;
    img.tabIndex = 0;
    img.setAttribute("role", "button");
    img.setAttribute("aria-label", `Expand ${c.title || "chart"}`);
    const open = () => openLightbox(img.src, c.title);
    img.addEventListener("click", open);
    img.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
    });
    fig.appendChild(img);
    if (c.title) fig.appendChild(el("figcaption", null, c.title));
    wrap.appendChild(fig);
  });
  if (wrap.childElementCount) bubble.appendChild(wrap);
}

// Lazily-created, reused modal that shows an enlarged chart. Native <dialog> gives us
// Esc-to-close and focus handling for free; we add a close button and backdrop-click.
let lightboxEls = null;
function ensureLightbox() {
  if (lightboxEls) return lightboxEls;
  const dlg = document.createElement("dialog");
  dlg.className = "lightbox";
  const close = el("button", "close", "✕");
  close.type = "button";
  close.setAttribute("aria-label", "Close");
  close.addEventListener("click", () => dlg.close());
  const fig = el("figure");
  const img = document.createElement("img");
  const cap = el("figcaption");
  fig.appendChild(close);
  fig.appendChild(img);
  fig.appendChild(cap);
  dlg.appendChild(fig);
  // A click whose target is the dialog itself is on the backdrop/padding → dismiss.
  dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });
  document.body.appendChild(dlg);
  lightboxEls = { dlg, img, cap };
  return lightboxEls;
}

function openLightbox(src, title) {
  const { dlg, img, cap } = ensureLightbox();
  img.src = src;
  img.alt = title || "benchmark chart";
  cap.textContent = title || "";
  cap.hidden = !title;
  if (typeof dlg.showModal === "function") dlg.showModal();
  else dlg.setAttribute("open", "");
}

// Lazily-created, reused modal that embeds the operator's Grafana dashboard at a large size. Native
// <dialog> gives Esc-to-close + focus handling; we add a ✕, a backdrop-click dismiss, and an
// "open in new tab" fallback for Grafana instances that refuse iframe embedding (X-Frame-Options /
// frame-ancestors) — without it those would render a blank modal. The iframe src is set on open and
// cleared on close so the embedded dashboard stops polling/refreshing in the background.
let grafanaModalEls = null;
function ensureGrafanaModal() {
  if (grafanaModalEls) return grafanaModalEls;
  const dlg = document.createElement("dialog");
  dlg.className = "grafana-modal";
  const inner = el("div", "grafana-modal-inner");
  const head = el("div", "grafana-modal-head");
  head.appendChild(el("span", "grafana-modal-title", "Grafana — live metrics"));
  const tab = el("a", "grafana-modal-tab", "Open in new tab ↗");
  tab.target = "_blank";
  tab.rel = "noopener noreferrer";
  const close = el("button", "grafana-modal-close", "✕");
  close.type = "button";
  close.setAttribute("aria-label", "Close");
  close.addEventListener("click", () => dlg.close());
  head.appendChild(tab);
  head.appendChild(close);
  const frame = el("iframe", "grafana-modal-frame");
  frame.title = "Live Grafana dashboard";
  frame.setAttribute("referrerpolicy", "no-referrer");
  frame.setAttribute("sandbox", "allow-scripts allow-same-origin");
  inner.appendChild(head);
  inner.appendChild(frame);
  dlg.appendChild(inner);
  // A click whose target is the dialog itself is on the backdrop → dismiss. Clearing src on close
  // (fires for ✕, Esc, and backdrop) stops the embed from fetching in the background.
  dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });
  dlg.addEventListener("close", () => { frame.src = "about:blank"; });
  document.body.appendChild(dlg);
  grafanaModalEls = { dlg, frame, tab };
  return grafanaModalEls;
}

function openGrafanaModal(url) {
  if (!url || !/^https?:\/\//i.test(url)) return;
  const { dlg, frame, tab } = ensureGrafanaModal();
  frame.src = url;
  tab.href = url;
  if (typeof dlg.showModal === "function") dlg.showModal();
  else dlg.setAttribute("open", "");
}

// ---- copy-to-clipboard for code / JSON blocks ----------------------------
// Wrap a <pre> in a hover container with a Copy button. Clipboard API with a textarea fallback
// for non-secure contexts; the button flashes "Copied" on success. Used for assistant fenced
// code (post-processed after markdown render) and tool-result JSON dumps.
function copyText(text, btn) {
  const flash = () => {
    const prev = btn.textContent;
    btn.textContent = "Copied"; btn.classList.add("copied");
    setTimeout(() => { btn.textContent = prev; btn.classList.remove("copied"); }, 1200);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(flash).catch(() => fallbackCopy(text, flash));
  } else {
    fallbackCopy(text, flash);
  }
}
function fallbackCopy(text, onDone) {
  try {
    const ta = document.createElement("textarea");
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.focus(); ta.select();
    document.execCommand("copy"); ta.remove();
    if (onDone) onDone();
  } catch (e) { /* clipboard unavailable — silently skip */ }
}
function wrapWithCopy(pre) {
  const parent = pre.parentNode;
  const wrap = el("div", "code-wrap");
  const btn = el("button", "copy-btn", "Copy");
  btn.type = "button"; btn.title = "Copy to clipboard"; btn.setAttribute("aria-label", "Copy to clipboard");
  btn.addEventListener("click", () => copyText(pre.textContent || "", btn));
  if (parent) parent.insertBefore(wrap, pre);   // take the pre's place in the DOM…
  wrap.appendChild(btn);
  wrap.appendChild(pre);                         // …then re-home the pre inside the wrapper
  return wrap;
}
// Post-process a freshly-rendered markdown bubble: give each fenced code block a Copy button.
function enhanceCodeBlocks(container) {
  container.querySelectorAll("pre.md-code").forEach(wrapWithCopy);
}

function prettyJson(obj) {
  const pre = el("pre", "json");
  let s;
  try { s = JSON.stringify(obj, null, 2); } catch { s = String(obj); }
  if (s && s.length > 4000) s = s.slice(0, 4000) + "\n… (truncated)";
  pre.textContent = s;
  return wrapWithCopy(pre);
}

// Build the body (heading + command/plan detail) shared by the live approval card and the
// resolved decision card replayed from history. `heading` is the card's title text.
function approvalCardBody(card, kind, payload, heading) {
  if (kind === "session_plan") {
    const h = el("h3");
    h.appendChild(el("span", null, heading));
    h.appendChild(el("span", "badge mut", "mutating"));
    card.appendChild(h);
    const dl = el("dl", "plan");
    const row = (k, v) => { if (v == null || v === "") return; dl.appendChild(el("dt", null, k)); dl.appendChild(el("dd", null, typeof v === "object" ? JSON.stringify(v) : String(v))); };
    row("use case", payload.use_case_summary);
    row("spec", payload.spec);
    row("namespace", payload.namespace);
    row("harness", payload.harness);
    row("workload", payload.workload);
    row("steps", (payload.expected_steps || []).join(" → "));
    row("reversible", payload.reversible);
    if (payload.notes) row("notes", payload.notes);
    card.appendChild(dl);
  } else {
    const h = el("h3");
    h.appendChild(el("span", null, heading));
    h.appendChild(el("span", "badge mut", "mutating"));
    card.appendChild(h);
    card.appendChild(el("div", "cmd", payload.command || (payload.argv || []).join(" ")));
  }
}

function addApprovalCard(data) {
  const { request_id, kind, payload } = data;
  // De-dup vs. SELF-HEAL: on reconnect the server (via reemit_pending) re-surfaces every
  // still-undecided approval — it is the source of truth that this gate is STILL OPEN. If our
  // cached pane already shows the card, skip re-adding (the existing card's buttons still work —
  // its resolve closure reads the current global `ws`, the freshly-reconnected socket). But trust
  // the re-emit over a STALE dedup key: if we hold a request_id but its card is no longer in the
  // live DOM (the pane was rebuilt/evicted/detached, or an older build mis-tracked it), the card
  // was silently lost — drop the dead ref and fall through to re-render, so a parked gate can
  // never strand the user with no Approve/Decline control after a chat switch.
  const existing = cur && cur.pendingApprovals[request_id];
  if (existing) {
    if (existing.isConnected) return false;    // genuinely already shown live — true dedup (no new card → don't tally)
    delete cur.pendingApprovals[request_id];   // stale ref: card is gone from the DOM — re-render
  }
  const card = el("div", "card");
  approvalCardBody(card, kind, payload, kind === "session_plan" ? "Session plan" : "Approve this command ");
  const actions = el("div", "actions");
  const approve = el("button", "approve", "Approve");
  const reject = el("button", "reject", "Reject");
  actions.appendChild(approve);
  // For COMMAND gates only (never the plan — auto-approve never skips the plan gate), offer a
  // one-click "approve this AND stop asking": it flips auto-approve on (server-persisted, mirrored
  // on the composer pill) so the rest of this chat's commands run without a card, then approves this
  // one. Only ever shown while auto-approve is OFF (when on, the server wouldn't emit this card).
  let approveAll = null;
  if (kind !== "session_plan") {
    approveAll = el("button", "approve approve-all", "Approve & stop asking");
    actions.appendChild(approveAll);
  }
  actions.appendChild(reject);
  card.appendChild(actions);
  // The user can also just TYPE a message instead of clicking — that declines this action and
  // steers the agent with what they said (see sendUserMessage). Tell them so the composer being
  // enabled under an open card reads as intentional, not a glitch.
  card.appendChild(el("div", "hint", "…or type a message to change something — that declines this and tells me what you want instead."));
  activePane.appendChild(card);
  if (cur) cur.pendingApprovals[request_id] = card;

  const resolve = (ok) => {
    // The socket can be mid-reconnect when the gate is clicked — setEnabled(false) only disables the
    // composer, not these buttons, so they stay clickable while disconnected. Sending on a
    // CLOSING/CLOSED socket throws InvalidStateError; bail WITHOUT the optimistic "resolved" UI so
    // the gate stays clickable and the decision can be re-sent once reconnected (mirrors cancelRun).
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ type: "approval", request_id, approved: ok }));
    if (cur) { cur.running = true; delete cur.pendingApprovals[request_id]; }
    setEnabled(false);  // re-lock the composer: clicking resumes the turn (working), not parked
    startWorking();     // the turn resumes after the user decides (approve or reject), until "done"
    approve.disabled = reject.disabled = true;
    if (approveAll) approveAll.disabled = true;   // disable it too, or a later click flips auto-approve + double-resolves
    const at = new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    card.appendChild(el("div", "resolved", ok ? `✓ Approved · ${at}` : "✗ Rejected"));
  };
  approve.onclick = () => resolve(true);
  reject.onclick = () => resolve(false);
  if (approveAll) approveAll.onclick = () => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "set_auto_approve", enabled: true }));
      applyAutoApprove(true);   // mirror it on the composer pill now (server persists + re-seeds via `ready`)
    }
    resolve(true);              // approve THIS command (resolve bails harmlessly if the socket is down)
  };
  return true;                  // a card was actually rendered (callers gate the unread tally on this)
}

// A resolved approval replayed from a reopened chat: same card, no buttons, just the outcome.
function addDecisionCard(it) {
  const { kind, payload, approved } = it;
  const card = el("div", "card");
  approvalCardBody(card, kind || "command", payload || {}, kind === "session_plan" ? "Session plan" : "Command ");
  card.appendChild(el("div", "resolved", approved ? "✓ Approved" : "✗ Rejected"));
  activePane.appendChild(card);
}

// Sticky auto-scroll: only jump to the bottom if the user was already pinned there (captured in
// `stickBottom` at the start of handle(), before new content shifted scrollHeight). This keeps a
// scrolled-up reading position — and a restored position on switch-back — instead of yanking down.
function scroll() { if (stickBottom) transcript.scrollTop = transcript.scrollHeight; updateJumpBtn(); }

// ---- "working" indicator (spinning hexagon + live status) ----------------
// Shown while a turn is in flight. The word cycles through generic gerunds while
// we're waiting on the model, and snaps to a specific verb/activity when a tool
// or command is running. Elapsed time ticks live.
const WORK_WORDS = ["Thinking", "Pondering", "Reasoning", "Planning", "Cogitating", "Crunching", "Calibrating", "Synthesizing", "Strategizing", "Working"];
const TOOL_VERBS = {
  probe_environment: "Probing environment",
  list_catalog: "Browsing catalog",
  read_repo_doc: "Reading docs",
  fetch_key_docs: "Reading docs",
  propose_session_plan: "Planning",
  check_capacity: "Checking capacity",
  ensure_repos: "Fetching repos",
  run_setup: "Setting up",
  write_and_validate_config: "Writing config",
  execute_llmdbenchmark: "Benchmarking",
  run_shell: "Running command",
  locate_and_parse_report: "Reading results",
  compare_reports: "Comparing results",
  compare_harness_runs: "Comparing harnesses",
  analyze_results: "Analyzing results",
  result_history: "Saving to history",
  orchestrate_benchmark_run: "Orchestrating run",
  observe_run_metrics: "Observing metrics",
};

function humanizeTool(name) {
  return String(name || "Working").replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}
function fmtElapsed(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return s + "s";
  return Math.floor(s / 60) + "m " + (s % 60) + "s";
}
function renderWorkStats() {
  let live = fmtElapsed(Date.now() - workStart);
  if (workActivity) live += " · " + workActivity;
  // Show the running turn token tally as it ticks up: ↑ = total input, ↓ = generated.
  if (turnUsage) {
    const up = (turnUsage.input || 0) + (turnUsage.cache_read || 0) + (turnUsage.cache_write || 0);
    live += " · ↑" + fmtTokens(up) + " ↓" + fmtTokens(turnUsage.output || 0);
  }
  workStatsEl.textContent = "(" + live + ")";
}
function cycleWord() {
  if (workWordFixed) return;
  workWordEl.textContent = WORK_WORDS[Math.floor(Math.random() * WORK_WORDS.length)];
}
function startWorking(initialWord) {
  workStart = Date.now();
  workActivity = null;
  turnUsage = null;          // reset the live turn tally; usage events repopulate it
  workWordFixed = false;
  workWordEl.textContent = initialWord || WORK_WORDS[0];
  renderWorkStats();
  workingEl.hidden = false;
  if (stopBtn) stopBtn.disabled = false;
  clearInterval(workTimer); clearInterval(wordTimer);
  workTimer = setInterval(renderWorkStats, 250);
  wordTimer = setInterval(cycleWord, 2200);
}
// Resume the indicator for an ALREADY-running turn on (re)connect: seed elapsed from the
// server's authoritative `running_elapsed_ms` (a duration, so it's clock-skew-proof — both
// terms use this client's Date.now()) and keep ticking. Unlike startWorking it PRESERVES the
// live turn tally and current verb/activity, which the buffered replay restores or extends.
function resumeWorking(elapsedMs) {
  workStart = Date.now() - (Number(elapsedMs) || 0);
  workingEl.hidden = false;
  if (stopBtn) stopBtn.disabled = false;
  renderWorkStats();
  clearInterval(workTimer); clearInterval(wordTimer);
  workTimer = setInterval(renderWorkStats, 250);
  wordTimer = setInterval(cycleWord, 2200);
}
function stopWorking() {
  clearInterval(workTimer); clearInterval(wordTimer);
  workTimer = wordTimer = null;
  workingEl.hidden = true;
  workActivity = null; workWordFixed = false;
}
// Stop button: send the `cancel` control frame (backend reaps the in-flight turn/subprocess and
// answers with `cancelled` + `done`). Optimistically reflect "Stopping…" so the click feels live;
// the events do the real cleanup. Cancelling targets the chat the socket is attached to.
function cancelRun() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  try { ws.send(JSON.stringify({ type: "cancel" })); } catch (e) { return; }
  workWordFixed = true;
  workWordEl.textContent = "Stopping";
  workActivity = null;
  renderWorkStats();
  if (stopBtn) stopBtn.disabled = true;
}
function setWorkTool(name) {       // a tool started — snap to its verb
  workWordFixed = true;
  workWordEl.textContent = TOOL_VERBS[name] || humanizeTool(name);
  workActivity = null;
  renderWorkStats();
}
function setWorkActivity(text) {   // a command started — show it after the "·"
  workActivity = text ? String(text).slice(0, 52) : null;
  renderWorkStats();
}
function resumeThinking() {        // back between steps — resume generic cycling
  workWordFixed = false;
  workActivity = null;
  cycleWord();
  renderWorkStats();
}

// ---- input --------------------------------------------------------------

// Send a user message — shared by the composer form and a clicked suggestion chip, so the two
// paths never drift. Appends the user bubble, sends the frame, and flips the UI into "working".
function sendUserMessage(text) {
  text = (text || "").trim();
  // No `busy` guard: sending WHILE a turn runs is allowed and means "steer". Only a closed socket
  // or empty text blocks the send. At an approval gate the turn is PARKED (busy=false), so that
  // type-instead-of-approve path flows through the normal "start working" branch below, unchanged.
  if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;
  const steering = busy;            // a turn is actively running -> this send redirects it
  removeWelcomeCard();              // the conversation has started — clear any suggestion chips
  // If a turn is parked at an approval gate and the user typed instead of clicking, this message
  // means "decline the pending action(s) and do THIS instead". The server resolves those gates as
  // rejected and threads this text into the same turn; reflect it in the open cards immediately so
  // the UI doesn't leave live Approve/Decline buttons under a message that already superseded them.
  if (cur && cur.pendingApprovals) {
    for (const rid of Object.keys(cur.pendingApprovals)) {
      const card = cur.pendingApprovals[rid];
      if (card) {
        card.querySelectorAll("button").forEach((b) => { b.disabled = true; });
        card.appendChild(el("div", "resolved", "✗ declined — you replied instead"));
      }
      delete cur.pendingApprovals[rid];
    }
  }
  addBubble("user", text);
  ws.send(JSON.stringify({ type: "user_message", text }));
  if (steering) {
    // The turn is already running and will pick this up at its next step — the server queued it.
    // Don't re-lock the composer or restart the "working" indicator (it's already spinning); just
    // leave the optimistic bubble in place. Mark a steer so the indicator reads as redirected.
    setWorkActivity("Steering — folding in your message…");
  } else {
    setEnabled(false);
    if (cur) cur.running = true;    // this chat now has a turn in flight (kept across switches)
    startWorking();
  }
  stickBottom = true; scroll();     // sending always pins to the newest message
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = input.value.trim();
  // No `busy` guard here either — a submit mid-turn steers (sendUserMessage routes it). Guard only
  // empty text / a closed socket so the field still clears predictably.
  if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;
  sendUserMessage(text);
  input.value = "";
  input.style.height = "auto";
});

input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
});
input.addEventListener("input", () => { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 200) + "px"; });

newChatBtn.addEventListener("click", newChat);
if (stopBtn) stopBtn.addEventListener("click", cancelRun);

// Jump-to-latest: a floating button that appears once the user scrolls up off the bottom of the
// transcript, and pins them back to the newest message on click. Complements the sticky
// auto-scroll (which only follows new content when already near the bottom).

// Tally a freshly-arrived message while the user is scrolled up (stickBottom is false), so the jump
// button can show how many they haven't seen. Called from the live handle() cases ONLY — replayed
// history doesn't pass through here, so the count only ever reflects real-time arrivals.
function noteNewMessage() { if (!stickBottom) unreadCount++; }

// Show the button only when there's meaningfully more content below the fold — and label it with the
// unread tally. Called on scroll, on chat switch (from activate), and now after every render (via
// scroll()) so the count/visibility stay live; a fresh/short chat hides a button left over from the
// chat we came from.
function updateJumpBtn() {
  if (!jumpBtn) return;
  const atBottom = (transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight) < 120;
  if (atBottom) unreadCount = 0;        // back at the bottom = caught up — clear the unread tally
  jumpBtn.hidden = atBottom;
  // Label doubles as an unread badge: "↓ N new messages" while messages arrived behind the fold,
  // plain "↓ Latest" when there's just more to scroll to but nothing new.
  jumpBtn.textContent = unreadCount > 0
    ? `↓ ${unreadCount} new message${unreadCount === 1 ? "" : "s"}`
    : "↓ Latest";
}
if (jumpBtn) {
  transcript.addEventListener("scroll", updateJumpBtn);
  jumpBtn.addEventListener("click", () => {
    stickBottom = true; unreadCount = 0; transcript.scrollTop = transcript.scrollHeight; updateJumpBtn();
  });
}

// Sidebar toggle (ChatGPT-style). The single header button is context-aware:
//   • Desktop (> mobile breakpoint): collapse/expand the persistent sidebar in place. The
//     collapsed state is remembered across reloads in localStorage.
//   • Mobile (≤ breakpoint): drive the off-canvas drawer + tap-scrim.
// The button itself sits in the main header, so it stays visible whether the sidebar is open
// or collapsed. Selecting a chat closes the mobile drawer (see switchTo).
const sidebarMql = window.matchMedia("(max-width: 760px)");

function setSidebar(open) {                         // mobile off-canvas drawer
  document.body.classList.toggle("sidebar-open", open);
  if (sidebarScrim) sidebarScrim.hidden = !open;
  syncSidebarToggleState();
}
function setSidebarCollapsed(collapsed) {           // desktop in-place collapse
  document.body.classList.toggle("sidebar-collapsed", collapsed);
  try { localStorage.setItem("llmd-sidebar-collapsed", collapsed ? "1" : "0"); } catch (e) {}
  syncSidebarToggleState();
}
function syncSidebarToggleState() {
  // aria-expanded reflects whichever mechanism is live at the current breakpoint.
  const open = sidebarMql.matches
    ? document.body.classList.contains("sidebar-open")
    : !document.body.classList.contains("sidebar-collapsed");
  if (sidebarToggle) sidebarToggle.setAttribute("aria-expanded", open ? "true" : "false");
  // The header expand button mirrors the inverse — it's the "open me" affordance.
  if (sidebarExpand) sidebarExpand.setAttribute("aria-expanded", open ? "true" : "false");
}
// Restore the persisted desktop collapse state on load.
try {
  if (localStorage.getItem("llmd-sidebar-collapsed") === "1") {
    document.body.classList.add("sidebar-collapsed");
  }
} catch (e) {}
syncSidebarToggleState();

// Both controls drive the same toggle: the in-sidebar button (primary collapse control) and the
// header expand button (reachable when the sidebar is collapsed away). Mobile → off-canvas drawer.
function toggleSidebar() {
  if (sidebarMql.matches) setSidebar(!document.body.classList.contains("sidebar-open"));
  else setSidebarCollapsed(!document.body.classList.contains("sidebar-collapsed"));
}
if (sidebarToggle) sidebarToggle.addEventListener("click", toggleSidebar);
if (sidebarExpand) sidebarExpand.addEventListener("click", toggleSidebar);
if (sidebarScrim) sidebarScrim.addEventListener("click", () => setSidebar(false));
// Keep aria-expanded honest when the viewport crosses the mobile breakpoint.
sidebarMql.addEventListener("change", syncSidebarToggleState);

// Manual collapse of the split view; the next `resource_stats` tick of a still-running run reopens it.
if (resourceSideClose) resourceSideClose.addEventListener("click", clearResourceStats);

// ---- guided benchmark builder -------------------------------------------
// A friendly form that lets a non-expert compose a benchmark request from a few choices, previews
// the plain-language brief it will send, and dispatches it as an ordinary user message. It does NO
// mapping of its own (which scenario/harness/workload, which flags) — that stays the agent's
// judgment; the builder only turns chosen options into English, exactly what a user could type.
let builderTouched = false;   // the user hand-edited the preview → stop auto-rewriting over them

// The selected chip in a group: {value (the data-value sent), noun (data-noun for phrasing)} or null.
function builderSel(field) {
  const group = builderDlg && builderDlg.querySelector('.chip-group[data-field="' + field + '"]');
  const on = group && group.querySelector(".bchip.sel");
  return on ? { value: on.dataset.value || "", noun: group.dataset.noun || "" } : null;
}

// A non-negative SLO number from an input, or null when blank/invalid (so it's simply omitted).
function sloVal(id) {
  const node = document.getElementById(id);
  const n = node ? parseFloat(node.value) : NaN;
  return Number.isFinite(n) && n >= 0 ? n : null;
}

// Compose the plain-language brief from the current selections. Only chosen fields appear; the
// closing line hands ALL the actual mapping (scenario/harness/workload, flags) back to the agent.
function composeBrief() {
  const lines = [];
  const uc = builderSel("usecase");
  lines.push(uc ? "I'd like to benchmark " + uc.value + "." : "Help me design a benchmark for my use case.");

  const scaleParts = [];
  const scale = builderSel("scale"); if (scale) scaleParts.push(scale.value);
  const pattern = builderSel("pattern"); if (pattern) scaleParts.push(pattern.value);
  if (scaleParts.length) lines.push("- Load: " + scaleParts.join(", ") + ".");

  const shapeParts = [];
  const inp = builderSel("input"); if (inp) shapeParts.push(inp.value + " " + inp.noun);
  const outp = builderSel("output"); if (outp) shapeParts.push(outp.value + " " + outp.noun);
  if (shapeParts.length) lines.push("- Token shape: " + shapeParts.join(", ") + ".");

  const slo = [];
  const ttft = sloVal("slo-ttft"); if (ttft != null) slo.push("TTFT ≤ " + ttft + " ms");
  const tpot = sloVal("slo-tpot"); if (tpot != null) slo.push("TPOT ≤ " + tpot + " ms");
  const tput = sloVal("slo-tput"); if (tput != null) slo.push("throughput ≥ " + tput + " tokens/s");
  if (slo.length) lines.push("- SLO targets: " + slo.join("; ") + ".");

  const hw = builderSel("hardware");
  if (hw) lines.push("- Hardware: " + hw.value + ".");

  lines.push("");
  lines.push("Please recommend the right scenario, harness, and workload profile, explain the trade-offs, and propose a plan I can approve.");
  return lines.join("\n");
}

function refreshBuilderPreview() {
  if (builderPreview && !builderTouched) builderPreview.value = composeBrief();
}

function openBuilder() {
  if (!builderDlg || !builderDlg.showModal || builderDlg.open) return;
  builderTouched = false;            // a fresh open re-syncs the preview to the current choices
  refreshBuilderPreview();
  builderDlg.showModal();
}
function closeBuilder() { if (builderDlg && builderDlg.open) builderDlg.close(); }

function submitBuilder() {
  const text = ((builderPreview && builderPreview.value) || "").trim();
  closeBuilder();
  if (!text) return;
  // If we can't send right now (socket not open), drop the brief into the composer so the user's
  // work isn't lost rather than silently no-op'ing. Do NOT gate on `busy`: sending WHILE a turn
  // runs is allowed and means "steer" (sendUserMessage handles it) — gating on busy here both
  // refused a legitimate steer AND clobbered any draft the user had already typed in the composer.
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    input.value = text; input.focus();
    input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 200) + "px";
    return;
  }
  sendUserMessage(text);
}

// Chip groups are single-select; a click selects (clearing its siblings), a second click on the
// same chip clears the choice. Any change re-previews unless the user has hand-edited the text.
if (builderDlg) {
  builderDlg.querySelectorAll(".chip-group").forEach((group) => {
    group.addEventListener("click", (e) => {
      const btn = e.target.closest(".bchip");
      if (!btn || !group.contains(btn)) return;
      const wasSel = btn.classList.contains("sel");
      group.querySelectorAll(".bchip").forEach((b) => b.classList.remove("sel"));
      if (!wasSel) btn.classList.add("sel");
      refreshBuilderPreview();
    });
  });
  builderDlg.querySelectorAll(".slo-input").forEach((i) => i.addEventListener("input", refreshBuilderPreview));
  if (builderPreview) builderPreview.addEventListener("input", () => { builderTouched = true; });
  builderDlg.addEventListener("click", (e) => { if (e.target === builderDlg) closeBuilder(); });  // backdrop
}
if (builderToggle) builderToggle.addEventListener("click", openBuilder);
if (builderClose) builderClose.addEventListener("click", closeBuilder);
if (builderCancel) builderCancel.addEventListener("click", closeBuilder);
if (builderSend) builderSend.addEventListener("click", submitBuilder);

// ---- share a chat via a read-only link ----------------------------------
// Two halves: (1) the OWNER mints/manages a link from the 🔗 header dialog (create + copy +
// revoke); (2) a RECIPIENT opens /share/<token>, which serves this same SPA — bootShareView()
// detects the path, renders the frozen snapshot read-only, and never opens a WebSocket. The
// transcript renderers (renderHistory + friends) are reused verbatim, so a shared chat looks
// exactly like the live one, just without the composer/sidebar/agent.
let shareToken = null;   // the token of the link currently shown in the dialog (for revoke)

// The /share/<token> path of the public viewer page, or null when this is the normal app.
function shareTokenFromPath() {
  const m = location.pathname.match(/^\/share\/([0-9a-f]{32})$/);
  return m ? m[1] : null;
}

function openShareDialog() {
  if (!shareDlg || !shareDlg.showModal || shareDlg.open) return;
  // Reset to the "creating" state on every open (a previous link's token must not linger).
  shareToken = null;
  if (shareUrlInput) shareUrlInput.value = "";
  if (shareOpenLink) { shareOpenLink.removeAttribute("href"); shareOpenLink.classList.add("disabled"); }
  if (shareDownloadLink) { shareDownloadLink.removeAttribute("href"); shareDownloadLink.classList.add("disabled"); }
  if (shareCopyBtn) shareCopyBtn.disabled = true;
  if (shareRevokeBtn) shareRevokeBtn.disabled = true;
  if (shareStatus) shareStatus.textContent = "Creating link…";
  shareDlg.showModal();
}
function closeShareDialog() { if (shareDlg && shareDlg.open) shareDlg.close(); }

// Put a URL into the dialog as the ACTIVE link (input + Open + Copy enabled).
function setShareUrl(url) {
  if (shareUrlInput) { shareUrlInput.value = url; shareUrlInput.focus(); shareUrlInput.select(); }
  if (shareOpenLink) { shareOpenLink.href = url; shareOpenLink.classList.remove("disabled"); }
  if (shareCopyBtn) shareCopyBtn.disabled = false;
}

// Mint (or surface) a read-only link for the CURRENT conversation and show it for copying. The
// backend snapshots the transcript as it is right now; sending more messages later won't change
// the shared copy. The link is same-origin (ABSOLUTE when SHARE_BASE_URL is set, else this
// browser's origin), so it works while the app is reachable; the Download button offers the
// self-contained .html export for sharing without exposing the app.
async function shareChat() {
  openShareDialog();
  if (!currentSession) {
    if (shareStatus) shareStatus.textContent = "Start the conversation first — there's nothing to share yet.";
    return;
  }
  try {
    const r = await fetch(`/api/sessions/${encodeURIComponent(currentSession)}/share`, { method: "POST" });
    if (r.status === 400) {
      if (shareStatus) shareStatus.textContent = "Start the conversation first — there's nothing to share yet.";
      return;
    }
    if (!r.ok) throw new Error("share failed: " + r.status);
    const j = await r.json();
    shareToken = j.token;
    // The same-origin link (ABSOLUTE when SHARE_BASE_URL is set, else this browser's origin).
    const localUrl = /^https?:\/\//i.test(j.url) ? j.url : location.origin + j.url;
    // Self-contained single-file export of this snapshot — always same-origin (the API path),
    // independent of SHARE_BASE_URL; the browser downloads it via the route's Content-Disposition.
    if (shareDownloadLink) {
      shareDownloadLink.href = `/api/share/${encodeURIComponent(j.token)}/page.html`;
      shareDownloadLink.classList.remove("disabled");
    }
    if (shareRevokeBtn) shareRevokeBtn.disabled = false;
    setShareUrl(localUrl);
    if (shareStatus) shareStatus.textContent =
      "Link ready — it opens a read-only copy while this app is reachable. Prefer a file? Download the self-contained .html below.";
  } catch (e) {
    if (shareStatus) shareStatus.textContent = "Couldn't create a share link. Please try again.";
  }
}

// Revoke the link currently shown — its snapshot is deleted server-side and the URL stops working.
async function revokeShare() {
  if (!shareToken) return;
  if (!confirm("Delete this share link? Anyone who has it will no longer be able to view the conversation.")) return;
  try {
    await fetch(`/api/share/${encodeURIComponent(shareToken)}`, { method: "DELETE" });
  } catch (e) { /* best-effort; reflect deletion regardless */ }
  shareToken = null;
  if (shareUrlInput) shareUrlInput.value = "";
  if (shareOpenLink) { shareOpenLink.removeAttribute("href"); shareOpenLink.classList.add("disabled"); }
  if (shareDownloadLink) { shareDownloadLink.removeAttribute("href"); shareDownloadLink.classList.add("disabled"); }
  if (shareCopyBtn) shareCopyBtn.disabled = true;
  if (shareRevokeBtn) shareRevokeBtn.disabled = true;
  if (shareStatus) shareStatus.textContent = "Link deleted — anyone who has it can no longer open it.";
}

if (shareBtn) shareBtn.addEventListener("click", shareChat);
if (shareClose) shareClose.addEventListener("click", closeShareDialog);
if (shareDone) shareDone.addEventListener("click", closeShareDialog);
if (shareCopyBtn) shareCopyBtn.addEventListener("click", () => copyText(shareUrlInput ? shareUrlInput.value : "", shareCopyBtn));
if (shareRevokeBtn) shareRevokeBtn.addEventListener("click", revokeShare);
if (shareDlg) shareDlg.addEventListener("click", (e) => { if (e.target === shareDlg) closeShareDialog(); });  // backdrop

// Render a shared snapshot ({title, shared_at, items}) into the read-only viewer. Shared by the
// live viewer (bootShareView, fetched over HTTP) and the offline self-contained export
// (bootSharedStatic, embedded) so both look pixel-identical and reuse every transcript renderer.
function renderSharedSnapshot(data) {
  data = data || {};
  document.title = (data.title ? data.title + " · " : "") + "Shared conversation";
  setHeaderTitle(data.title || "Shared conversation");   // the header bar shows the snapshot's title
  // A small header line: the conversation title + when it was shared.
  const meta = el("div", "share-meta");
  meta.appendChild(el("div", "share-meta-title", data.title || "Shared conversation"));
  const when = data.shared_at ? relTime(data.shared_at) : "";
  meta.appendChild(el("div", "share-meta-sub", "Read-only snapshot" + (when ? " · shared " + when : "")));
  // The session's cumulative token spend, frozen into the snapshot's `usage` at share time.
  // Mirrors the live per-turn footer's ↑/↓ shape (appendTurnTokens) so the numbers read the same
  // way; `context` is the "N ctx" meter the owner saw when the link/export was made.
  const u = data.usage || {};
  if (u.total) {
    const up = (u.input || 0) + (u.cache_read || 0) + (u.cache_write || 0);
    let line = `Tokens: ↑${fmtTokens(up)} ↓${fmtTokens(u.output || 0)} · ${fmtTokens(u.total)} total`;
    if (u.cache_read > 0) line += ` (${fmtTokens(u.cache_read)} cached)`;
    if (u.context > 0) line += ` · ${fmtTokens(u.context)} ctx at share time`;
    meta.appendChild(el("div", "share-meta-sub", line));
  }
  activePane.appendChild(meta);
  renderHistory(data.items || []);
  if (!(data.items || []).length) addBubble("assistant", "_This conversation is empty._");
}

// The public read-only viewer (/share/<token>). Reuses every transcript renderer; no WebSocket,
// no composer, no sidebar — body.share-view hides them via CSS. The "Read-only snapshot" meta
// line is the only read-only cue; the stripped-down, composer-less page makes the rest
// self-evident, so there's deliberately no banner.
async function bootShareView(token) {
  document.body.classList.add("share-view");
  activate(makeRecord(null));   // set up activePane so renderHistory has somewhere to append
  try {
    const r = await fetch(`/api/share/${encodeURIComponent(token)}`);
    if (!r.ok) throw new Error("not found: " + r.status);
    renderSharedSnapshot(await r.json());
  } catch (e) {
    addBubble("error", "This shared link is no longer available — it may have been deleted by its owner, or the link is incorrect.");
  }
}

// The offline, self-contained export (app/packaging/shared_chat.py): the SAME SPA with the
// snapshot EMBEDDED in window.__LLMD_SHARED__ instead of fetched. Renders read-only with ZERO
// network — so a shared chat can live as one .html file on any static host, the agent never
// involved. (No try/catch: the data is inline, so there's nothing to fail.)
function bootSharedStatic(data) {
  document.body.classList.add("share-view");
  activate(makeRecord(null));
  renderSharedSnapshot(data);
}

// ---- boot ---------------------------------------------------------------
// ui/preview.html sets window.__LLMD_PREVIEW__ to drive the renderers with fixture data and no
// backend. In that mode we skip the live boot (sessions/history fetch + WebSocket connect) and
// expose the render entry points so the preview can exercise the real rendering paths.
if (window.__LLMD_SHARED__) {
  // Self-contained static export: the snapshot is embedded, so render it read-only with NO
  // live boot and NO network at all (this file may be opened from disk or a static host).
  bootSharedStatic(window.__LLMD_SHARED__);
} else if (window.__LLMD_PREVIEW__) {
  window.__llmd = {
    handle, bootChat, startWorking,
    renderResultsCard, renderParetoCard, renderComparisonCard, renderHarnessCompareCard,
    renderResourceStats, renderAgentSuggestions,
    renderEnvStatus, renderCapacityCard, renderReadinessCard,
    renderAcceleratorCard, renderDoeCard, renderOrchestrateCard,
    openBuilder, composeBrief,
    bootShareView, bootSharedStatic,
  };
} else if (shareTokenFromPath()) {
  // Public read-only viewer page: render the shared snapshot, no live boot / WebSocket.
  bootShareView(shareTokenFromPath());
} else {
  loadSessions();
  loadHistory();
  bootChat();
}
