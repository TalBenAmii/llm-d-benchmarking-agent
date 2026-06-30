# `app/mcp/` — standalone MCP server (stdio)

> Re-exposes the agent's tools + knowledge + workflow to *other people's* MCP clients (Claude Desktop,
> Claude Code, Cursor). Pure mechanism: reuses `app/tools` (registry / `dispatch` / `ToolContext`); the
> judgment ships as MCP resources/prompts/server-`instructions` sourced from `knowledge/` (data, never
> duplicated here). Full design of record + rationale → **`DESIGN.md`**; locked decisions →
> `docs/proposals/05-mcp-server.md` §9. Run it with `python -m app.mcp`.

## Non-negotiables specific to this folder
1. **Thin code, thick agent** — adapters + transport only, no decision logic.
2. **Reuse, don't fork** — `list_tools` mirrors `tool_definitions()`; `call_tool` → `run_tool` → the
   shared `dispatch()`. Don't re-implement validation or handlers here.
3. **The approval gate is re-homed, not removed** (`approval.py`) — `kind="command"` returns True (the
   client already prompted for the tool call); `kind="session_plan"` uses MCP `elicit_form` with a
   sentinel pass-through fallback. Never a silent auto-approve of a mutation.
4. **Security deferred to local/stdio single-user** (`DESIGN.md` §11) — acceptable only over stdio;
   revisit before any HTTP/shared transport.
5. **SDK pin:** `mcp>=1.28,<2`, low-level `mcp.server.lowlevel.Server` (not `FastMCP`, not the v2 on
   the SDK's `main` branch). camelCase `inputSchema`/`mimeType` on the `types.*` models.
6. **Additive only** — this package does NOT modify the web path; `context_factory.py` builds its own
   `ToolContext` mirroring `app/main.py:90-108` (see `DESIGN.md` §4).

## Files
```
app/mcp/
├─ __init__.py          exports build_server, main
├─ __main__.py          python -m app.mcp → main()
├─ server.py            low-level Server: list_tools/call_tool (run_tool) + stdio loop + wires resources/prompts
├─ context_factory.py   build_connection_context — one ToolContext per stdio connection
├─ approval.py          ApproveFn adapter (client-gated commands + elicit_form/sentinel for SessionPlan)
├─ events.py            EmitFn adapter → MCP log notification (best-effort) + structured log
├─ resources.py         knowledge/ → doc://knowledge/<stem> resources (+ traversal guard)
├─ prompts.py           5 workflow prompts → MCP prompts (embed the relevant knowledge/ playbooks)
├─ instructions.py      INSTRUCTIONS — server-level role/workflow nudge (trimmed from prompt.py ROLE)
├─ CLAUDE.md            this file
└─ DESIGN.md            the implementation spec / design of record
```

## Scoped tests
`tests/test_mcp_server.py` (17 hermetic tests: tool mirror/dispatch, approval re-homing, resources,
prompts, instructions wiring). Don't run the suite by hand — the merge gate runs ruff + pytest.
