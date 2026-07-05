"""Import-surface contract guard for the external ``llm-d-bench-mcp`` server.

The MCP server was split into its own repo (github.com/TalBenAmii/llm-d-bench-mcp) on 2026-07-05.
It consumes this project as an editable install and imports a fixed set of ``app.*`` symbols. No
code in THIS repo imports them for the MCP path anymore, so an ordinary refactor here could rename
or move one and silently break the external adapter. This test pins that cross-repo contract: if it
fails, the split-out server needs the same change (restore the symbol, or update its imports).

Source of truth: the ``from app.…`` imports in the external repo's
``llm_d_bench_mcp/{server,adapters,content}.py``.
"""

import importlib

import pytest

# module path -> the names the external llm-d-bench-mcp adapter imports from it.
MCP_IMPORT_SURFACE = {
    "app.agent.lifecycle": ["RunRegistry"],
    "app.agent.prompt": ["_one_line_purpose"],
    "app.config": ["Settings"],
    "app.security.allowlist": ["Allowlist"],
    "app.security.runner": ["CommandRunner"],
    "app.tools.context": ["ApproveFn", "EmitFn", "ToolContext", "ApprovalRejected", "ToolError"],
    "app.tools.knowledge_access": ["EXCLUDED_KNOWLEDGE_FILES"],
    "app.tools.registry": ["dispatch", "tool_definitions"],
}


@pytest.mark.parametrize("module, names", list(MCP_IMPORT_SURFACE.items()))
def test_mcp_import_surface(module, names):
    mod = importlib.import_module(module)
    missing = [n for n in names if not hasattr(mod, n)]
    assert not missing, (
        f"{module} no longer exports {missing} — the external llm-d-bench-mcp server imports "
        f"these; restore the symbol or update that repo when you change this."
    )
