"""Inference-endpoint readiness — pure analysis (no cluster access).

The orchestrator must not submit a benchmark Job against a stack whose inference endpoint
is not actually *serving*. Today's ``probe_environment`` ``stack`` check only proves a pod
*exists and reports Ready* — it does NOT prove the **Service has ready backing endpoints**
(the Kubernetes notion of an endpoint that can actually receive traffic) nor that the
benchmark CLI can see an inference endpoint to target. This module turns those richer signals
into a single structured verdict.

It is **mechanism only**: it parses ``kubectl get endpoints -o json`` (and, optionally, the
benchmark CLI's own ``run --list-endpoints`` output) into facts. WHETHER to stand up a stack
when none is ready — and the (approval-gated) decision to do so — is the agent's judgment
(``knowledge/orchestrator.md`` / ``knowledge/preconditions.md``); this never mutates anything.

Pure functions, no I/O — the tool layer (:mod:`app.tools.readiness`) feeds it live output.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class EndpointReadiness:
    """A structured endpoint-readiness verdict over one namespace.

    ``ready`` is the load-bearing gate: True only when at least one Service in the namespace
    has at least one **ready** backing address (a serviceable endpoint), which is strictly
    stronger than "a pod is present/Running". ``ready_endpoints`` lists the
    ``service -> ready/notReady address counts`` so the agent can narrate exactly what is (or
    is not) serving. ``reason`` is a short machine token; ``detail`` is human-facing."""

    namespace: str
    ready: bool
    reason: str
    detail: str
    ready_endpoints: list[dict[str, Any]] = field(default_factory=list)
    not_ready_endpoints: list[dict[str, Any]] = field(default_factory=list)
    cli_endpoints_seen: int | None = None  # from `run --list-endpoints`, when probed; else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "ready": self.ready,
            "reason": self.reason,
            "detail": self.detail,
            "ready_endpoints": self.ready_endpoints,
            "not_ready_endpoints": self.not_ready_endpoints,
            "cli_endpoints_seen": self.cli_endpoints_seen,
        }


def _endpoints_subsets(ep_obj: dict[str, Any]) -> tuple[int, int]:
    """Count (ready, not_ready) backing addresses across an Endpoints object's subsets.

    A Kubernetes ``Endpoints`` object lists, per ``subset``, the ``addresses`` that are ready
    to receive traffic and the ``notReadyAddresses`` that are not (failing readiness probes,
    terminating, etc.). Summing these is the canonical "does this Service actually have a live
    backend?" signal — distinct from a pod merely existing."""
    ready = 0
    not_ready = 0
    for subset in ep_obj.get("subsets") or []:
        ready += len(subset.get("addresses") or [])
        not_ready += len(subset.get("notReadyAddresses") or [])
    return ready, not_ready


def analyze_endpoints(
    endpoints_json: str,
    *,
    namespace: str,
    cli_endpoints_seen: int | None = None,
) -> EndpointReadiness:
    """Turn ``kubectl get endpoints -n <ns> -o json`` output into a readiness verdict.

    ``endpoints_json`` is the raw stdout (a v1 ``List`` of ``Endpoints``); a single object or
    empty/garbage input degrades gracefully to "not ready" (never raises). The default
    ``kubernetes`` Service endpoint (the API server) is ignored — it is always present and
    never an inference endpoint, so counting it would mask an unready stack.

    ``cli_endpoints_seen`` (optional) is how many inference endpoints the benchmark CLI's own
    ``run --list-endpoints`` reported; it is carried through for the agent but the gate is
    driven by the authoritative Kubernetes endpoint-address readiness."""
    items = _parse_items(endpoints_json)
    ready_eps: list[dict[str, Any]] = []
    not_ready_eps: list[dict[str, Any]] = []

    for obj in items:
        name = (obj.get("metadata") or {}).get("name", "")
        if name == "kubernetes":  # the API server's own Service — never an inference endpoint
            continue
        ready, not_ready = _endpoints_subsets(obj)
        entry = {"service": name, "ready_addresses": ready, "not_ready_addresses": not_ready}
        if ready > 0:
            ready_eps.append(entry)
        else:
            not_ready_eps.append(entry)

    if ready_eps:
        names = ", ".join(e["service"] for e in ready_eps)
        return EndpointReadiness(
            namespace=namespace, ready=True, reason="endpoints_ready",
            detail=f"{len(ready_eps)} service(s) have ready backing endpoints ({names}).",
            ready_endpoints=ready_eps, not_ready_endpoints=not_ready_eps,
            cli_endpoints_seen=cli_endpoints_seen,
        )

    if not_ready_eps:
        # Services exist but NONE has a ready backing address — the stack is standing up or
        # unhealthy (pods present, but not yet serving). Strictly beyond pod-presence.
        names = ", ".join(e["service"] for e in not_ready_eps)
        return EndpointReadiness(
            namespace=namespace, ready=False, reason="endpoints_not_ready",
            detail=f"service(s) exist but have NO ready backing endpoints yet ({names}) — "
                   f"the inference stack is not serving (pods present but not ready).",
            ready_endpoints=ready_eps, not_ready_endpoints=not_ready_eps,
            cli_endpoints_seen=cli_endpoints_seen,
        )

    return EndpointReadiness(
        namespace=namespace, ready=False, reason="no_endpoints",
        detail=f"no inference service endpoints found in namespace {namespace!r} — "
               f"there is no stack to benchmark here.",
        ready_endpoints=ready_eps, not_ready_endpoints=not_ready_eps,
        cli_endpoints_seen=cli_endpoints_seen,
    )


def _parse_items(text: str) -> list[dict[str, Any]]:
    """Parse ``kubectl get ... -o json`` into a list of objects (a ``List`` → its items; a
    single object → a one-element list; empty/garbage → ``[]``). Never raises."""
    raw = (text or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict) and "items" in data:
        return [i for i in (data.get("items") or []) if isinstance(i, dict)]
    if isinstance(data, dict) and data.get("kind"):
        return [data]
    return []
