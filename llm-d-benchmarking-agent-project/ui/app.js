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

const toolEls = {}; // id -> details element

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
  clearCmdlog();
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
      if (data.running) addNote("⏳ A benchmark is still running in this chat in the background. Reopen this chat once it finishes to see the results.");
      else if (!data.resumed) addNote("Session ready. What would you like to benchmark?");
      loadSessions();
      break;
    case "history": renderHistory(data.items || [], data.commands || []); break;
    case "assistant_text": addBubble("assistant", data.text); break;
    case "tool_call": startTool(data); break;
    case "command": onCommand(data); break;
    case "output": appendConsole(data.line); break;
    case "tool_result": finishTool(data); break;
    case "approval_request": addApprovalCard(data); break;
    case "error": addBubble("error", data.message); break;
    case "done": setEnabled(true); activeConsole = null; loadSessions(); loadHistory(); break;
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

function renderSidebar(sessions) {
  convList.innerHTML = "";
  if (!sessions.length) {
    convList.appendChild(el("div", "conv-empty", "No conversations yet."));
    return;
  }
  for (const s of sessions) {
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
    convList.appendChild(row);
  }
}

async function deleteSession(sid) {
  if (!confirm("Delete this conversation?")) return;
  try { await fetch(`/api/sessions/${encodeURIComponent(sid)}`, { method: "DELETE" }); } catch (e) {}
  if (sid === currentSession) newChat();   // start fresh if we deleted the open one
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
  wrap.appendChild(bubble);
  transcript.appendChild(wrap);
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

// ---- input --------------------------------------------------------------

form.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text || busy || !ws || ws.readyState !== WebSocket.OPEN) return;
  addBubble("user", text);
  ws.send(JSON.stringify({ type: "user_message", text }));
  input.value = "";
  input.style.height = "auto";
  setEnabled(false);
  scroll();
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
