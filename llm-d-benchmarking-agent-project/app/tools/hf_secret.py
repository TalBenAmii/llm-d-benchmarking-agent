"""provision_hf_secret — approval-gated provisioning of the cluster HF token Secret.

A gated-model standup needs the cluster to hold a HuggingFace token Secret (the upstream
``llm-d-hf-token``) so the model server can pull the gated weights. This tool materializes
that Secret BEFORE standup. It is the natural follow-on to the Phase 62 gated-access
pre-flight: ``check_capacity`` tells you a model is GATED+UNAUTHORIZED because no token is
configured cluster-side; this is the mutating step that fixes it.

This handler is pure MECHANISM: it builds the argv for the vetted ``provision_hf_secret.py``
script and calls ``ctx.run_command``, which routes the (mutating, per the allowlist) command
through the approval gate. There is NO decision logic here — WHEN a gated model is in scope
and WHEN to provision lives in ``knowledge/capacity.md``.

The token NEVER crosses this layer. The handler's argv carries only ``--namespace`` and
(optionally) ``--name``; the script reads ``HF_TOKEN`` from the scrubbed child env that the
runner injects (``settings.extra_subprocess_env``). So the token appears in no tool input,
no argv, no ``command`` event, and no log.
"""
from __future__ import annotations

from typing import Any

from app.tools.context import ToolContext

# Upstream HF_TOKEN_NAME default (llm-d/helpers/hf-token.md). Kept in lockstep with the
# script's own default; the script applies it when --name is omitted from the argv.
_DEFAULT_SECRET_NAME = "llm-d-hf-token"


async def provision_hf_secret(
    ctx: ToolContext,
    *,
    namespace: str,
    name: str | None = None,
) -> dict[str, Any]:
    """Create/update the cluster HuggingFace token Secret in ``namespace``.

    Mutating → routed through the approval gate by ``ctx.run_command`` (the allowlist marks
    ``provision_hf_secret.py`` ``mode: mutating``). The token is read backend-side by the
    script from the scrubbed child env and is never part of the argv built here.
    """
    secret_name = name or _DEFAULT_SECRET_NAME
    argv = ["provision_hf_secret.py", "--namespace", namespace, "--name", secret_name]
    res = await ctx.run_command(argv)
    return {
        "namespace": namespace,
        "name": secret_name,
        "provisioned": res.exit_code == 0,
        "exit_code": res.exit_code,
        "timed_out": res.timed_out,
        # kubectl's own confirmation (e.g. "secret/llm-d-hf-token created"); never the token.
        "stdout_tail": res.output[-2000:],
        "note": (
            "HuggingFace token Secret provisioned. Re-run check_capacity to confirm the gated "
            "model is now authorized before standing up."
            if res.exit_code == 0
            else "Secret provisioning FAILED — read the stdout_tail. If HF_TOKEN is not "
            "configured in the backend, it must be set there (it stays backend-only)."
        ),
    }
