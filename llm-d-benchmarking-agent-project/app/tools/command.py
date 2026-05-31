"""Generic allowlisted-command tool.

This is the escape hatch that makes the allowlist the *single* place to widen the
agent's powers: any command added to ``security/allowlist.yaml`` is immediately usable
by the agent through this tool — no new Python, no new dedicated tool.

It is exactly as safe as the allowlist: every argv passes the deny-by-default validator
(charset screen + per-flag value constraints), read-only commands auto-run, and mutating
commands route through the same approval gate as everything else. Dedicated tools
(``execute_llmdbenchmark``, ``ensure_repos``, ``run_setup``) still exist for ergonomics
and sensible defaults; prefer them when one fits. Use this for allowlisted commands that
have no dedicated tool — e.g. ``kind create cluster`` or ``install_prereqs.sh``.
"""
from __future__ import annotations

from typing import Any

from app.tools.context import ToolContext, ToolError


async def run_command(
    ctx: ToolContext,
    *,
    argv: list[str],
    timeout: float | None = None,
) -> dict[str, Any]:
    if not argv or not all(isinstance(t, str) for t in argv):
        raise ToolError("argv must be a non-empty list of strings")

    # Validate up front for a clean, specific error before any approval prompt.
    decision = ctx.allowlist.validate(argv, catalog=ctx.catalog_for_allowlist())
    if not decision.allowed:
        raise ToolError(f"command refused by allowlist: {decision.reason}\n  argv: {' '.join(argv)}")

    res = await ctx.run_command(argv, timeout=timeout)
    return {
        "argv": list(argv),
        "mode": decision.mode,
        "exit_code": res.exit_code,
        "duration_s": res.duration_s,
        "timed_out": res.timed_out,
        "stdout_tail": res.output[-2500:],
    }
