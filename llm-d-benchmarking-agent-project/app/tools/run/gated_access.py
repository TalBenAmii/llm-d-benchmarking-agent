"""Deterministic gated-model access guardrail — a security gate, like the approval gate.

The agent's *steering* (the system-prompt HARD_RULE, ``knowledge/capacity.md``, and
``check_capacity``'s ``gated_note``) ASKS the model not to deploy a gated model whose weights the
backend HuggingFace token can't pull. Steering alone proved unreliable against a flaky model, so
this module is the non-bypassable BACKSTOP: once ``check_capacity`` reports a model is
``gated: true`` + ``authorized: false``, any attempt to ``standup`` / ``run`` / ``smoketest`` is
REFUSED at the command chokepoint (``CommandExecutor.run_command`` — exactly where the
policy / approval gates live) until a later ``check_capacity`` for that model reports
``authorized`` (provision the HF secret, then re-check). A standup of an un-pullable model only
fails opaquely minutes in; this turns that into an instant, actionable refusal.

This is MECHANISM enforcing a stated SAFETY boundary, not domain judgment (cf. the non-negotiable
"thin code, thick agent"): it acts ONLY on the gated/authorized FACTS the capacity bridge already
produced — it never decides *what* to benchmark. It is the same shape as the existing
mutating→approval guardrail.

State (``ctx.gated_access``) is per-session and RUNTIME-ONLY (like ``ctx.fetched_docs`` /
``ctx.env_snapshot``): it lives for the session process. A resumed chat re-establishes it on its
next ``check_capacity`` — which the mandatory pre-flight requires before any standup anyway.
``provision_hf_secret`` is never blocked (it is not an llmdbenchmark deploy, and it is the fix).
"""
from __future__ import annotations

import shlex
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.tools.context import ToolContext

# llmdbenchmark subcommands that DEPLOY or hit the model endpoint. These are the only ones gated:
# a model the backend token can't pull must not be stood up / run / smoke-tested. (plan / teardown
# / results / experiment don't pull weights, so they're never blocked.)
_DEPLOY_SUBCOMMANDS = frozenset({"standup", "run", "smoketest"})
_CLI = "llmdbenchmark"
_MODEL_FLAGS = frozenset({"-m", "--models", "--model"})


def record_capacity_verdict(
    ctx: ToolContext,
    *,
    model: str | None,
    gated: bool | None,
    authorized: bool | None,
    gated_reason: str = "",
) -> None:
    """Record a ``check_capacity`` gated-access verdict for ``model`` onto the session context so
    the command guardrail can later refuse deploying a gated+unauthorized model. Overwriting the
    entry on each check is how the block CLEARS: a re-check that returns ``authorized: true``
    replaces the ``authorized: false`` entry. No-op when ``model`` is falsy (no id to key by)."""
    if not model:
        return
    ctx.gated_access[str(model)] = {
        "gated": gated,
        "authorized": authorized,
        "gated_reason": gated_reason or "",
    }


def _flatten_tokens(argv: list[str]) -> list[str]:
    """argv tokens with any ``bash -lc "<cmd>"`` / ``sh -c "<cmd>"`` wrapper expanded, so an
    llmdbenchmark invocation issued through the ad-hoc ``run_shell`` tool is visible to the parser
    too. Best-effort shlex; an unparseable inner string falls back to a whitespace split."""
    tokens: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("bash", "sh") and i + 2 < len(argv) and argv[i + 1] in ("-lc", "-c"):
            inner = argv[i + 2]
            try:
                tokens.extend(shlex.split(inner))
            except ValueError:
                tokens.extend(inner.split())
            i += 3
            continue
        tokens.append(tok)
        i += 1
    return tokens


def _model_from_equals_form(tok: str) -> str | None:
    """The value of an equals-form model flag (``--models=meta-llama/…`` / ``--model=…``), or None.
    Space-form (``--models <value>``) is handled by the caller's look-ahead; this covers the
    ``--flag=value`` spelling that the look-ahead would otherwise miss (a cleared model deployed
    this way must be recognized, not wrongly refused)."""
    for flag in _MODEL_FLAGS:
        prefix = flag + "="
        if tok.startswith(prefix):
            return tok[len(prefix):]
    return None


def _parse_deploy_invocation(argv: list[str]) -> tuple[str, str | None] | None:
    """If ``argv`` runs ``llmdbenchmark … <standup|run|smoketest> …`` (directly OR inside a
    ``bash -lc`` string), return ``(subcommand, model_or_None)`` where model is the value of
    ``-m`` / ``--models`` / ``--model`` (space- OR equals-form) when present. Otherwise return
    ``None`` (not a gated-deploy command).

    The CLI binary is matched by BASENAME, so a path-qualified invocation through the ad-hoc
    ``run_shell`` surface (``/usr/local/bin/llmdbenchmark`` / ``./llmdbenchmark``) is recognized
    too — the guardrail must not be bypassable simply by spelling the binary with a path."""
    tokens = _flatten_tokens(argv)
    cli_idx = next((i for i, tok in enumerate(tokens) if Path(tok).name == _CLI), None)
    if cli_idx is None:
        return None
    rest = tokens[cli_idx + 1:]
    sub: str | None = None
    model: str | None = None
    for j, tok in enumerate(rest):
        if sub is None and tok in _DEPLOY_SUBCOMMANDS:
            sub = tok
        if tok in _MODEL_FLAGS and j + 1 < len(rest):
            model = rest[j + 1]                 # space-form: --models <value>
        else:
            eq = _model_from_equals_form(tok)   # equals-form: --models=<value>
            if eq is not None:
                model = eq
    if sub is None:
        return None
    return sub, model


def gated_block(ctx: ToolContext, argv: list[str]) -> tuple[str, str] | None:
    """Return ``(model, gated_reason)`` if this command must be REFUSED because a gated+unauthorized
    model is outstanding for this session; else ``None``.

    Only llmdbenchmark deploy subcommands are gated. While ANY model is outstanding
    gated+unauthorized, a deploy is refused UNLESS it explicitly targets (``-m``) a model that a
    ``check_capacity`` has POSITIVELY cleared (recorded public, or gated+authorized). A deploy with
    no explicit model — or with an explicit model that was never check_capacity'd — is refused (you
    can't deploy blind, or past an unconfirmed model, while an access block is outstanding; the
    unconfirmed model may BE the gated one under a different key, e.g. the spec default vs the -m
    override). The block clears when a later ``check_capacity`` records the model ``authorized`` (or
    public)."""
    parsed = _parse_deploy_invocation(argv)
    if parsed is None:
        return None
    _, target = parsed  # subcommand already validated as a deploy verb by the parser
    blocked = {
        m: v
        for m, v in ctx.gated_access.items()
        if v.get("gated") and v.get("authorized") is False
    }
    if not blocked:
        return None
    if target is not None:
        v = blocked.get(str(target))
        if v is not None:
            # This exact model is the one flagged gated+unauthorized → refuse.
            return str(target), str(v.get("gated_reason", ""))
        # The deploy names a model that is NOT itself in the blocked set. Allow it ONLY if a
        # check_capacity POSITIVELY cleared it — i.e. it was RECORDED (public, or gated+authorized).
        # An UNRECORDED model is refused while ANY access block is outstanding: we have not
        # confirmed THIS model can be pulled, and a sibling model IS known un-pullable, so
        # proceeding risks standing up a gated model whose verdict was recorded under a different
        # key (e.g. the spec's default model vs the -m override the standup actually uses — the
        # real-eval gap). The fix is to re-run check_capacity for the model actually being deployed.
        # (A clean session — nothing blocked — already returned None above, so this strict path
        # never touches a normal public/authorized deploy.)
        if str(target) in ctx.gated_access:
            return None   # recorded AND not in the blocked set ⇒ cleared (public / authorized)
        _, bv = next(iter(blocked.items()))
        return str(target), str(bv.get("gated_reason", ""))
    # No explicit model on a deploy while an access block is outstanding → refuse, naming it.
    model, v = next(iter(blocked.items()))
    return model, str(v.get("gated_reason", ""))


def gated_block_message(model: str, gated_reason: str) -> str:
    """The refusal text for a blocked deploy. Deliberately also a strong, in-context nudge to
    call ``provision_hf_secret`` — it fires at the exact moment the agent attempts the standup.

    Worded to be accurate BOTH when ``model`` is the confirmed gated+unauthorized model AND when it
    is an unconfirmed model deployed while a sibling block is outstanding (see ``gated_block``):
    "access … is not confirmed pullable" covers both without over-asserting."""
    detail = f" Upstream detail: {gated_reason}" if gated_reason else ""
    return (
        f"BLOCKED by the gated-model access guardrail: access for '{model}' is not confirmed "
        f"pullable by the backend HuggingFace token, so a standup / run / smoketest would risk "
        f"failing opaquely minutes in. Resolve access FIRST, then retry — if no token is configured "
        f"cluster-side, call the provision_hf_secret tool now (approval-gated); if the token merely "
        f"lacks access, request it at huggingface.co/{model}. After that, re-run check_capacity for "
        f"this exact model and proceed only once it reports authorized: true.{detail} See "
        f"read_knowledge('capacity')."
    )
