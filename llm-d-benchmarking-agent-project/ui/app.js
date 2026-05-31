// Chat UI client for the llm-d Benchmarking Assistant.
// Talks to the backend over a WebSocket. Renders chat, streamed command output, and
// Approve/Reject cards. No secrets or commands originate here.

const transcript = document.getElementById("transcript");
const statusEl = document.getElementById("status");
const form = document.getElementById("composer");
const input = document.getElementById("input");
const sendBtn = document.getElementById("send");
const themeBtn = document.getElementById("theme-toggle");

// ---- theme (dark default, light optional; persisted) --------------------
function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  // ☀ in dark mode invites switching to light; ☾ does the reverse.
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

let ws = null;
let busy = false;
let activeConsole = null; // <pre> for the currently-running command's output

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onopen = () => setStatus("connected", "ok");
  ws.onclose = () => { setStatus("disconnected — retrying…", "down"); setEnabled(false); setTimeout(connect, 1500); };
  ws.onerror = () => setStatus("connection error", "down");
  ws.onmessage = (ev) => handle(JSON.parse(ev.data));
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
    case "ready": setEnabled(true); addNote(`Session ${data.session_id} ready. What would you like to benchmark?`); break;
    case "assistant_text": addBubble("assistant", data.text); break;
    case "tool_call": startTool(data); break;
    case "output": appendConsole(data.line); break;
    case "tool_result": finishTool(data); break;
    case "approval_request": addApprovalCard(data); break;
    case "error": addBubble("error", data.message); break;
    case "done": setEnabled(true); activeConsole = null; break;
    case "pong": break;
  }
  scroll();
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

const toolEls = {}; // id -> details element

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

function appendConsole(line) {
  if (!activeConsole) return;
  activeConsole.textContent += (activeConsole.textContent ? "\n" : "") + line;
  activeConsole.scrollTop = activeConsole.scrollHeight;
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
  setEnabled(false);
  scroll();
});

input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
});
input.addEventListener("input", () => { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 160) + "px"; });

connect();
