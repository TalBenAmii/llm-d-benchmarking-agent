# MCP server: moved to its own repo

The standalone MCP server (`llm-d-bench`) that re-exposes this agent's tools, knowledge, and
workflow to external MCP clients (Claude Code, Claude Desktop, Cursor, …) now lives in its own
repository:

**→ [github.com/TalBenAmii/llm-d-bench-mcp](https://github.com/TalBenAmii/llm-d-bench-mcp)**

Install it with one command (it clones this engine repo at latest `main`, builds a venv, and
registers the server with Claude Code):

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/TalBenAmii/llm-d-bench-mcp/main/scripts/install.sh)
```

This engine's own `./scripts/install/install_local.sh` now sets the MCP server up by default as well
(registers `llm-d-bench`; opt out with `--no-mcp`). Conversely, the one-liner above installs the
web UI into the shared venv too (`cd <engine> && ./scripts/run.sh --open` → http://127.0.0.1:8000),
so either installer leaves you with both front-ends.

That repo carries the full tool/prompt/resource list, manual-config block, security model, and
design of record. The server consumes this project as an editable install; the `app.*` import
surface it relies on is guarded here by `tests/test_mcp_import_surface.py`.
