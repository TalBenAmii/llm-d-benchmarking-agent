# SPEC: MCP server — expose the agent's tools to other people's agents

> _Historical proposal. Built 2026-06-30 as `app/mcp/`, then **split to its own repo on 2026-07-05**
> → [github.com/TalBenAmii/llm-d-bench-mcp](https://github.com/TalBenAmii/llm-d-bench-mcp), where the
> design of record now lives. Kept here as the original decision record._

> **Status (2026-06-30): decisions locked, not yet built.** The six forks this doc raised are now
> decided (§9). What remains is to turn them into a file-level implementation spec in the style of the
> four shipped siblings, then code. Headline: build the **full operator** (all 38 tools, including the
> mutating ones) **plus the judgment layer**, over **stdio**, leaning on the connecting MCP client's
> *own* tool-permission prompt as the human-in-the-loop. **Cluster/security hardening is deliberately
> deferred** (§8) — the v1 target is "runs like any local agent the user already trusts with their own
> kubeconfig," not a hardened multi-tenant service.

## 0. Confirmation of invariants

An MCP server stresses two of the project's invariants in ways the web app never did. Naming them up
front:

- **Thin code, thick agent** (`CLAUDE.md` rule 3). The load-bearing one. MCP exposes *mechanism* (the
  38 tools). The *judgment* (`knowledge/` + the assembled system prompt) does **not** travel with a
  tool call by default; a connecting agent brings its own reasoning. The decision (§4) is to ship the
  judgment too, because nudging a generic agent into our workflow is the entire point of the product.
- **Determinism at the boundaries** (rule 4). Helps us: tool args are already Pydantic, so they map
  1:1 to MCP tool JSON Schemas (§2). Stresses us: the SessionPlan approval gate is a boundary that
  assumed a human at a browser; an MCP caller is an *agent* fronting a human at a different UI (§3).
- **The mutating→approval gate is the guardrail** (rule 5). The allowlist (`security/allowlist.yaml`,
  DATA) and its pure validator port unchanged. What is re-homed is *where the human approval comes
  from*: the connecting client's tool-permission prompt instead of our card (§3).
- **Secrets stay in the backend** (rule 6) and **read repo truth at runtime** (rule 7). Both hold as
  in the web app. The new wrinkle — whose cluster/kubeconfig the server acts against — is the security
  question we are explicitly deferring for v1 (§8).

No invariant is broken. One (the approval gate) is *re-homed* to the client, and one trade-off
(security hardening) is consciously postponed and put on the record.

---

## 1. The framing question: which "same thing"?

"An MCP that does the same thing as the benchmarking agent" resolves to one of three products:

1. **The advisory half.** Capacity/accelerator pre-flight, report analysis, run comparison, workload
   inspection, knowledge retrieval, environment sensing. All read-only, no approval gate.
2. **The full operator.** The above plus deploy a stack, run a benchmark, orchestrate a K8s Job, tear
   it down. This is where the mutating gate lives.
3. **The full operator, with the judgment.** #2 plus a way for the connecting agent to import the
   playbooks (which spec to pick, how to read an SLO verdict) so it behaves like *this* agent rather
   than a generic LLM holding a pile of tools.

**Decided: product #2 and #3 together — full operator with the judgment shipped alongside.** The
security weight of #2 is deferred for v1 (§8), and #3 is treated as *core* rather than optional,
because the goal is to nudge the connecting agent into behaving like this benchmark agent, which is
exactly what the resources/prompts carry.

---

## 2. What already exists (the easy 80%)

The tool layer is already shaped for this; very little is net-new mechanism.

- **38 tools, Pydantic I/O.** `app/tools/registry.py` builds `REGISTRY` from 38 `ToolSpec` entries
  (`build_registry()`, `registry.py:544`); `tool_definitions()` (`registry.py:662`) already emits the
  model-facing JSON Schema list, which is structurally an MCP `tools/list` response. The translation is
  a re-serialization, not a redesign.
- **An in-process MCP server already exists in the codebase.** `app/llm/agent_sdk_provider.py:240-246`
  calls `create_sdk_mcp_server(...)` and presents every tool as `mcp__{server}__{tool_name}`
  (`agent_sdk_provider.py:42-45`). The project already speaks MCP internally for the Claude Agent SDK
  path; this proposal points that same surface *outward* over a real stdio transport. Note the existing
  shim wires `can_use_tool=_deny_tool` (`:75`, `:268`) because the host app runs the handlers under its
  own gate; an external server makes the opposite choice and actually executes, which is what §3 is about.
- **Handlers are decoupled from the loop.** Each handler takes a `ToolContext` DI container
  (`app/tools/context.py`), not the `AgentLoop`. Read-only tools touch only `workspace` + `catalog()` +
  the runner's read-only path; they have no dependency on the agent loop or the browser.
- **The allowlist is pure DATA + a pure validator.** `security/allowlist.yaml` and the read-only/
  mutating classifier port verbatim; classification works identically whoever the caller is.

The upshot: the advisory tools are nearly a transport adapter over `tool_definitions()` + the existing
handlers + workspace + allowlist + catalog. The operator tools add the approval re-homing of §3.

---

## 3. The approval gate, re-homed to the client

Today there are two approval points, both routed through `ctx.request_approval(...)`, both rendered as
a browser card the user clicks:

| Gate | Trigger | Path |
|---|---|---|
| **SessionPlan** | `propose_session_plan` | validate against live catalog (`app/validation/session_plan.py`), then `ctx.request_approval("session_plan", plan)` |
| **Mutating command** | any handler calling `ctx.run_command()` | allowlist classify → if mutating, `ctx.request_approval("command", …)`; rejection raises `ApprovalRejected` |

An MCP server has tools, resources, and prompts, but no window of its own, so "show the user an Approve
card" has no native home. The decision:

**Primary human-in-the-loop = the connecting client's own tool-call permission prompt.** An MCP client
(Claude Desktop, Claude Code, Cursor) already asks its user before invoking a server tool, so the agent
"works freely like a normal agent the user runs": the MCP adapter wires `ctx.request_approval` to treat
the client's invocation as the approval rather than rendering our card. **This is not auto-approve** —
the human still approves, just at the client layer instead of ours. The guardrail moves; it does not
vanish.

**Structured plan approval = elicitation with a sentinel fallback (B→C).** On top of the per-call client
prompt, the richer **SessionPlan** confirmation uses MCP's *elicitation* primitive where the client
supports it (the server asks the user to confirm the resolved plan mid-call). Where the client does not
support elicitation (uneven across clients as of early 2026), the server falls back to returning the
validated plan as a structured **sentinel** for the agent to surface and re-confirm. Never a silent
auto-approve of a SessionPlan.

The allowlist + mutating classifier still run on every command (they are pure and free to keep); they
just no longer raise into a browser card. Cluster blast-radius hardening that would normally accompany
an operator gate is deferred (§8).

---

## 4. The judgment layer — shipped, and treated as core

The agent is good because of `knowledge/` (~40 md/yaml files) and the assembled system prompt, not the
tools. A connecting agent that calls our tools but reasons with its own generic prompt will pick specs
badly, misread SLO verdicts, and skip preconditions. So the judgment is the product, not a nice-to-have.

**Decision: ship it, as core.** Concretely, three vehicles, strongest nudge MCP affords:

1. **Resources** — publish `knowledge/*` as readable MCP resources, mirroring the in-app
   `read_knowledge` tool, so a connecting agent can pull the relevant playbook on demand.
2. **Prompts** — publish the interview / precondition / plan / analyze playbooks as parameterized MCP
   prompt templates a client can surface as slash-commands ("benchmark this model", "interpret this
   report").
3. **Server `instructions`** — put the role + workflow summary in the MCP server's initialize-time
   `instructions` field (many clients fold this into their system prompt), so even a client that never
   fetches a resource still inherits the basic shape of "interview → check preconditions → propose a
   plan → run → explain."

All three are best-effort: a client can ignore any of them, and the nudge is advisory text the caller's
model may or may not follow. It is strictly weaker than the in-app guarantee (where the judgment is a
byte-stable cached prefix the model cannot skip), but it is the most a server can do over MCP, and it is
the whole reason this product exists rather than a bare tools dump.

---

## 5. State and session model

The web app is stateful per session (a `workspace/` dir, a transcript, a run registry, namespace
grouping). MCP tool calls are independent by default. Because v1 is the **operator**, multi-call flows
(propose plan → run → analyze) must share one workspace + run registry across calls.

**Decision (derived from scope): map an MCP connection to one of our `Session` objects.** A connection
gets a `Session` (and its `workspace/` + run registry), so a plan proposed on one call is the plan run
on the next and analyzed on the third, exactly as in the web app. The simpler shared-root and stateless
`workspace_path` models are rejected for v1 because the operator flow needs cross-call correlation they
cannot give. (For a hypothetical advisory-only build the stateless arg would have sufficed; that is not
what we are building.)

---

## 6. Phasing and build order

| Phase | Scope | Approval | State | Effort | In v1? |
|---|---|---|---|---|---|
| **1 — Advisory** | ~11–14 read-only tools | none | n/a | Low | yes (built first to de-risk transport) |
| **2 — Operator** | + mutating (deploy/run/orchestrate/teardown) | client prompt + elicitation/sentinel (§3) | Session-per-connection (§5) | Med | yes |
| **3 — Judgment** | knowledge as resources, playbooks as prompts, role in `instructions` | n/a | n/a | Med | yes (core, §4) |

**Decided plan: build 1+2+3 together as the v1 target** (full operator with judgment), with the
security column of Phase 2 explicitly deferred (§8). Internal build order still goes read-only surface
first (cheapest to stand up, de-risks the stdio transport), then the mutating set wired to the
client-mediated approval of §3, then the resources/prompts/`instructions` of §4. Phase 3 is promoted
from "optional polish" to "core."

---

## 7. Transport and packaging

- **Additive, not a replacement.** The MCP server is a separate entrypoint alongside the FastAPI app,
  not a rewrite of `/ws`. The web UI, its approval cards, history, and result cards are untouched.
- **Transport: stdio only** (decision §9.4). Local clients — Claude Desktop, a local Claude Code,
  Cursor. No network surface in v1, which also keeps the deferred-security posture (§8) honest: there is
  nothing remote to attack. HTTP is a later, separate decision.
- Reuse `tool_definitions()` for `tools/list` and the existing `REGISTRY` dispatch for `tools/call`.
- **Secrets and creds stay backend** (rule 6). The connecting agent sees tool results, never keys. The
  server acts with whatever kubeconfig it is configured with (see §8).

---

## 8. Risks, non-goals, and the deferred security decision

- **Non-goal: replacing the chat UI.** The browser approval flow remains the gold-standard path; MCP is
  for reuse elsewhere, not a migration.
- **Lost affordances under MCP.** No transcript, no result cards, no command audit stream, no
  disconnect-surviving background runs unless the connecting client rebuilds them. Acceptable for a
  local single-user operator; a real gap if this ever becomes a shared service.
- **Security — deferred for v1, deliberately and on the record.** An operator-grade MCP server is a
  process that runs `kubectl`/`helm`/the CLI against a real cluster on behalf of whatever agent connects
  to it. The v1 decision is to target the **local single-user** model: it runs on the user's machine,
  acts with the user's own kubeconfig, and is trusted exactly as much as any agent that user already
  runs locally. No connection authn/authz, no per-caller credential scoping, no network exposure (stdio
  only reinforces this). The allowlist + mutating classifier still run, but the human gate is the
  client's tool prompt, not a hardened server policy. **This is acceptable ONLY for local/stdio use and
  MUST be revisited before any HTTP / shared / remote deployment** — at which point "who may connect,
  whose creds, what is the blast radius" become blocking questions rather than deferred ones. Flagged
  loudly here so the deferral is a choice, not an oversight.

---

## 9. Decisions (locked 2026-06-30)

Four were the user's explicit calls; two (state, trust) follow from scope and are recorded as defaults
the user can still override.

| # | Decision | Choice | Source |
|---|---|---|---|
| 1 | **v1 scope** | **Full operator** — all 38 tools incl. mutating (Phase 1+2), *plus* the judgment layer (Phase 3). Security weight deferred. | user |
| 2 | **Approval** | Primary gate = the connecting **client's own tool-permission prompt** ("works freely like a normal local agent"); structured SessionPlan approval via **elicitation + sentinel fallback (B→C)**; never a silent auto-approve. | user |
| 3 | **Judgment layer** | **Ship it, as core** — `knowledge/` as MCP resources, playbooks as MCP prompts, role/workflow in the server `instructions`. The product's whole point: nudge a generic agent to act like this one. | user |
| 4 | **Transport** | **stdio only** — local clients (Claude Desktop / Claude Code / Cursor). No network surface in v1. | user |
| 5 | **State model** | **MCP connection ↔ `Session` mapping** — operator flows (propose plan → run → analyze) share one workspace + run registry across calls, which the stateless models cannot give. | derived from scope |
| 6 | **Distribution / trust** | **Local single-user, hardening deferred** (§8) — server acts with the user's own kubeconfig, trusted like any local agent; revisit before any non-stdio deployment. | derived + user "defer security" |

**Built (2026-06-30), then split out (2026-07-05):** first shipped as `app/mcp/`, the server now
lives in its own repo — **[llm-d-bench-mcp](https://github.com/TalBenAmii/llm-d-bench-mcp)** — which
consumes this project as its engine (the design of record moved there too). It reuses
`tool_definitions()` + `dispatch()` for the exposed tools, re-homes approval onto the connecting
client (command) with `elicit_form` + sentinel for the SessionPlan, ships `knowledge/` as
`doc://knowledge/*` resources + 5 workflow prompts + the server `instructions`, and is covered by that
repo's hermetic tests. This project keeps only an import-surface guard
(`tests/test_mcp_import_surface.py`) so a refactor here can't silently break the external adapter.
