"""Agent tool: check inference-endpoint READINESS before submitting a benchmark.

Goes BEYOND ``probe_environment``'s pod-presence check. It asks two read-only, policy-allowed
questions about the target namespace and folds them into one structured verdict:

  1. **Kubernetes endpoint readiness** (authoritative): ``kubectl get endpoints -n <ns>
     -o json`` — does any Service have a *ready backing address*? A pod that exists but is
     failing its readiness probe is NOT in a Service's ready addresses, so this is strictly
     stronger than "a pod is Running".
  2. **The benchmark CLI's own endpoint view** (corroborating, best-effort): ``llmdbenchmark
     run --list-endpoints`` (already policy-allowed, read-only) — how many inference endpoints
     the tool that will actually drive the benchmark can see.

Both are read-only (auto-run, no approval) and mutate nothing. When the stack is not ready
the result carries a ``standup_suggestion`` so the agent can OFFER an approval-gated standup
— but this tool never stands anything up itself. The DECISION to stand up (and the approval
gate) is the agent's judgment; see ``knowledge/orchestrator.md`` / ``knowledge/preconditions.md``.

In GATEWAY-mode deploys (GKE/Istio/agentgateway) it ALSO reads the Gateway-API control plane —
``kubectl get gateway,gatewayclass,inferencepool,httproute -o json`` — and folds the PROGRAMMED /
Accepted / ResolvedRefs / Reconciled status conditions into ``gateway`` (Phase 65). That extends
the gate from "the model pods are Ready" to "traffic can actually REACH them": pods can be Ready
while the Gateway is still ``PROGRAMMED:False`` (or the InferencePool is unresolved), so no traffic
flows yet. Those are FACTS only; the wait-vs-stand-up-vs-config-error JUDGMENT lives in
``knowledge/gateway_readiness.md`` (``read_knowledge('gateway_readiness')``), never a Python branch.
"""
from __future__ import annotations

import asyncio
import re
import shutil
from typing import Any

from app.readiness.diagnostics import (
    EndpointReadiness,
    GatewayReadiness,
    ServingReadiness,
    analyze_endpoints,
    analyze_gateway,
    classify_serving_readiness,
)
from app.security.runner import RunResult
from app.tools.context import ToolContext, ToolError

# A line like "Endpoint: http://..." / "endpoints: 2" / a bare URL in `run --list-endpoints`
# output. Best-effort corroboration only — the Kubernetes endpoint-address readiness is the gate.
_URL_RE = re.compile(r"https?://[^\s'\"]+")

# The HTTP status line `curl -i` prints first, e.g. "HTTP/1.1 200 OK" / "HTTP/2 503". We read the
# status code from it — pure mechanism (no loading-vs-broken judgment; that's the knowledge file).
_STATUS_LINE_RE = re.compile(r"^HTTP/[\d.]+\s+(\d{3})", re.MULTILINE)

# Model-server serving ports, by pod role (llm-d docs/readiness-probes.md). The decode pod is
# proxied through a sidecar on 8200; prefill / standalone serve directly on 8000.
_PORT_BY_ROLE = {"prefill": 8000, "decode": 8200, "standalone": 8000}

# The two model-aware probe paths. /v1/models is serving-ready (startup+readiness probe);
# /health is only process-alive (liveness probe). Constrained to this enum in the policy.
_MODELS_PATH = "/v1/models"
_HEALTH_PATH = "/health"


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


def _probe_status(res: RunResult) -> tuple[int | None, bool]:
    """Map a ``curl -i`` RunResult to ``(http_status_code, reachable)`` — pure mechanism.

    The HTTP status is read from the response status line; a connection-refused / timed-out
    probe (no status line, non-zero curl exit) yields ``(None, False)``. This only COPIES the
    outcome through; whether a given code means "loading" vs "broken" is the knowledge file's
    judgment, never decided here."""
    m = _STATUS_LINE_RE.search(res.output or "")
    if m:
        return int(m.group(1)), True
    # No status line: curl never got an HTTP response (connection refused / timeout / DNS).
    return None, False


def _svc_probe_url(namespace: str, service: str, port: int, path: str) -> str:
    """Build the in-namespace cluster-DNS service URL the policy permits for the probe
    (``http://<svc>.<ns>.svc:<port><path>``). The policy's ``modelserver_probe_url`` regex
    re-validates host/port/path independently — this is just the construction side."""
    return f"http://{service}.{namespace}.svc:{port}{path}"


async def _curl_probe(ctx: ToolContext, url: str) -> tuple[int | None, bool]:
    """Run ONE constrained, read-only GET probe and return ``(status, reachable)``. Best-effort:
    any policy/runner failure degrades to ``(None, False)`` and never raises — a probe that
    cannot run must not break the readiness gate."""
    argv = ["curl", "-s", "-S", "-i", "-m", "5", "-X", "GET", url]
    try:
        res = await ctx.run_readonly(argv, timeout=10.0)
    except ToolError:
        return None, False
    return _probe_status(res)


def _probe_target(verdict: EndpointReadiness) -> str | None:
    """Pick the Service to probe: the first not-ready inference Service in the verdict (the one
    that is present but not serving). Returns None when there is no candidate."""
    for entry in verdict.not_ready_endpoints or verdict.ready_endpoints:
        svc = entry.get("service")
        if svc:
            return str(svc)
    return None


async def _serving_readiness(
    ctx: ToolContext, namespace: str, verdict: EndpointReadiness
) -> ServingReadiness | None:
    """For a Running-but-NotReady endpoint, gather model-load serving-readiness FACTS.

    Reads the already-policy-allowed ``kubectl get pods -o json`` (read-only) and runs the
    tightly-constrained GET probes against the in-namespace service URL: ``/v1/models``
    (serving-ready) and ``/health`` (process-alive), on the model-server port. The pod JSON +
    the verbatim probe outcomes are folded into :class:`ServingReadiness` (signals only). Whether
    those signals mean "still loading weights" vs "wedged/broken" is the LLM's call over
    ``knowledge/readiness_probes.md`` — there is NO such if/elif here. Best-effort throughout:
    a kubectl/curl failure degrades gracefully and never raises."""
    try:
        pods_res = await ctx.run_readonly(
            ["kubectl", "get", "pods", "-n", namespace, "-o", "json"], timeout=15.0
        )
        pods_json = pods_res.output if pods_res.exit_code == 0 else ""
    except ToolError:
        pods_json = ""

    service = _probe_target(verdict)
    health_status: int | None = None
    health_reachable = False
    models_status: int | None = None
    models_reachable = False
    if service is not None:
        # Probe the standalone/prefill port (8000); if unreachable there, try the decode port
        # (8200) — a decode pod is proxied on 8200. This is connectivity selection (mechanism),
        # not a loading-vs-broken decision.
        for port in (_PORT_BY_ROLE["standalone"], _PORT_BY_ROLE["decode"]):
            h_status, h_reach = await _curl_probe(
                ctx, _svc_probe_url(namespace, service, port, _HEALTH_PATH)
            )
            m_status, m_reach = await _curl_probe(
                ctx, _svc_probe_url(namespace, service, port, _MODELS_PATH)
            )
            health_status, health_reachable = h_status, h_reach
            models_status, models_reachable = m_status, m_reach
            if h_reach or m_reach:
                break  # got a responding port — keep these facts

    return classify_serving_readiness(
        pods_json,
        namespace=namespace,
        health_status=health_status,
        models_status=models_status,
        health_reachable=health_reachable,
        models_reachable=models_reachable,
    )


async def _gateway_readiness(ctx: ToolContext, namespace: str) -> GatewayReadiness:
    """Read the Gateway-API control plane for ``namespace`` and fold it into condition FACTS.

    Runs four already-policy-allowed, read-only ``kubectl get <res> -o json`` probes — ``gateway``,
    ``inferencepool`` and ``httproute`` (namespaced), and ``gatewayclass`` (cluster-scoped, so NO
    ``-n``). Each is best-effort: a non-zero exit (gateway CRDs absent on a Kind cluster, namespace
    missing, cluster unreachable) degrades that resource to an empty string, never raises — a
    Gateway-API probe that cannot run must not break the endpoint-readiness gate. The verbatim
    PROGRAMMED / Accepted / ResolvedRefs / Reconciled conditions are extracted by the pure
    :func:`analyze_gateway`; whether ``PROGRAMMED:False`` means "still provisioning — keep waiting"
    vs "misconfigured — surface a config error" is the LLM's call over
    ``knowledge/gateway_readiness.md``, NOT a branch here."""
    async def _get(resource: str, *, namespaced: bool) -> str:
        argv = ["kubectl", "get", resource]
        if namespaced:
            argv += ["-n", namespace]
        argv += ["-o", "json"]
        try:
            res = await ctx.run_readonly(argv, timeout=15.0)
        except ToolError:
            return ""
        return res.output if res.exit_code == 0 else ""

    # The four reads are independent and read-only (read-only commands skip the run semaphore),
    # so gather them — one round-trip instead of four serial ones. On a Kind cluster with no
    # Gateway-API CRDs all four fast-fail together rather than in series.
    gateway_json, gatewayclass_json, inferencepool_json, httproute_json = await asyncio.gather(
        _get("gateway", namespaced=True),
        _get("gatewayclass", namespaced=False),  # cluster-scoped
        _get("inferencepool", namespaced=True),
        _get("httproute", namespaced=True),
    )
    return analyze_gateway(
        namespace=namespace,
        gateway_json=gateway_json,
        gatewayclass_json=gatewayclass_json,
        inferencepool_json=inferencepool_json,
        httproute_json=httproute_json,
    )


async def check_endpoint_readiness(
    ctx: ToolContext,
    *,
    namespace: str,
    spec: str | None = None,
    probe_cli_endpoints: bool = True,
    check_gateway: bool = True,
) -> dict[str, Any]:
    """Structured inference-endpoint readiness for ``namespace``. Read-only; auto-runs.

    Returns ``ready`` (the gate), the per-service ready/not-ready endpoint address counts, an
    optional benchmark-CLI endpoint count, and — when NOT ready — a ``standup_suggestion`` the
    agent can act on (offer an approval-gated standup). Never mutates.

    When a Service exists but has NO ready backing endpoint (the Running-but-NotReady case), it
    ALSO classifies WHY by gathering model-load serving-readiness FACTS (Phase 59): the pod
    readiness conditions / restartCount / age from ``kubectl get pods`` plus the verbatim
    outcomes of two tightly-constrained GET probes — ``/v1/models`` (model-serving-ready) and
    ``/health`` (process-alive) — on the model-server port. Those facts ride on
    ``serving_readiness``; the loading-vs-broken VERDICT (still loading weights — keep waiting —
    vs wedged/broken — stop) is the agent's, driven by ``knowledge/readiness_probes.md``
    (``read_knowledge('readiness_probes')``), never a Python branch.

    In GATEWAY-mode deploys (``check_gateway=True``, the default), it ALSO reads the Gateway-API
    control plane — ``kubectl get gateway,gatewayclass,inferencepool,httproute -o json`` — and folds
    the PROGRAMMED / Accepted / ResolvedRefs / Reconciled conditions onto ``gateway`` (Phase 65).
    This distinguishes "the model pods are Ready" from "traffic can actually REACH them": pods can be
    Ready while the Gateway is still ``PROGRAMMED:False`` (or the InferencePool is unresolved), so no
    traffic flows yet. Those are FACTS; when the control plane is not wired the result carries a
    ``gateway_readiness_guidance`` pointer — the wait-vs-stand-up-vs-config-error JUDGMENT lives in
    ``knowledge/gateway_readiness.md`` (``read_knowledge('gateway_readiness')``), never a Python
    branch. Set ``check_gateway=False`` on non-gateway/Kind deploys to skip the four extra reads."""
    if not shutil.which("kubectl"):
        raise ToolError("kubectl is not on PATH — cannot check endpoint readiness")

    verdict = await _kube_endpoint_readiness(ctx, namespace)

    cli_seen: int | None = None
    if probe_cli_endpoints and verdict.reason in {"endpoints_ready", "endpoints_not_ready", "no_endpoints"}:
        cli_seen = await _cli_endpoints_seen(ctx, namespace, spec)
    verdict.cli_endpoints_seen = cli_seen

    # Running-but-NotReady: classify still-loading vs wedged via /v1/models vs /health + pod facts.
    if verdict.reason == "endpoints_not_ready":
        verdict.serving_readiness = await _serving_readiness(ctx, namespace, verdict)

    # Gateway-mode: read the Gateway-API control plane so the agent can tell "pods Ready" from
    # "traffic reaches them" (PROGRAMMED / Accepted / ResolvedRefs facts). Read-only; best-effort.
    if check_gateway:
        verdict.gateway = await _gateway_readiness(ctx, namespace)

    out = verdict.as_dict()
    if not verdict.ready:
        out["standup_suggestion"] = _standup_suggestion(namespace, spec, verdict.reason)
        if verdict.serving_readiness is not None:
            # Point the agent at the judgment knowledge so it reports loading-vs-broken (and the
            # recommended wait/stop action) before any benchmark is submitted.
            out["serving_readiness_guidance"] = {
                "read_knowledge": "readiness_probes",
                "why": "a Service exists but is not serving yet — these are the model-load "
                       "facts (/v1/models vs /health + pod conditions). Read "
                       "knowledge/readiness_probes.md to decide 'still loading weights (keep "
                       "waiting)' vs 'wedged/broken (stop)' before submitting a benchmark.",
            }
    # Gateway control-plane not wired (a FACT from the conditions): point the agent at the
    # judgment knowledge so it narrates "pods up but traffic can't reach them yet" and picks
    # wait-vs-stand-up-vs-config-error — even when the endpoints themselves look ready.
    if verdict.gateway is not None and verdict.gateway.not_ready_reason is not None:
        out["gateway_readiness_guidance"] = {
            "read_knowledge": "gateway_readiness",
            "reason": verdict.gateway.not_ready_reason,
            "why": "the model pods may be Ready, but the Gateway-API control plane is not wired "
                   f"({verdict.gateway.not_ready_reason}) — no traffic reaches the pods yet. Read "
                   "knowledge/gateway_readiness.md to decide 'still provisioning (keep waiting)' "
                   "vs 'stand up the missing piece' vs 'surface a config error' before targeting "
                   "the Gateway with a benchmark.",
        }
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
