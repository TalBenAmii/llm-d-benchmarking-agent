# DESIGN: `app/mcp/` — standalone MCP server exposing the agent's tools, knowledge, and workflow

> **Status (2026-06-30): IMPLEMENTED on branch `worktree-mcp-proposal`.** The `app/mcp/` package now
> exists and is import-checked + unit-tested (`tests/test_mcp_server.py`, 17 tests); the merge gate
> (ruff + pytest) is the authoritative green check. This doc stays the design of record; the file-level
> detail below reflects the built code, with one noted deviation in §4 (the per-connection context is
> built directly in `adapters.py` rather than via a shared `main.py` helper). It follows the
> locked decisions in `docs/history/proposals/05-mcp-server.md` §9.

Decisions this implements (from proposal 05 §9): full operator (all functional tools, incl. mutating),
judgment shipped as MCP resources + prompts + server `instructions`, **stdio** transport, one
`Session` per stdio connection, approval re-homed to the connecting client, security hardening
deferred to local/stdio single-user.

---

## 0. Confirmation of invariants

- **Thin code, thick agent** (`CLAUDE.md` rule 3). `app/mcp/` is *pure mechanism*: transport + adapters
  that reuse the existing registry/dispatch/context. No decision logic, no `if/elif` judgment. The
  judgment ships as **data** (knowledge files as resources, playbooks as prompts, role/workflow as the
  server `instructions` string).
- **Determinism at the boundaries** (rule 4). Tool args stay schema-validated: `list_tools` emits the
  exact JSON Schemas from `tool_definitions()`, and `call_tool` routes through `dispatch()`
  (`registry.py:681`), which validates against each tool's Pydantic `input_model` before any handler
  runs. The SessionPlan gate (`validate_plan`, `session_plan.py:141`) is unchanged.
- **The mutating→approval gate is the guardrail** (rule 5). The allowlist
  (`security/allowlist.yaml`) + `Allowlist.validate()` (`app/security/allowlist.py:191`) +
  `classify_shell_command()` (`app/tools/shell.py:130`) still run on every command. What changes is only *where the human approval comes
  from* (§5): the connecting client's per-tool-call permission prompt, plus optional elicitation for the
  richer SessionPlan confirmation.
- **Secrets stay in the backend** (rule 6). The server process holds env (e.g. `HF_TOKEN`); the
  connecting agent sees tool *results*, never secrets. Subprocess env scrubbing in the runner is
  untouched.
- **Read repo truth at runtime** (rule 7). Resources and the catalog read `knowledge/` and the upstream
  repos live; nothing is vendored.

No invariant is broken. The approval gate is *re-homed* (§5); security hardening is consciously deferred
and flagged (§11).

---

## 1. Goal and what ships

A standalone process, launched by an MCP client (Claude Desktop, Claude Code, Cursor) over **stdio**,
that exposes three MCP surfaces backed by the existing app:

1. **Tools** — the functional tools from `REGISTRY` (`registry.py:593`), so a connecting agent can
   probe, plan, deploy, run, orchestrate, analyze, and tear down.
2. **Resources** — every `knowledge/*.md|*.yaml` file (50 files), so a connecting agent can pull the
   same playbooks our agent reads.
3. **Prompts** — a small set of workflow prompts (interview → plan → run → explain, interpret-report,
   design-a-sweep, goal-seek-to-SLO) that inject the relevant playbook + the workflow shape.

Plus the server-level `instructions` string (role + workflow), advertised at `initialize`, so even a
client that never fetches a resource inherits the basic "how this agent behaves" shape.

The product goal, in one line: **let a generic agent behave like our benchmark agent.** The tools are
its hands; the resources/prompts/instructions are the nudge toward our judgment.

---

## 2. Package layout (`app/mcp/`)

| File | Responsibility |
|---|---|
| `__init__.py` | exports `main`; package docstring |
| `__main__.py` | `python -m app.mcp` → `main()` |
| `server.py` | builds the low-level `Server`, registers all six handlers, runs the stdio loop (`main`) |
| `adapters.py` | the per-connection adapters: stands up one `ToolContext` + `Session` per connection without the web loop and wires the approval/emit adapters (`build_connection_context`); the `ApproveFn` adapter (client-gated commands + elicitation/sentinel for SessionPlan); the `EmitFn` adapter → MCP logging notifications (best-effort) + structured log |
| `content.py` | the knowledge-exposure surface: `knowledge/` → MCP resources (list/read), `doc://knowledge/<stem>` scheme; playbooks → MCP prompts (list/get); the server `instructions` string (reuses `ROLE` from `app/agent/prompt.py`, trimmed of web-UI references, + an MCP workflow preamble) |
| `CLAUDE.md` | scoped map of this package (file-level detail) |
| `DESIGN.md` | this spec |

All judgment-bearing text lives in `knowledge/` (data) and is *referenced*, not duplicated, by
`content.py`.

---

## 3. Tool surface (`server.py`)

**`list_tools` mirrors `tool_definitions()`.** One mapping detail: our dicts use the snake_case key
`input_schema` (`registry.py:662-678`); MCP `types.Tool` wants camelCase `inputSchema`. Translate per
tool.

```python
import mcp.types as types
from app.tools.registry import tool_definitions

@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(name=d["name"], description=d["description"], inputSchema=d["input_schema"])
        for d in tool_definitions(loaded=_EXPOSED)   # see meta-tool note below
    ]
```

**`call_tool` routes through `dispatch()` and mirrors `loop._invoke`'s error handling**
(`loop.py:440-442`). `dispatch()` (`registry.py:681`) already (a) returns `{"error": ...}` dicts for
unknown-tool / invalid-args instead of raising, and (b) lets a handler raise `ApprovalRejected`
(`context.py:27`). We catch that one exception, exactly as the web loop does:

```python
import json
from app.tools.registry import dispatch
from app.tools.context import ApprovalRejected

@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.ContentBlock]:
    ctx = _connection_ctx()                      # the per-connection ToolContext (§4)
    ctx.current_tool_call_id = _new_call_id()
    try:
        result = await dispatch(ctx, name, arguments or {})
    except ApprovalRejected as exc:
        result = {"rejected": True, "reason": str(exc)}
    return [types.TextContent(type="text", text=json.dumps(result, default=str))]
```

Optionally return the `(content, structured_dict)` tuple form so clients that support structured tool
output get the raw dict too; the `TextContent` JSON is the floor every client understands.

**Meta-tool adaptation.** Two of the 36 are web-loop-only:
1. `load_tools` — its only purpose is lazy reveal of tool groups to protect the web agent's *cached
   prompt prefix* (`registry.py` `_TOOL_GROUPS`, `STARTER_KIT` at `:618-651`). An MCP client manages its
   own tool list, so **drop it**: `_EXPOSED` is the full grouped set, flat. (If a future client proves
   to choke on 35 tools, revisit by mapping groups to `tools/list_changed`; out of scope for v1.)
2. `suggest_next_steps` — **keep it**, but its output is plain structured suggestions the connecting
   agent can render however it likes (no UI buttons). No code change to the tool; it already returns a
   dict.

So `_EXPOSED` = all of `REGISTRY` minus `load_tools` (35 tools). Document this in `CLAUDE.md`.

---

## 4. Per-connection context (`adapters.py`)

A stdio server process serves exactly **one** client connection, so "one `Session` per connection"
(decision 05 §9.5) collapses to **one `ToolContext` + `Session` per process**, built lazily on first use
and reused for the process lifetime. This gives the operator flow (propose plan → run → analyze) a
shared `workspace/` + run registry across calls, for free.

The web path builds a `Session`/`ToolContext` in `SessionManager` (`app/main.py`) and wires `emit`/
`request_approval` at `run_turn` time (`loop.py:74-85`). We build the same `ToolContext`
(`context.py:55-98`) directly, wiring **our** adapters instead of the loop's:

```python
from app.config import Settings
from app.tools.context import ToolContext

def build_connection_context(settings: Settings) -> ToolContext:
    ctx = ToolContext(
        settings=settings,
        allowlist=load_allowlist(settings),        # same loader main.py uses
        runner=CommandRunner(settings),            # app/security/runner.py
        workspace=settings.workspace_dir / "mcp" / _session_id,
        run_semaphore=asyncio.Semaphore(settings.max_concurrent_runs),
        runs=new_run_registry(),                   # same registry type main.py builds
        quota=QuotaCounter(),
        session_id=_session_id,
    )
    ctx.request_approval = make_approval_fn(app)   # §5
    ctx.emit = make_emit_fn(app)                   # §6
    ctx.catalog()                                  # pre-warm the live catalog (context.py:99)
    return ctx
```

**Decision taken (deviation from the original plan).** Rather than refactor `app/main.py`'s startup to
share a `build_context(...)` helper (which would touch the web path with no local way to run the suite
and verify it), `adapters.build_connection_context` constructs the deps directly — mirroring
`app/main.py:90-108` (`Allowlist.from_file` / `CommandRunner(repo_paths, extra_env=…)` /
`asyncio.Semaphore(max_concurrent_runs)` / `RunRegistry()`) with a pointer comment. The drift surface is
~5 lines and a later refactor can still extract the shared helper. The benefit: the change stayed purely
additive — **no existing runtime code was modified**, so the web path is untouched and the merge gate
only has the new package + tests to validate.

The `Session` object (`session.py:94-175`) is optional for v1 — most fields (`card_results`,
`approvals`, token counters, `namespace`) are web-UI bookkeeping. We need only the `ctx`. Hold a module
- or app-state-level singleton `ToolContext`; create a thin `Session` only if a tool reads
`session.approved_plan` (today it is used for sidebar namespace inference only, so likely unneeded).
Confirm during build; if any exposed tool reads `Session`, attach a minimal one.

---

## 5. Approval adapter (`adapters.py`)

`ToolContext.request_approval` has type `ApproveFn = Callable[[str, dict], Awaitable[bool]]`
(`context.py:51`), called with `kind ∈ {"command", "session_plan"}`. The adapter:

**`kind == "command"` → return `True` (client already gated the call).** Every `tools/call` is
independently prompted by the connecting client's permission system before the handler runs, so by the
time a handler reaches `ctx.run_command()`, the user has already allowed *this tool invocation*. We
treat that as the approval and return `True`. This is the "works freely like a normal local agent"
decision — **not** a silent auto-approve, because the human checkpoint is the client's per-call prompt,
and a single tool call maps to a single user permission. (A tool that internally runs several commands
still corresponds to one user-approved invocation, which is the right granularity.)

**`kind == "session_plan"` → elicitation, sentinel fallback.** The plan is *inert* (it mutates
nothing), so the safety here is confirmation quality, not gating a mutation. Where the client advertises
the `elicitation` capability, ask explicitly; otherwise pass through (every downstream *mutating* tool
call is still independently client-gated):

```python
async def request_approval(kind: str, payload: dict) -> bool:
    if kind == "command":
        return True
    # kind == "session_plan"
    if not _client_supports_elicitation(app):
        return True                                  # sentinel: plan returned in tool result; mutations stay client-gated
    res = await app.request_context.session.elicit_form(
        message=_render_plan_for_confirm(payload),   # human-readable plan summary
        requestedSchema={
            "type": "object",
            "properties": {"approve": {"type": "boolean", "title": "Approve this benchmark plan?"}},
            "required": ["approve"],
        },
    )
    return res.action == "accept" and bool((res.content or {}).get("approve"))
```

Notes from the SDK (verified against `mcp` 1.28.1):
- The session is reached via `app.request_context.session` *inside* a handler (lowlevel `server.py`).
- Use `elicit_form(message, requestedSchema)`; the bare `session.elicit(...)` is deprecated in 1.28.1.
- `requestedSchema` must be a **flat object of primitives** (string/number/integer/boolean/enum) — no
  nesting. A single boolean `approve` fits.
- `ElicitResult.action ∈ {"accept","decline","cancel"}`; `content` is populated only on `accept`.
- `_client_supports_elicitation` checks the client capability captured at `initialize`; on any
  elicitation error, degrade to the sentinel path (treat as unsupported) so an older client never
  hard-fails a plan proposal.

The allowlist + classifier still execute on every command regardless of `kind` handling; the adapter
only decides the human-gate question, never whether a command is *allowed*.

---

## 6. Event adapter (`adapters.py`)

`EmitFn = Callable[[str, dict], Awaitable[None]]` (`context.py:52`) feeds the web UI's live event
stream (streaming output, cards). MCP has no equivalent rich surface. Map best-effort:

- Forward to the connecting client as an MCP **logging notification**
  (`app.request_context.session.send_log_message(...)`) when a logging level is set, so a curious client
  can watch progress.
- Always write to the existing structured logger (`app/observability/`), so the server has its own trail
  even when the client ignores logs.

`emit` must never raise into a handler; wrap in try/except and swallow (a dropped progress line is not a
tool failure).

---

## 7. Resources (`content.py`)

Publish every knowledge file as an MCP resource. Source of truth is the same glob the prompt builder
uses (`_knowledge_sections`, `prompt.py:293-333`): `knowledge/*.md|*.yaml|*.yml`, minus
`EXCLUDED_KNOWLEDGE_FILES` (`CLAUDE.md`, `CONTEXT.md`). 50 files today.

```python
from pydantic import AnyUrl
from mcp.server.lowlevel.helper_types import ReadResourceContents

_SCHEME = "doc"   # doc://knowledge/<stem>

@app.list_resources()
async def list_resources() -> list[types.Resource]:
    return [
        types.Resource(
            uri=AnyUrl(f"{_SCHEME}://knowledge/{p.stem}"),
            name=p.stem,
            description=_first_heading(p),                  # reuse prompt.py's one-line purpose extractor
            mimeType="text/markdown" if p.suffix == ".md" else "application/yaml",
        )
        for p in _knowledge_files(settings.knowledge_dir)   # config.py:260
    ]

@app.read_resource()
async def read_resource(uri: AnyUrl) -> list[ReadResourceContents]:
    path = _resolve_knowledge_uri(uri, settings.knowledge_dir)   # reject path escapes
    text = path.read_text(encoding="utf-8")
    mime = "text/markdown" if path.suffix == ".md" else "application/yaml"
    return [ReadResourceContents(content=text, mime_type=mime)]
```

`_resolve_knowledge_uri` must reject any URI that resolves outside `knowledge/` (path-traversal guard;
read-only, but still). Declaring `@list_resources()` is what advertises the `resources` capability.

Later (out of v1 scope): also expose the upstream repo docs catalogued in `knowledge/key_docs.yaml`
under a `repo://` scheme. Noted, not built.

---

## 8. Prompts (`content.py`)

A small set of workflow prompts, each returning messages that embed the relevant playbook content + the
workflow directive. These are the user-invokable "slash commands" a client surfaces. The playbooks live
in `knowledge/` (per the internal map): `quickstart_playbook.md`, `deploy_path_playbook.md`,
`sweep_playbook.md`, `results_interpretation.md`, `orchestrator.md`,
`welllit_path_advisor.yaml`, `conversation_style.md`.

| Prompt name | Arguments | Returns (message content) |
|---|---|---|
| `benchmark_this_model` | `model`, `goal?`, `slo?` | the interview→preconditions→plan→run→explain workflow + `quickstart_playbook.md` + `welllit_path_advisor.yaml` |
| `pick_deploy_path` | `model?`, `accelerator?` | `deploy_path_playbook.md` + `welllit_path_advisor.yaml` |
| `interpret_this_report` | `report_path?` | `results_interpretation.md` + `analysis.md`, directing use of `analyze_results`/`locate_and_parse_report` |
| `design_a_sweep` | `objective?` | `sweep_playbook.md`, directing `generate_doe_experiment`/`orchestrate_sweep` |
| `goal_seek_to_slo` | `slo` | `sweep_playbook.md` (its goal-seeking section), directing iterative sweep rounds + `analyze_results` |

```python
@app.get_prompt()
async def get_prompt(name: str, arguments: dict[str, str] | None = None) -> types.GetPromptResult:
    spec = _PROMPTS[name]                              # static table: title, args, playbook files, directive
    body = spec.directive.format(**(arguments or {})) + "\n\n" + _load_playbooks(spec.playbooks)
    return types.GetPromptResult(
        description=spec.description,
        messages=[types.PromptMessage(role="user", content=types.TextContent(type="text", text=body))],
    )
```

`list_prompts` returns the table as `types.Prompt(name, description, arguments=[PromptArgument(...)])`.
The directive text is the only new prose; the substance is loaded from `knowledge/` so it cannot drift.

---

## 9. Server instructions (`content.py`)

Advertised once at `initialize`; many clients fold it into their system prompt. Assemble from the
existing `ROLE` constant in `app/agent/prompt.py` (reuse, don't duplicate), stripped of web-UI specifics
(approval cards, sidebar, WebSocket), plus a short MCP workflow preamble:

```
You drive llm-d benchmarking for a non-expert. Workflow: interview to understand the goal →
probe the environment and check preconditions → propose a SessionPlan and get it approved →
run (locally or orchestrated) → explain results from the validated Benchmark Report, never from
logs. Prefer the tools on this server; read the doc://knowledge/* resources for judgment
(which spec/harness/workload, how to read SLO verdicts, deploy-path choice). Mutations are gated:
the user approves each tool call in your client.
```

Set via `Server("llm-d-bench", instructions=INSTRUCTIONS)` (constructor arg, lowlevel
`server.py`); `create_initialization_options()` folds it in automatically.

---

## 10. Entrypoint and packaging (`__main__.py`, `pyproject.toml`)

```python
# server.py
import anyio
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

def build_server(settings) -> Server:
    app = Server("llm-d-bench", instructions=INSTRUCTIONS)
    # register the six handlers (§3, §7, §8) bound to a per-process ToolContext (§4)
    return app

def main() -> None:
    settings = Settings()                  # app/config.py
    app = build_server(settings)
    async def arun():
        async with stdio_server() as (read, write):
            await app.run(read, write, app.create_initialization_options())
    anyio.run(arun)
```

- `app/mcp/__main__.py`: `from app.mcp.server import main; main()`.
- `pyproject.toml`: add dependency `mcp>=1.28,<2` (pin away from the in-development v2 on `main`; v2 has
  an incompatible constructor-callback API). Project already targets Python ≥3.10, which `mcp` requires.
- `[project.scripts]`: `llm-d-bench-mcp = "app.mcp.server:main"`.
- Client registration (Claude Desktop `claude_desktop_config.json` / Claude Code `.mcp.json`), identical
  stdio block:

```json
{ "mcpServers": { "llm-d-bench": {
    "command": "python", "args": ["-m", "app.mcp"],
    "env": { "HF_TOKEN": "${HF_TOKEN}" }
} } }
```

Secrets go in `env` (expanded at launch), never in `args`.

---

## 11. Security posture (deferred, on the record)

Per decision 05 §9.6 / §8, v1 targets **local single-user / stdio**:
- The server acts with the user's own kubeconfig; it is trusted like any local agent the user runs.
- No connection authn/authz, no per-caller credential scoping, no network listener (stdio only).
- Still enforced (free, pure): the allowlist + mutating classifier on every command; subprocess env
  scrubbing; the read-only path for read-only tools.
- The human gate is the connecting client's per-tool-call permission prompt (§5).

**This is acceptable ONLY for local/stdio use. Before any HTTP / shared / remote transport, "who may
connect, whose credentials, what is the blast radius" become blocking, not deferred.** Flagged loudly so
the deferral stays a choice.

---

## 12. Tests (`tests/test_mcp_server.py`, hermetic — no live cluster, no LLM, no network)

> BUILT: 17 tests covering the areas below (flat file, matching the house `tests/test_*.py`
> convention rather than a `tests/mcp/` subdir). All logic was validated by direct calls against the
> installed `mcp` 1.28.0; the merge gate runs them under the full suite.

1. `test_list_tools_mirrors_registry` — `list_tools()` returns one `types.Tool` per `_EXPOSED` tool;
   `inputSchema` equals each tool's `tool_definitions()` `input_schema`; `load_tools` absent.
2. `test_call_tool_dispatches` — a read-only tool (e.g. `list_catalog`) round-trips through `call_tool`
   and returns JSON `TextContent` matching a direct `dispatch()` call.
3. `test_call_tool_invalid_args` — bad args yield `dispatch()`'s `{"error": "invalid arguments", ...}`
   surfaced as content, no exception.
4. `test_command_approval_autotrue` — the command-`kind` adapter returns `True` without touching the
   client (a mutating command under a fake runner proceeds).
5. `test_session_plan_elicit_accept` / `_decline` — with a fake `session.elicit_form` returning
   accept/decline, `request_approval("session_plan", ...)` returns True/False.
6. `test_session_plan_sentinel_fallback` — with elicitation capability absent, returns `True` and does
   not call `elicit_form`.
7. `test_list_resources_matches_knowledge_glob` — resource names equal the non-excluded `knowledge/`
   glob; `read_resource` returns file contents; a traversal URI is rejected.
8. `test_list_prompts_and_get_prompt` — every prompt in the table lists; `get_prompt` embeds the named
   playbook text + directive.
9. `test_instructions_present` — `create_initialization_options().instructions` is non-empty and free of
   web-UI terms (no "card", "sidebar", "websocket").

Use the `mcp` test helpers / an in-memory stream pair where useful; otherwise call the handler functions
directly with a fake `request_context`. Keep the suite in the ~14s hermetic budget (`tests/CLAUDE.md`);
`conftest` already forces `SIMULATE=0`.

---

## 13. Out of scope for v1 (noted, not built)

1. HTTP / streamable transport and the authz it forces (§11).
2. Multi-connection / multi-tenant session mapping (stdio = one connection, §4).
3. Repo-doc resources under `repo://` (§7).
4. Group-based lazy tool reveal via `tools/list_changed` (§3 meta-tool note).
5. Rich event surface (cards, command-trail streaming) — only best-effort MCP logging (§6).

---

## 14. Build order (status)

1. ✅ `mcp>=1.28,<2` dependency + `app/mcp/` skeleton (`__init__`, `__main__`, `server.main`).
2. ✅ `adapters.build_connection_context` (§4) — built directly, not via a `main.py` refactor.
3. ✅ Tools: `list_tools` + `call_tool` (`run_tool`) (§3); `command`-kind approval returns True.
4. ✅ Resources (§7) + path-traversal guard (whitelist by stem).
5. ✅ Prompts (§8) + server `instructions` (§9).
6. ✅ SessionPlan elicitation + sentinel fallback (§5).
7. ✅ Event adapter (§6).
8. ✅ Tests (§12), logic-verified by direct call. ⬜ Manual smoke via MCP Inspector / Claude Code over
   stdio — recommended before relying on it against a real client.
9. ⬜ Finish loop (review → commit → `--no-ff` merge, which runs the ruff+pytest gate) — the merge is
   the green check, per the standing rule.
