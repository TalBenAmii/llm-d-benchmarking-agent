"""Agent tool: check inference-endpoint READINESS before submitting a benchmark.

Goes BEYOND ``probe_environment``'s pod-presence check. It asks two read-only, allowlisted
questions about the target namespace and folds them into one structured verdict:

  1. **Kubernetes endpoint readiness** (authoritative): ``kubectl get endpoints -n <ns>
     -o json`` — does any Service have a *ready backing address*? A pod that exists but is
     failing its readiness probe is NOT in a Service's ready addresses, so this is strictly
     stronger than "a pod is Running".
  2. **The benchmark CLI's own endpoint view** (corroborating, best-effort): ``llmdbenchmark
     run --list-endpoints`` (already allowlisted, read-only) — how many inference endpoints
     the tool that will actually drive the benchmark can see.

Both are read-only (auto-run, no approval) and mutate nothing. When the stack is not ready
the result carries a ``standup_suggestion`` so the agent can OFFER an approval-gated standup
— but this tool never stands anything up itself. The DECISION to stand up (and the approval
gate) is the agent's judgment; see ``knowledge/orchestrator.md`` / ``knowledge/preconditions.md``.
"""
from __future__ import annotations

import re
import shutil
from typing import Any

from app.orchestrator.readiness import EndpointReadiness, analyze_endpoints
from app.tools.context import ToolContext, ToolError

# A line like "Endpoint: http://..." / "endpoints: 2" / a bare URL in `run --list-endpoints`
# output. Best-effort corroboration only — the Kubernetes endpoint-address readiness is the gate.
_URL_RE = re.compile(r"https?://[^\s'\"]+")


async def _kube_endpoint_readiness(ctx: ToolContext, namespace: str) -> EndpointReadiness:
    """The authoritative gate: read the namespace's Endpoints objects and analyze them."""
    res = await ctx.run_readonly(
        ["kubectl", "get", "endpoints", "-n", namespace, "-o", "json"], timeout=15.0
    )
    if res.exit_code != 0:
        return EndpointReadiness(
            namespace=namespace, ready=False, reason="cluster_unreachable",
            detail="could not read endpoints — the cluster may be unreachable or the "
                   "namespace may not exist (kubectl exited non-zero).",
        )
    return analyze_endpoints(res.output, namespace=namespace)


async def _cli_endpoints_seen(ctx: ToolContext, namespace: str, spec: str | None) -> int | None:
    """Corroborating, best-effort: how many endpoints does the benchmark CLI itself see?

    Uses the CLI's own read-only ``run --list-endpoints`` (a ``read_only_trigger`` flag, so it
    never deploys). Returns the count, or None if the probe couldn't run (no venv yet, etc.) —
    a failure here NEVER changes the gate, which is driven by the Kubernetes endpoint readiness."""
    bench_venv_python = ctx.settings.bench_repo / ".venv" / "bin" / "python"
    if not bench_venv_python.exists():
        return None  # the benchmark CLI isn't installed yet — skip the corroborating probe
    argv = ["llmdbenchmark"]
    if spec:
        argv += ["--spec", spec]
    argv += ["run", "-p", namespace, "--list-endpoints"]
    try:
        res = await ctx.run_readonly(argv, timeout=60.0)
    except ToolError:
        return None
    if res.exit_code != 0:
        return None
    return len(set(_URL_RE.findall(res.output or "")))


async def check_endpoint_readiness(
    ctx: ToolContext,
    *,
    namespace: str,
    spec: str | None = None,
    probe_cli_endpoints: bool = True,
) -> dict[str, Any]:
    """Structured inference-endpoint readiness for ``namespace``. Read-only; auto-runs.

    Returns ``ready`` (the gate), the per-service ready/not-ready endpoint address counts, an
    optional benchmark-CLI endpoint count, and — when NOT ready — a ``standup_suggestion`` the
    agent can act on (offer an approval-gated standup). Never mutates."""
    if not shutil.which("kubectl"):
        raise ToolError("kubectl is not on PATH — cannot check endpoint readiness")

    verdict = await _kube_endpoint_readiness(ctx, namespace)

    cli_seen: int | None = None
    if probe_cli_endpoints and verdict.reason in {"endpoints_ready", "endpoints_not_ready", "no_endpoints"}:
        cli_seen = await _cli_endpoints_seen(ctx, namespace, spec)
    verdict.cli_endpoints_seen = cli_seen

    out = verdict.as_dict()
    if not verdict.ready:
        out["standup_suggestion"] = _standup_suggestion(namespace, spec, verdict.reason)
    return out


def _standup_suggestion(namespace: str, spec: str | None, reason: str) -> dict[str, Any]:
    """The agent's cue to OFFER bringing up a stack — NOT an action. The decision and the
    approval gate are the agent's/user's; this only describes the approval-gated path
    (``execute_llmdbenchmark subcommand='standup'``) that WOULD make the endpoint ready."""
    return {
        "recommended": True,
        "namespace": namespace,
        "spec": spec,
        "via": "execute_llmdbenchmark",
        "subcommand": "standup",
        "approval_required": True,
        "why": "no ready inference endpoint in this namespace"
               if reason == "no_endpoints"
               else "the stack is present but not serving yet",
        "note": "OFFER this to the user; standup is mutating and requires explicit approval. "
                "Do not stand up without the user's go-ahead.",
    }
