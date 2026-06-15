# Developer plugins (Claude Code)

> **NOTE (2026-06): all 14 plugins below were UNINSTALLED in commit `394130c`
> (`chore(plugins): uninstall all Claude Code dev plugins`, Jun 13).** That commit fully
> removed them (registry, on-disk cache, and data dirs — not just disabled) and deleted the
> `enabledPlugins` block; **`.claude/settings.json` now has an empty `enabledPlugins: {}`** and
> no `settings*.json` references any of these plugins. **This table is historical/reference
> only** — a record of what was tried and what could be re-enabled, **not** a description of the
> current state. The "Status in this env" column describes how each plugin behaved *while it was
> enabled* (commit `6659817` → `394130c`).

These are **developer-environment** plugins for working *on* this repo with Claude Code — not
agent runtime features and not application dependencies. While enabled, they were project-scoped
in the repo-root **`.claude/settings.json`** under `enabledPlugins`, all from the
`claude-plugins-official` marketplace (`anthropics/claude-plugins-official`).

> **Two caveats that applied while these were enabled (kept for reference if re-enabling)**
> 1. **Enablement takes effect on the next Claude Code start** — plugins load at startup, so a
>    session that was already running when this file changed would not see them until restarted.
> 2. **This host is headless WSL with no Node/Chromium and no third-party API keys.** Plugins
>    whose capability is a Claude skill/hook/agent work out of the box; plugins that are an
>    external **MCP server** need their own setup (a binary, an account, or a key) before their
>    tools come online. The table flags which is which.

## Plugins that were enabled (now uninstalled — reference only)

| Plugin | What it gave you | Status while enabled |
|---|---|---|
| `superpowers` | Brainstorming, TDD, systematic debugging, subagent-driven dev, skill authoring | ✅ Works (Claude skills) |
| `frontend-design` | Higher-quality, non-generic frontend code generation (the `ui/` work) | ✅ Works (Claude skill) |
| `claude-md-management` | Audit/improve CLAUDE.md, capture session learnings | ✅ Works (Claude skill) |
| `code-simplifier` | Agent that simplifies recently-changed code without behaviour change | ✅ Works (Claude agent) |
| `pyright-lsp` | Pyright language server — type intelligence/diagnostics | ✅ Works (local LS); complements the existing `mypy` gate |
| `security-guidance` | Pattern warnings on edits + LLM diff review on Stop + agentic commit reviewer | ✅ Works (Claude hooks/agent) |
| `semgrep` | Static security analysis / secure-coding guidance | ✅ CLI runs via `uvx semgrep` (network needed for `--config=auto`) |
| `code-review` | Multi-agent PR review with confidence scoring | ✅ Works (Claude agents); overlaps the built-in `/code-review` skill |
| `agent-sdk-dev` | Dev kit for the Claude Agent SDK (this app's LLM runtime is `claude-agent-sdk`) | ✅ Works (Claude skills/docs) |
| `context7` | Up-to-date, version-specific library docs pulled into context (FastAPI, k8s, SDK) | ⚠️ MCP server — needs network; may need an Upstash key for rate limits |
| `playwright` | Real-browser E2E for the `ui/` (currently verified only via `tests/test_ui_frontend.py` + `ui/preview.html`) | ⛔ MCP server — **needs Node + browser binaries; not installed on this WSL host** |
| `codspeed` | Performance benchmarking/flamegraphs (fits a benchmarking agent) | ⚠️ MCP server — needs a CodSpeed account/token |
| `serena` | Semantic code search/refactor via LSP (complements the `graphify-out/` dev graph) | ⚠️ MCP server — runs locally via `uvx`; starts a language server on first use |
| `greptile` | Natural-language codebase search/Q&A | ⚠️ MCP server — needs a Greptile API key + an indexed repo |

Legend: ✅ usable now · ⚠️ needs external account/key/network · ⛔ blocked by a missing host dep.

## Notes for this repo (why they were optional, and why removal is safe)
- **Code quality is already gated locally** — `ruff check` (clean) and `mypy` (clean, 91 files)
  run via the venv and a `ruff_autofix` PostToolUse hook. `pyright-lsp` + `semgrep` + `code-review`
  would have added overlapping coverage; they never replaced the existing `make`/pytest gates,
  which is part of why uninstalling them (commit `394130c`) cost no required capability.
- **The repo deliberately does not follow `ruff format` style** (only `ruff check` is enforced) —
  don't let a formatter plugin mass-reformat the tree.
- **`playwright`/`chrome-devtools` won't run here** until Node + a browser are installed; the
  static-UI verification path (`tests/test_ui_frontend.py`, `ui/preview.html`) remains the way to
  check the UI in this environment.
