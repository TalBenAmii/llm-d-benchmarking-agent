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
      if (!data.resumed) addNote("Session ready. What would you like to benchmark?");
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
    case "done": setEnabled(true); activeConsole = null; loadSessions(); break;
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

// ---- rendering ----------------------------------------------------------

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function addBubble(role, text) {
  const wrap = el("div", `msg ${role}`);
  wrap.appendChild(el("div", "who", role === "user" ? "you" : role));
  wrap.appendChild(el("div", "bubble", text || ""));
  transcript.appendChild(wrap);
}

function addNote(text) { addBubble("assistant", text); }

function renderHistory(items, commands) {
  for (const it of items) {
    if (it.role === "user") addBubble("user", it.text);
    else if (it.role === "assistant") addBubble("assistant", it.text);
    else if (it.role === "tool_call") addHistoryTool(it);
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

function addApprovalCard(data) {
  const { request_id, kind, payload } = data;
  const card = el("div", "card");
  if (kind === "session_plan") {
    card.appendChild(el("h3", null, "Review the plan before we start"));
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
    h.appendChild(el("span", null, "Approve this command "));
    h.appendChild(el("span", "badge mut", "mutating"));
    card.appendChild(h);
    card.appendChild(el("div", "cmd", payload.command || (payload.argv || []).join(" ")));
  }
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
input.addEventListener("input", () => { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 160) + "px"; });

newChatBtn.addEventListener("click", newChat);

// ---- boot ---------------------------------------------------------------
loadSessions();
connect(null);
