"""The opt-in, allowlist-BYPASSING shell tool (``UNRESTRICTED_TOOLS=1``).

This is the deliberate escape hatch the deny-by-default allowlist normally forbids: it runs
an ARBITRARY command string through a real ``bash -lc``. It exists ONLY when
``settings.unrestricted_tools`` is set — the registry does not even expose it otherwise, and
the handler refuses (raises) as defense in depth if it is somehow reached with the flag off.

The human-in-the-loop guarantee the rest of the agent relies on is PRESERVED: a heuristic
(:func:`classify_shell_command`) classifies the command read-only vs mutating BEFORE it runs.
Read-only commands auto-run (no prompt); mutating OR UNKNOWN commands route through the same
``ctx.request_approval("command", …)`` gate every allowlisted mutating command uses, and raise
the same :class:`~app.tools.context.ApprovalRejected` when declined. The command is announced
with the SAME ``command`` event shape the allowlisted executor emits (so the debug-view /
command-trail plumbing works unchanged), under the same per-session run-cap semaphore and the
runner's timeout/env-scrubbing/cwd conventions.

The classifier FAILS SAFE: anything it cannot positively prove read-only is treated as
mutating, so an unrecognized binary always prompts.
"""
from __future__ import annotations

import contextlib
import shlex
from typing import Any

from app.observability import instrument
from app.security.allowlist import MUTATING, READ_ONLY
from app.tools.context import ApprovalRejected, ToolContext, ToolError

CommandMode = str  # READ_ONLY | MUTATING, reusing the allowlist's mode vocabulary

# Shell tokens that, appearing anywhere, mean the command WRITES — a redirect to a file or a
# tee. (`<` is an input redirect and does not write, so it is intentionally absent.)
_WRITE_OPERATORS = frozenset({">", ">>"})

# Verbs that mutate state wherever they appear in a pipeline. A bare entry matches the
# executable; a "binary subcommand" entry (e.g. "git push") matches exe + first argument.
_WRITE_VERBS = frozenset({
    "rm", "mv", "cp", "mkdir", "rmdir", "touch", "ln", "chmod", "chown", "dd", "truncate",
    "install", "tee", "apt", "apt-get", "yum", "dnf", "pip", "pip3", "npm", "make",
})
_WRITE_SUBCOMMANDS = {
    "git": frozenset({"push", "commit", "add", "checkout", "reset", "merge", "rebase"}),
    "kubectl": frozenset({
        "apply", "create", "delete", "patch", "edit", "scale", "rollout", "label",
        "annotate", "cordon", "drain", "exec", "cp", "replace", "set",
    }),
    "helm": frozenset({"install", "upgrade", "uninstall", "delete"}),
    "docker": frozenset({"run", "rm", "rmi", "build", "push", "exec", "stop", "kill"}),
    "kind": frozenset({"create", "delete"}),
}

# Plain read-only executables (no subcommand grammar). `xargs` is deliberately NOT here — its
# behavior depends on the sub-command it runs, so we treat a bare `xargs` as MUTATING to be safe.
_READ_ONLY_EXES = frozenset({
    "ls", "cat", "head", "tail", "grep", "egrep", "rg", "find", "echo", "pwd", "whoami",
    "env", "printenv", "which", "df", "du", "ps", "top", "uname", "date", "stat", "wc",
    "file", "hostname", "id", "uptime", "free", "sort", "uniq", "cut", "awk", "jq", "yq",
})
# Read-only subcommands of binaries that ALSO have mutating subcommands.
_READ_ONLY_SUBCOMMANDS = {
    "kubectl": frozenset({
        "get", "describe", "logs", "top", "version", "api-resources", "explain",
        "cluster-info", "config",  # `config view` only — narrowed below
    }),
    "docker": frozenset({"ps", "images", "info", "version", "inspect", "logs", "stats"}),
    "git": frozenset({
        "status", "log", "diff", "show", "branch", "remote", "rev-parse", "config",
        "blame", "ls-files",  # `config --get` only — narrowed below
    }),
    "helm": frozenset({"list", "status", "get", "version", "history"}),
}


def _split_pipeline(command: str) -> list[list[str]]:
    """Tokenize a command into its simple-command segments (split on the pipeline/list
    operators ``| & && || ;``). Returns one argv-ish token list per segment. A token that is a
    bare write operator (``>`` / ``>>``) is kept so the redirect screen can see it."""
    tokens = shlex.split(command)
    segments: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok in ("|", "&", "&&", "||", ";"):
            segments.append(current)
            current = []
        else:
            current.append(tok)
    segments.append(current)
    return [seg for seg in segments if seg]


def _segment_is_read_only(seg: list[str]) -> bool:
    """True only if this single simple-command is POSITIVELY known read-only. Unknown → False
    (the caller then classifies the whole command MUTATING)."""
    exe = seg[0]
    if exe == "sed":
        # `sed -n` (no in-place) is a read-only printer; any other sed may edit in place (-i).
        return "-n" in seg[1:] and not any(a.startswith("-i") for a in seg[1:])
    if exe in _READ_ONLY_EXES:
        return True
    sub_ro = _READ_ONLY_SUBCOMMANDS.get(exe)
    if sub_ro is not None:
        args = [a for a in seg[1:] if not a.startswith("-")]
        if not args:
            return False  # a bare `kubectl`/`git`/… with no subcommand — not provably read-only
        return args[0] in sub_ro
    return False


def classify_shell_command(command: str) -> CommandMode:
    """Classify an arbitrary shell command READ_ONLY vs MUTATING — the human-approval gate for
    the unrestricted shell tool. FAILS SAFE: anything not POSITIVELY proven read-only is
    MUTATING, so an unrecognized binary always prompts.

    Logic: a command is MUTATING if it contains ANY write indicator — a ``>``/``>>`` redirect,
    or a write verb (``rm``/``mv``/``tee``/``pip``/``git push``/``kubectl apply``/…) anywhere in
    the pipeline, or a ``curl`` that writes (``-X POST|PUT|DELETE`` / ``-d`` / ``-O`` / ``-o``).
    It is READ_ONLY only when EVERY simple-command in the pipeline is positively known read-only;
    an unparseable or unknown command defaults to MUTATING."""
    try:
        segments = _split_pipeline(command)
    except ValueError:
        return MUTATING  # unbalanced quotes etc. — fail safe
    if not segments:
        return MUTATING

    for seg in segments:
        # 1) Shell write redirect anywhere in the segment → mutating.
        if any(tok in _WRITE_OPERATORS for tok in seg):
            return MUTATING
        exe = seg[0]
        # 2) A bare write verb (rm/mv/tee/pip/make/…) → mutating.
        if exe in _WRITE_VERBS:
            return MUTATING
        # 3) A write subcommand of a multi-mode binary (git push / kubectl apply / …) → mutating.
        sub_write = _WRITE_SUBCOMMANDS.get(exe)
        if sub_write is not None:
            args = [a for a in seg[1:] if not a.startswith("-")]
            if args and args[0] in sub_write:
                return MUTATING
        # 4) curl that writes (uploads or downloads to a file) → mutating.
        if exe == "curl" and _curl_writes(seg[1:]):
            return MUTATING

    # MUTATING unless EVERY segment is positively read-only (unknown binary ⇒ not read-only).
    if all(_segment_is_read_only(seg) for seg in segments):
        return READ_ONLY
    return MUTATING


def _curl_writes(args: list[str]) -> bool:
    """A curl invocation is treated as mutating when it POSTs/PUTs/DELETEs, sends a body
    (``-d``/``--data``), or downloads to a file (``-O``/``-o``)."""
    write_methods = {"POST", "PUT", "DELETE", "PATCH"}
    for i, a in enumerate(args):
        if a in ("-O", "-o", "-d", "--data", "--data-raw", "--data-binary", "-T", "--upload-file"):
            return True
        if a in ("-X", "--request") and i + 1 < len(args) and args[i + 1].upper() in write_methods:
            return True
    return False


async def run_shell(
    ctx: ToolContext,
    *,
    command: str,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Run an ARBITRARY shell command via ``bash -lc`` (UNRESTRICTED_TOOLS only).

    Read-only commands auto-run; mutating/unknown commands park for the user's Approve/Decline
    via the same gate every allowlisted mutating command uses, raising
    :class:`~app.tools.context.ApprovalRejected` when declined. Emits the SAME ``command`` event
    shape (argv/text/mode/auto_run/…) the allowlisted executor emits."""
    # Defense in depth: this tool is only registered when the flag is on, but never run it
    # if the flag is off (e.g. a stale handler reference).
    if not ctx.settings.unrestricted_tools:
        raise ToolError("run_shell is disabled (set UNRESTRICTED_TOOLS=1 to enable it)")
    if not isinstance(command, str) or not command.strip():
        raise ToolError("command must be a non-empty shell string")

    mode = classify_shell_command(command)
    requires_approval = mode == MUTATING
    argv = ["bash", "-lc", command]

    # Mutating/unknown commands gate on approval BEFORE running (skipped in simulate, where
    # commands are harmless no-ops — mirroring the allowlisted executor).
    if requires_approval and not ctx.settings.simulate:
        if ctx.request_approval is None:
            raise ToolError("approval required but no approver is wired")
        payload = {"command": command, "argv": argv, "mode": mode}
        if not await ctx.request_approval("command", payload):
            raise ApprovalRejected(argv)

    auto_run = not requires_approval
    await _emit_command(ctx, argv=argv, mode=mode, auto_run=auto_run)

    # Stream output to the UI exactly like the allowlisted path (when an emit is wired).
    async def _emit_line(line: str) -> None:
        if ctx.emit is not None:
            await ctx.emit("output", {"line": line})

    on_line = _emit_line if ctx.emit is not None else None
    # Bound concurrent heavy (mutating) shells under the shared run-cap semaphore, like run_command.
    if ctx.run_semaphore is not None and mode == MUTATING:
        async with ctx.run_semaphore:
            res = await ctx.runner.run_shell(command, on_line=on_line, timeout=timeout)
    else:
        res = await ctx.runner.run_shell(command, on_line=on_line, timeout=timeout)

    _record_metric(mode=mode, auto_run=auto_run, duration_s=res.duration_s,
                   exit_code=res.exit_code, timed_out=res.timed_out)
    return {
        "command": command,
        "argv": list(argv),
        "mode": mode,
        "auto_run": auto_run,
        "exit_code": res.exit_code,
        "duration_s": res.duration_s,
        "timed_out": res.timed_out,
        "stdout_tail": res.output[-2500:],
    }


async def _emit_command(ctx: ToolContext, *, argv: list[str], mode: str, auto_run: bool) -> None:
    """Announce the shell command with the SAME ``command`` event shape the allowlisted executor
    emits (see app/tools/command_exec.py::_emit_command), so the debug-view / command-trail
    plumbing records it identically."""
    if ctx.emit is not None:
        await ctx.emit("command", {
            "argv": list(argv),
            "text": " ".join(argv),
            "mode": mode,
            "auto_run": auto_run,
            "simulated": ctx.settings.simulate,
            "tool_call_id": ctx.current_tool_call_id,
        })


def _record_metric(
    *, mode: str, auto_run: bool,
    duration_s: float, exit_code: int, timed_out: bool,
) -> None:
    """File the executed-shell fact into the metrics registry + structured log, mirroring the
    allowlisted executor's _record_metric. ``exe`` is fixed at ``bash`` (bounded cardinality —
    never the arbitrary command, which would explode the label space). Best-effort."""
    with contextlib.suppress(Exception):
        instrument.record_command(exe="bash", mode=mode, auto_run=auto_run, duration_s=duration_s)
    with contextlib.suppress(Exception):
        from app.tools.command_exec import log
        log.info("command.exec", extra={
            "exe": "bash", "mode": mode, "auto_run": auto_run,
            "duration_s": duration_s, "exit_code": exit_code, "timed_out": timed_out,
        })
