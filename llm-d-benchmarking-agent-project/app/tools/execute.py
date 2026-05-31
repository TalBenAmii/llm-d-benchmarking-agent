"""The single gated entry point for running the ``llmdbenchmark`` CLI.

Every standup / smoketest / run / teardown / plan goes through here. The handler builds
an argv list from structured arguments (never a shell string), validates it against the
allowlist for a clean early error, then runs it via the approval-gated runner.
"""
from __future__ import annotations

import re
from typing import Any

from app.tools.context import ToolContext, ToolError

_SUBCOMMANDS = {"plan", "standup", "smoketest", "run", "teardown", "results"}

# Sensible per-subcommand timeouts (seconds).
_TIMEOUTS = {
    "plan": 300.0,
    "standup": 3600.0,
    "smoketest": 900.0,
    "run": 3600.0,
    "teardown": 900.0,
    "results": 300.0,
}


def build_argv(
    subcommand: str,
    *,
    spec: str | None = None,
    namespace: str | None = None,
    harness: str | None = None,
    workload: str | None = None,
    flags: dict[str, Any] | None = None,
    extra: list[str] | None = None,
) -> list[str]:
    """Assemble the logical argv. Global flags (``--spec``) precede the subcommand."""
    flags = flags or {}
    argv: list[str] = ["llmdbenchmark"]
    if spec:
        argv += ["--spec", spec]
    argv.append(subcommand)
    if namespace:
        argv += ["-p", namespace]
    if harness:
        argv += ["-l", harness]
    if workload:
        argv += ["-w", workload]
    if flags.get("methods"):
        argv += ["-t", str(flags["methods"])]
    if flags.get("output"):
        argv += ["-r", str(flags["output"])]
    if flags.get("endpoint_url"):
        argv += ["-U", str(flags["endpoint_url"])]
    if flags.get("skip_smoketest"):
        argv.append("--skip-smoketest")
    if flags.get("list_endpoints"):
        argv.append("--list-endpoints")
    if flags.get("dry_run"):
        argv.append("--dry-run")
    argv += list(extra or [])
    return argv


async def execute_llmdbenchmark(
    ctx: ToolContext,
    *,
    subcommand: str,
    spec: str | None = None,
    namespace: str | None = None,
    harness: str | None = None,
    workload: str | None = None,
    flags: dict[str, Any] | None = None,
    extra: list[str] | None = None,
) -> dict[str, Any]:
    if subcommand not in _SUBCOMMANDS:
        raise ToolError(f"unsupported subcommand {subcommand!r}; allowed: {sorted(_SUBCOMMANDS)}")

    flags = dict(flags or {})
    # Default `run` output into the session workspace so the report is easy to locate.
    if subcommand == "run" and not flags.get("output") and not flags.get("list_endpoints") and not flags.get("dry_run"):
        flags["output"] = str(ctx.workspace / "results")

    argv = build_argv(
        subcommand, spec=spec, namespace=namespace, harness=harness,
        workload=workload, flags=flags, extra=extra,
    )

    # Validate up front for a clean, specific error message before any approval prompt.
    decision = ctx.allowlist.validate(argv, catalog=ctx.catalog_for_allowlist())
    if not decision.allowed:
        raise ToolError(f"command refused by allowlist: {decision.reason}\n  argv: {' '.join(argv)}")

    res = await ctx.run_command(argv, timeout=_TIMEOUTS.get(subcommand, 1800.0))
    results_dir = _parse_results_dir(res.output) or (str(ctx.workspace / "results") if flags.get("output") else None)
    return {
        "argv": argv,
        "mode": decision.mode,
        "exit_code": res.exit_code,
        "duration_s": res.duration_s,
        "timed_out": res.timed_out,
        "results_dir": results_dir,
        "stdout_tail": res.output[-2500:],
    }


_RESULTS_RE = re.compile(r"(/[\w./-]*results[\w./-]*)")


def _parse_results_dir(output: str) -> str | None:
    """Best-effort: pull a results directory path out of CLI output."""
    matches = _RESULTS_RE.findall(output or "")
    return matches[-1] if matches else None
