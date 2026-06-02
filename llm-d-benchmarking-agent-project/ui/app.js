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
const themeBtn = document.getElementById("theme-toggle");
const convList = document.getElementById("conv-list");
const newChatBtn = document.getElementById("new-chat");
const debugBtn = document.getElementById("debug-toggle");
const cmdlogList = document.getElementById("cmdlog-list");
const historyList = document.getElementById("history-list");
const historyRefresh = document.getElementById("history-refresh");
const trendMetric = document.getElementById("trend-metric");
const trendView = document.getElementById("trend-view");
const workingEl = document.getElementById("working");
const workWordEl = workingEl.querySelector(".working-word");
const workStatsEl = workingEl.querySelector(".working-stats");
const tokenChip = document.getElementById("token-total");

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

// ---- debug view (show only executed commands; persisted) ----------------
function applyDebug(on) {
  document.documentElement.setAttribute("data-debug", on ? "on" : "off");
  debugBtn.setAttribute("aria-pressed", on ? "true" : "false");
  debugBtn.title = on
    ? "Hide debug view — back to chat"
    : "Toggle debug view — show only the commands the agent executed";
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
initDebug();

let ws = null;
let busy = false;
let activeConsole = null;     // <pre> for the currently-running command's output
let currentSession = null;    // id of the chat we're attached to (null until "ready")
let switching = false;        // true while intentionally closing to switch chats
let welcomeCard = null;       // the start-of-chat suggestion-chips card, removed once a turn starts
let readyNoteTimer = null;    // defers the plain "Session ready" note so chips can supersede it
let resourcePanel = null;     // single live resource-stats panel, updated in place during a run

const toolEls = {}; // id -> details element

// "working" indicator state (spinning-hexagon status line; see helpers below)
let workTimer = null, wordTimer = null, workStart = 0, workActivity = null, workWordFixed = false;

// ---- token usage (REAL provider counts; see the `usage` event) -----------
let sessionTokens = 0;     // running SESSION total (header chip), persisted across reloads
let turnUsage = null;      // latest in-progress-turn totals (live line + per-turn footer)

// Compact token formatting: <1000 -> integer; <1M -> one-decimal k; else one-decimal M.
function fmtTokens(n) {
  n = Number(n) || 0;
  if (n < 1000) return String(Math.round(n));
  if (n < 1000000) return (n / 1000).toFixed(1) + "k";
  return (n / 1000000).toFixed(1) + "M";
}

function setSessionTokens(total) {
  sessionTokens = Number(total) || 0;
  if (!tokenChip) return;
  tokenChip.hidden = sessionTokens <= 0;
  tokenChip.textContent = "Σ " + fmtTokens(sessionTokens) + " tokens";
}

// A `usage` event (per LLM call): refresh the running turn tally (live line) + the header chip.
function onUsage(data) {
  turnUsage = data.turn || null;
  if (data.session) setSessionTokens(data.session.total);
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
  transcript.appendChild(el("div", "turn-tokens", text));
  turnUsage = null;
}

// ---- connection ---------------------------------------------------------

function connect(sid) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const qs = sid ? `?session=${encodeURIComponent(sid)}` : "";
  ws = new WebSocket(`${proto}://${location.host}/ws${qs}`);

  ws.onopen = () => setStatus("connected", "ok");
  ws.onclose = () => {
    if (switching) { switching = false; return; }   // a deliberate switch opens its own socket
    setStatus("disconnected — retrying…", "down");
    setEnabled(false);
    stopWorking();                                   // don't keep spinning while disconnected; "ready".running restarts it
    setTimeout(() => connect(currentSession), 1500); // reconnect resumes the same chat
  };
  ws.onerror = () => setStatus("connection error", "down");
  ws.onmessage = (ev) => handle(JSON.parse(ev.data));
}

function switchTo(sid) {
  switching = true;
  currentSession = sid || null;
  try { if (ws) ws.close(); } catch (e) {}
  resetTranscript();
  connect(sid || null);
}

function newChat() { switchTo(null); }
function openSession(sid) { if (sid !== currentSession) switchTo(sid); }

function resetTranscript() {
  transcript.innerHTML = "";
  for (const k in toolEls) delete toolEls[k];
  activeConsole = null;
  welcomeCard = null;          // cleared with the transcript; a new chat re-renders its own
  resourcePanel = null;
  if (readyNoteTimer) { clearTimeout(readyNoteTimer); readyNoteTimer = null; }
  clearCmdlog();
  stopWorking();
}

function clearCmdlog() {
  if (!cmdlogList) return;
  cmdlogList.innerHTML = "";
  cmdlogList.appendChild(el("div", "cmdlog-empty", "No commands executed yet."));
}

function setStatus(text, cls) {
  statusEl.textContent = text;
  statusEl.className = "status" + (cls ? " " + cls : "");
}

function setEnabled(on) {
  busy = !on;
  input.disabled = !on;
  sendBtn.disabled = !on;
  if (on) input.focus();
}

function handle(msg) {
  const { type, data } = msg;
  switch (type) {
    case "ready":
      currentSession = data.session_id;
      setEnabled(true);
      // Restore the persisted session token total so the header chip is correct on (re)connect.
      setSessionTokens((data.usage && data.usage.total) || 0);
      if (data.running) addNote("⏳ A benchmark is still running in this chat in the background. Reopen this chat once it finishes to see the results.");
      // A brand-new chat shows the welcome card with suggestion chips (a `suggestions` event
      // follows `ready`). The plain note is only a FALLBACK for when no chips arrive — defer it
      // briefly so the chips, if any, supersede it.
      else if (!data.resumed) scheduleReadyNote();
      loadSessions();
      if (data.running) startWorking("Working");   // a turn is in flight — show the live indicator
      break;
    case "history": renderHistory(data.items || [], data.commands || []); break;
    case "suggestions": renderSuggestions(data.chips || []); break;
    case "assistant_text":
      removeWelcomeCard();                          // the conversation has started — clear the chips
      addBubble("assistant", data.text);
      if (!workingEl.hidden) resumeThinking();      // between steps: back to generic cycling
      break;
    case "tool_call": startTool(data); setWorkTool(data.name); break;
    case "command": removeWelcomeCard(); onCommand(data); setWorkActivity(data.text || (data.argv || []).join(" ")); break;
    case "output": appendConsole(data.line); break;
    case "tool_result": finishTool(data); resumeThinking(); break;
    case "approval_request": addApprovalCard(data); stopWorking(); break;  // now waiting on the user, not the model
    case "error": addBubble("error", data.message); stopWorking(); break;
    case "usage": onUsage(data); break;
    case "resource_stats": renderResourceStats(data); break;
    case "done": setEnabled(true); activeConsole = null; appendTurnTokens(); clearResourceStats(); loadSessions(); loadHistory(); stopWorking(); break;
    case "pong": break;
  }
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

let trendMetricsLoaded = false;

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
  if (!trendMetric || trendMetricsLoaded || !metrics.length) return;
  for (const m of metrics) {
    const opt = document.createElement("option");
    opt.value = m;
    opt.textContent = m;
    trendMetric.appendChild(opt);
  }
  trendMetricsLoaded = true;
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
    } else {                                                  // paragraph (joins soft-wrapped lines)
      closeList();
      const para = [line]; i++;
      while (i < lines.length && !_MD_SPECIAL.some((re) => re.test(lines[i]))) { para.push(lines[i]); i++; }
      html += `<p>${mdInline(para.join("<br>"))}</p>`;
    }
  }
  closeList();
  return html;
}

function addBubble(role, text) {
  const wrap = el("div", `msg ${role}`);
  wrap.appendChild(el("div", "who", role === "user" ? "you" : role));
  if (role === "assistant") {
    // The agent writes markdown; render it. User/error text stays literal (so a user's
    // own `**` is never interpreted and errors show raw).
    const bubble = el("div", "bubble markdown");
    bubble.innerHTML = renderMarkdown(text || "");
    wrap.appendChild(bubble);
  } else {
    wrap.appendChild(el("div", "bubble", text || ""));
  }
  transcript.appendChild(wrap);
}

function addNote(text) { addBubble("assistant", text); }

// ---- start-of-chat welcome card + suggestion chips -----------------------
// On a brand-new chat the server emits a `suggestions` event right after `ready`. We show a
// welcome card with one chip per suggestion; clicking a chip sends its prompt. The plain
// "Session ready…" note is only a fallback shown when no chips arrive (or they arrive late).

// Defer the plain note briefly so a `suggestions` event (which follows `ready`) can supersede
// it. If chips render first, removeWelcomeNoteFallback cancels this; otherwise the note shows.
function scheduleReadyNote() {
  if (readyNoteTimer) clearTimeout(readyNoteTimer);
  readyNoteTimer = setTimeout(() => {
    readyNoteTimer = null;
    if (!welcomeCard) addNote("Session ready. What would you like to benchmark?");
  }, 400);
}

function renderSuggestions(chips) {
  if (!Array.isArray(chips) || !chips.length) return;
  if (readyNoteTimer) { clearTimeout(readyNoteTimer); readyNoteTimer = null; }  // chips win over the note
  removeWelcomeCard();
  const card = el("div", "welcome-card");
  card.appendChild(el("div", "welcome-heading",
    "Hi! I can help you run a benchmark — try one of these, or just describe your use case:"));
  const wrap = el("div", "welcome-chips");
  for (const chip of chips) {
    if (!chip || !chip.label || !chip.prompt) continue;
    const btn = el("button", "chip", chip.label);
    btn.type = "button";
    btn.onclick = () => { sendUserMessage(chip.prompt); };   // sendUserMessage removes the card
    wrap.appendChild(btn);
  }
  card.appendChild(wrap);
  transcript.appendChild(card);
  welcomeCard = card;
  scroll();
}

function removeWelcomeCard() {
  if (readyNoteTimer) { clearTimeout(readyNoteTimer); readyNoteTimer = null; }
  if (welcomeCard) { welcomeCard.remove(); welcomeCard = null; }
}

// ---- live resource-stats panel (backend-streamed during a run) -----------
// One panel, updated in place from each `resource_stats` event (NOT appended per event), and
// cleared on `done`. Zero agent/LLM cost — purely a backend-pushed view.
function ensureResourcePanel() {
  if (resourcePanel && resourcePanel.isConnected) return resourcePanel;
  resourcePanel = el("div", "resource-panel");
  transcript.appendChild(resourcePanel);
  return resourcePanel;
}

function renderResourceStats(data) {
  const panel = ensureResourcePanel();
  panel.innerHTML = "";
  if (data.available === false) {
    panel.appendChild(el("div", "resource-note", data.note || "live resource stats unavailable"));
    scroll();
    return;
  }
  const rows = data.rows || [];
  const head = el("div", "resource-head", `live resource usage${data.namespace ? " · " + data.namespace : ""}`);
  panel.appendChild(head);
  if (!rows.length) {
    panel.appendChild(el("div", "resource-note", "no pods reporting yet"));
    scroll();
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
  panel.appendChild(table);
  scroll();
}

function clearResourceStats() {
  if (resourcePanel) { resourcePanel.remove(); resourcePanel = null; }
}

function renderHistory(items, commands) {
  for (const it of items) {
    if (it.role === "user") addBubble("user", it.text);
    else if (it.role === "assistant") addBubble("assistant", it.text);
    else if (it.role === "tool_call") addHistoryTool(it);
    else if (it.role === "approval_decision") addDecisionCard(it);
  }
  if (commands && commands.length) {
    clearCmdlog();
    for (const c of commands) addCmdRow(c);
  }
  scroll();
}

function addHistoryTool(it) {
  const d = el("details", "tool");
  const sum = el("summary");
  sum.appendChild(el("span", "tname", it.name || "tool"));
  sum.appendChild(el("span", null, "earlier"));
  d.appendChild(sum);
  const body = el("div", "body");
  if (it.input && Object.keys(it.input).length) body.appendChild(prettyJson(it.input));
  d.appendChild(body);
  transcript.appendChild(d);
}

function startTool(data) {
  const d = el("details", "tool");
  d.open = true;
  const sum = el("summary");
  sum.appendChild(el("span", "tname", data.name));
  sum.appendChild(el("span", null, "running…"));
  d.appendChild(sum);
  const body = el("div", "body");
  if (data.input && Object.keys(data.input).length) {
    body.appendChild(prettyJson(data.input));
  }
  d.appendChild(body);
  transcript.appendChild(d);
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

// A command the agent actually executed — show it inline (so even silent read-only
// probes are visible in the tool's console) and add a row to the debug command log.
function onCommand(data) {
  consoleLine("$ " + (data.text || (data.argv || []).join(" ")), "cmd-line");
  addCmdRow(data);
}

function addCmdRow(data) {
  if (!cmdlogList) return;
  const empty = cmdlogList.querySelector(".cmdlog-empty");
  if (empty) empty.remove();
  const mutating = data.mode && data.mode !== "read_only";
  const row = el("div", "cmd-row");
  row.appendChild(el("span", "badge " + (mutating ? "mut" : "ro"), mutating ? "mutating" : "read-only"));
  row.appendChild(el("span", "cmd-text", data.text || (data.argv || []).join(" ")));
  row.appendChild(el("span", "cmd-tag", data.auto_run ? "auto" : "approved"));
  cmdlogList.appendChild(row);
  cmdlogList.scrollTop = cmdlogList.scrollHeight;
}

function finishTool(data) {
  const d = toolEls[data.id];
  if (d) {
    const sum = d.querySelector("summary span:last-child");
    if (sum) sum.textContent = "done";
    d.open = false;
  }
  // Special-case the report summary for a friendly view.
  if (data.name === "locate_and_parse_report" && data.result && data.result.summary) {
    renderReportSummary(data.result);
  } else if (d) {
    d.querySelector(".body").appendChild(prettyJson(data.result));
  }
  activeConsole = null;
}

function renderReportSummary(result) {
  const s = result.summary;
  const wrap = el("div", "msg assistant");
  wrap.appendChild(el("div", "who", "report"));
  const bubble = el("div", "bubble");
  bubble.appendChild(el("strong", null, `Benchmark results — ${s.model || "model"}`));
  const grid = el("div", "summary-grid");
  const add = (k, v) => { if (v == null) return; const c = el("div", "stat"); c.appendChild(el("div", "k", k)); c.appendChild(el("div", "v", v)); grid.appendChild(c); };
  add("requests", s.requests_total);
  add("success %", s.success_rate_pct);
  const ttft = s.latency && s.latency.ttft;
  if (ttft) add(`TTFT mean (${ttft.units || ""})`, ttft.mean);
  const tput = s.throughput && s.throughput.total_token_rate;
  if (tput) add(`tok/s (${tput.units || ""})`, tput.mean);
  bubble.appendChild(grid);
  renderReportCharts(bubble, result.charts);
  wrap.appendChild(bubble);
  transcript.appendChild(wrap);
}

// Render the per-run chart images the harness produced (served by the backend artifact
// route). `charts` is locate_and_parse_report's list of {title, session_id, path}; absent
// on the CPU-sim quickstart / guidellm, in which case we show nothing.
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
    fig.appendChild(img);
    if (c.title) fig.appendChild(el("figcaption", null, c.title));
    wrap.appendChild(fig);
  });
  if (wrap.childElementCount) bubble.appendChild(wrap);
}

function prettyJson(obj) {
  const pre = el("pre", "json");
  let s;
  try { s = JSON.stringify(obj, null, 2); } catch { s = String(obj); }
  if (s && s.length > 4000) s = s.slice(0, 4000) + "\n… (truncated)";
  pre.textContent = s;
  return pre;
}

// Build the body (heading + command/plan detail) shared by the live approval card and the
// resolved decision card replayed from history. `heading` is the card's title text.
function approvalCardBody(card, kind, payload, heading) {
  if (kind === "session_plan") {
    card.appendChild(el("h3", null, heading));
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
  const card = el("div", "card");
  approvalCardBody(card, kind, payload, kind === "session_plan" ? "Review the plan before we start" : "Approve this command ");
  const actions = el("div", "actions");
  const approve = el("button", "approve", "Approve");
  const reject = el("button", "reject", "Reject");
  actions.appendChild(approve);
  actions.appendChild(reject);
  card.appendChild(actions);
  transcript.appendChild(card);

  const resolve = (ok) => {
    ws.send(JSON.stringify({ type: "approval", request_id, approved: ok }));
    startWorking();   // the turn resumes after the user decides (approve or reject), until "done"
    approve.disabled = reject.disabled = true;
    card.appendChild(el("div", "resolved", ok ? "✓ approved" : "✗ rejected"));
  };
  approve.onclick = () => resolve(true);
  reject.onclick = () => resolve(false);
}

// A resolved approval replayed from a reopened chat: same card, no buttons, just the outcome.
function addDecisionCard(it) {
  const { kind, payload, approved } = it;
  const card = el("div", "card");
  approvalCardBody(card, kind || "command", payload || {}, kind === "session_plan" ? "Plan" : "Command ");
  card.appendChild(el("div", "resolved", approved ? "✓ approved" : "✗ rejected"));
  transcript.appendChild(card);
}

function scroll() { transcript.scrollTop = transcript.scrollHeight; }

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
  run_command: "Running command",
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
  if (!text || busy || !ws || ws.readyState !== WebSocket.OPEN) return;
  removeWelcomeCard();              // the conversation has started — clear any suggestion chips
  addBubble("user", text);
  ws.send(JSON.stringify({ type: "user_message", text }));
  setEnabled(false);
  startWorking();
  scroll();
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text || busy || !ws || ws.readyState !== WebSocket.OPEN) return;
  sendUserMessage(text);
  input.value = "";
  input.style.height = "auto";
});

input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
});
input.addEventListener("input", () => { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 200) + "px"; });

newChatBtn.addEventListener("click", newChat);

// ---- boot ---------------------------------------------------------------
loadSessions();
loadHistory();
connect(null);
