"""Deterministic skill-grounding gate — refuse a mutating operation until its grounding doc was
fetched this session.

De-inlining the kind runbook (``knowledge/quickstart_playbook.md`` is no longer in CORE; it now
loads on demand via ``fetch_key_docs(task="quickstart")``, exactly like the upstream guides) removed
the always-on steering that used to keep the kind demo on-procedure. This gate replaces it: once an
operation is attempted, it is REFUSED at the command chokepoint (``CommandExecutor.run_command``) and
at the plan gate (``propose_session_plan``) unless its grounding task is in ``ctx.consulted_skills``
— the per-session ledger ``fetch_key_docs`` writes keyed on the task ARG (so an absent skills repo
can't defeat the gate).

Same shape/rationale as the gated-model access guardrail (``app/tools/run/gated_access.py``): MECHANISM
enforcing a stated boundary on facts the agent already produced, never domain judgment (thin code,
thick agent). It acts ONLY on which tasks were fetched — it never decides *what* to benchmark.

Spec-aware: on the kind / CPU-sim path (spec starts with ``cicd/kind``) the runbook lives in the
``quickstart`` task, so the required task is ``quickstart`` REGARDLESS of subcommand. Off that path
each mutating llmdbenchmark subcommand — and the deploy plan — grounds in its own llm-d-skills SKILL
(``deploy_skill`` / ``benchmark_skill`` / ``teardown_skill`` / ``compare_skill``).
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.security.allowlist import Decision
    from app.tools.context import ToolContext

_CLI = "llmdbenchmark"

# execute_llmdbenchmark subcommand -> the llm-d-skills task that grounds it (used off the kind path;
# on the kind path 'quickstart' overrides every subcommand). plan/results carry no operation skill,
# so they are absent -> never gated. Subcommand names are exactly those execute.py::build_argv emits.
# A NEW MUTATING llmdbenchmark subcommand MUST be added here or it bypasses grounding (coverage test:
# tests/test_skill_gate.py::test_gate_covers_every_mutating_subcommand).
_TASK_BY_SUBCOMMAND = {
    "standup": "deploy_skill",
    "smoketest": "benchmark_skill",
    "run": "benchmark_skill",
    "teardown": "teardown_skill",
    "experiment": "compare_skill",   # the DoE sweep / compare subcommand
}


def _is_kind_spec(spec: str | None) -> bool:
    """The kind / CPU-sim MVP path, whose runbook lives in the quickstart task."""
    return bool(spec) and str(spec).startswith("cicd/kind")


def _gate_message(op: str, required: str) -> str:
    return (f'Ground this {op} first: call fetch_key_docs(task="{required}") before {op}. '
            "(skill-grounding gate)")


def _block(ctx: ToolContext, op: str, operation_task: str, spec: str | None) -> str | None:
    """The shared verdict: None when the required grounding task is in ``ctx.consulted_skills``,
    else the self-healing refusal message. The required task is spec-aware — ``quickstart`` on the
    kind path (the runbook now lives there), else the operation's own ``*_skill``."""
    required = "quickstart" if _is_kind_spec(spec) else operation_task
    if required in ctx.consulted_skills:
        return None
    return _gate_message(op, required)


def _parse_llmd_invocation(argv: list[str]) -> tuple[str, str | None] | None:
    """If ``argv`` runs ``llmdbenchmark … <subcommand> …``, return ``(subcommand, spec_or_None)``
    where spec is the value of the global ``--spec`` flag (space- or equals-form); otherwise None
    (not an llmdbenchmark invocation with a recognized operation subcommand). The CLI is matched by
    BASENAME so a path-qualified binary is still recognized. The command chokepoint passes a clean
    built argv (no ``bash -lc`` wrapper), so no shell-string flattening is needed here."""
    cli_idx = next((i for i, tok in enumerate(argv) if Path(tok).name == _CLI), None)
    if cli_idx is None:
        return None
    rest = argv[cli_idx + 1:]
    spec: str | None = None
    sub: str | None = None
    skip = -1
    for j, tok in enumerate(rest):
        if j == skip:
            continue  # this token is the consumed --spec value; don't mis-bind it as a subcommand
        if tok == "--spec" and j + 1 < len(rest):
            spec = rest[j + 1]
            skip = j + 1
        elif tok.startswith("--spec="):
            spec = tok[len("--spec="):]
        elif sub is None and tok in _TASK_BY_SUBCOMMAND:
            sub = tok
    if sub is None:
        return None
    return sub, spec


def skill_gate_block(ctx: ToolContext, decision: Decision) -> str | None:
    """Command-chokepoint gate (wired in command_exec.py for MUTATING decisions only): refuse an
    llmdbenchmark standup/smoketest/run/teardown/experiment until its grounding doc was fetched this
    session. Returns the self-healing message, or None to allow. Non-llmdbenchmark commands and the
    non-operation subcommands (plan/results) are never gated."""
    parsed = _parse_llmd_invocation(decision.argv)
    if parsed is None:
        return None
    sub, spec = parsed
    return _block(ctx, sub, _TASK_BY_SUBCOMMAND[sub], spec)


def plan_skill_gate_block(ctx: ToolContext, *, spec: str | None) -> str | None:
    """Early plan gate (wired in propose_session_plan): a SessionPlan always proposes a DEPLOY, so
    refuse to surface the approval card until the deploy's grounding doc was fetched — the friendly,
    pre-approval sibling of skill_gate_block. Spec-aware: ``quickstart`` on the kind path, else
    ``deploy_skill``. Returns the same self-healing message, or None to allow."""
    return _block(ctx, "deploy", "deploy_skill", spec)
