# MCP server — moved to its own repo

The standalone MCP server (`llm-d-bench`) that re-exposes this agent's tools, knowledge, and
workflow to external MCP clients (Claude Code, Claude Desktop, Cursor, …) now lives in its own
repository:

**→ [github.com/TalBenAmii/llm-d-bench-mcp](https://github.com/TalBenAmii/llm-d-bench-mcp)**

Install it with one command (it clones this engine repo at latest `main`, builds a venv, and
registers the server with Claude Code):

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/TalBenAmii/llm-d-bench-mcp/main/scripts/install.sh)
```

That repo carries the full tool/prompt/resource list, manual-config block, security model, and
design of record. The server consumes this project as an editable install — the `app.*` import
surface it relies on is guarded here by `tests/test_mcp_import_surface.py`.
