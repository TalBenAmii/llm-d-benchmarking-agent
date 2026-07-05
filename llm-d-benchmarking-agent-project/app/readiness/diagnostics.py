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

Pure functions, no I/O — the tool layer (:mod:`app.readiness.probes`) feeds it live output.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# Model-server serving ports, by pod role (Phase 59 / llm-d docs/readiness-probes.md).
# A container that exposes one of these is the vLLM API surface we probe for serving-readiness.
_ROLE_BY_PORT = {8000: "prefill", 8200: "decode"}


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
    # Phase 59: when the endpoint is Running-but-NotReady, the model-load serving-readiness
    # FACTS (pod conditions + /v1/models vs /health probe results). None when not classified
    # (e.g. the endpoint is already serving, or no pod facts were gathered). The loading-vs-broken
    # JUDGMENT lives in knowledge/readiness_probes.md, NOT here — this is signals only.
    serving_readiness: ServingReadiness | None = None
    # Phase 65: in GATEWAY-mode deploys, the Gateway-API control-plane readiness FACTS (Gateway
    # PROGRAMMED, InferencePool Accepted/ResolvedRefs, HTTPRoute Accepted/Reconciled, GatewayClass
    # existence). None when not probed (Kind/non-gateway deploys). This distinguishes "model pods
    # are Ready" from "traffic can actually reach them": pods can be Ready while the Gateway is
    # still PROGRAMMED:False (or the InferencePool is unresolved), so no traffic flows yet. The
    # wait-vs-standup-vs-config-error JUDGMENT lives in knowledge/gateway_readiness.md, NOT here.
    gateway: GatewayReadiness | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "ready": self.ready,
            "reason": self.reason,
            "detail": self.detail,
            "ready_endpoints": self.ready_endpoints,
            "not_ready_endpoints": self.not_ready_endpoints,
            "cli_endpoints_seen": self.cli_endpoints_seen,
            "serving_readiness": (
                self.serving_readiness.as_dict() if self.serving_readiness is not None else None
            ),
            "gateway": (self.gateway.as_dict() if self.gateway is not None else None),
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
) -> EndpointReadiness:
    """Turn ``kubectl get endpoints -n <ns> -o json`` output into a readiness verdict.

    ``endpoints_json`` is the raw stdout (a v1 ``List`` of ``Endpoints``); a single object or
    empty/garbage input degrades gracefully to "not ready" (never raises). The default
    ``kubernetes`` Service endpoint (the API server) is ignored — it is always present and
    never an inference endpoint, so counting it would mask an unready stack."""
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
        )

    return EndpointReadiness(
        namespace=namespace, ready=False, reason="no_endpoints",
        detail=f"no inference service endpoints found in namespace {namespace!r} — "
               f"there is no stack to benchmark here.",
        ready_endpoints=ready_eps, not_ready_endpoints=not_ready_eps,
    )


@dataclass
class GatewayReadiness:
    """Gateway-API control-plane readiness FACTS for one namespace (Phase 65 — gateway-mode).

    Extends the endpoint-readiness gate from "the model pods are Ready" to "traffic can actually
    REACH them". In a gateway-mode deploy (GKE/Istio/agentgateway) the data plane is a Gateway →
    HTTPRoute → InferencePool → EPP chain; a benchmark targeting the Gateway address can fail even
    though every model pod is Ready, because the Gateway is still ``PROGRAMMED:False`` (the
    LoadBalancer/controller hasn't provisioned it) or the InferencePool hasn't reached
    ``ResolvedRefs:True`` (its EPP/backend refs don't resolve), or the HTTPRoute isn't
    ``Reconciled`` (the GKE *fault filter abort* symptom). See guides/prereq/gateways/gke.md.

    This is **mechanism only** — it carries the raw condition signals extracted verbatim from
    ``kubectl get gateway,gatewayclass,inferencepool,httproute -o json`` status. It does NOT decide
    wait-vs-stand-up-vs-config-error; that JUDGMENT (how long ``PROGRAMMED:False`` should take,
    what *fault filter abort* implies, when to surface a misconfiguration) lives in
    ``knowledge/gateway_readiness.md`` and is applied by the LLM over these facts. There is NO
    wait/standup/error if/elif here.

    Facts:
      * ``programmed`` — Gateway ``status.conditions[type=='Programmed'].status`` mapped to a bool
        (True/False) or None when the condition is absent/unknown (no Gateway, or not yet stamped).
      * ``gatewayclass_exists`` — whether any GatewayClass object is present (the controller class
        the Gateway binds to). A missing GatewayClass is a common cause of a never-PROGRAMMED Gateway.
      * ``inferencepools`` — per-InferencePool ``{name, accepted, resolved_refs}`` from
        ``status.parents[].conditions`` (Accepted / ResolvedRefs); booleans or None when absent.
      * ``httproutes`` — per-HTTPRoute ``{name, accepted, reconciled}`` from
        ``status.parents[].conditions`` (Accepted / Reconciled).
      * ``control_plane_ready`` — a DERIVED FACT (not a decision): True only when a Gateway is
        PROGRAMMED, a GatewayClass exists, and every InferencePool present is ResolvedRefs:True. It
        is the "can traffic reach the pods?" companion to endpoint readiness — the agent still reads
        knowledge/gateway_readiness.md to choose the ACTION."""

    namespace: str
    programmed: bool | None = None
    gateway_name: str | None = None
    gatewayclass_name: str | None = None
    gatewayclass_exists: bool = False
    inferencepools: list[dict[str, Any]] = field(default_factory=list)
    httproutes: list[dict[str, Any]] = field(default_factory=list)

    @property
    def inferencepools_resolved(self) -> bool:
        """True only when at least one InferencePool is present AND every present pool is
        ResolvedRefs:True. False if any pool is unresolved/None or none exist. A FACT, not a
        decision."""
        if not self.inferencepools:
            return False
        return all(p.get("resolved_refs") is True for p in self.inferencepools)

    @property
    def control_plane_ready(self) -> bool:
        """Derived FACT: the Gateway-API data path is fully wired — Gateway PROGRAMMED:True, a
        GatewayClass exists, and every InferencePool present is ResolvedRefs:True. This says
        "traffic CAN reach the pods"; it does NOT say what to do when it's False (that's the
        knowledge file's judgment)."""
        return bool(self.programmed) and self.gatewayclass_exists and self.inferencepools_resolved

    @property
    def not_ready_reason(self) -> str | None:
        """A single gateway-specific reason TOKEN naming the first control-plane gap, as a FACT
        for the agent to narrate (``gatewayclass_missing`` / ``gateway_not_programmed`` /
        ``inferencepool_unresolved``). None when the control plane is ready. This labels WHICH
        condition is unmet; the wait-vs-standup-vs-error ACTION stays in knowledge/."""
        if self.control_plane_ready:
            return None
        if not self.gatewayclass_exists:
            return "gatewayclass_missing"
        if not self.programmed:
            return "gateway_not_programmed"
        if not self.inferencepools_resolved:
            return "inferencepool_unresolved"
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "programmed": self.programmed,
            "gateway_name": self.gateway_name,
            "gatewayclass_name": self.gatewayclass_name,
            "gatewayclass_exists": self.gatewayclass_exists,
            "inferencepools": self.inferencepools,
            "inferencepools_resolved": self.inferencepools_resolved,
            "httproutes": self.httproutes,
            "control_plane_ready": self.control_plane_ready,
            "not_ready_reason": self.not_ready_reason,
        }


def _condition_status(conditions: list[Any], wanted_type: str) -> bool | None:
    """Map a Kubernetes ``status.conditions[]`` entry of ``type == wanted_type`` to a bool
    (``"True"`` -> True, ``"False"`` -> False), or None when absent/unknown. Pure mechanism —
    it copies the controller's stamped condition through, it never interprets it."""
    for cond in conditions or []:
        if isinstance(cond, dict) and cond.get("type") == wanted_type:
            status = cond.get("status")
            if status == "True":
                return True
            if status == "False":
                return False
            return None
    return None


def _parent_conditions(obj: dict[str, Any]) -> list[Any]:
    """Flatten ``status.parents[].conditions[]`` (Gateway-API per-parentRef status) into one list.

    An InferencePool/HTTPRoute reports its Accepted/ResolvedRefs/Reconciled conditions per
    parentRef (the Gateway it attaches to); for the readiness FACT we fold every parent's
    conditions together (a route accepted by ANY parent surfaces that Accepted condition)."""
    out: list[Any] = []
    for parent in (obj.get("status") or {}).get("parents") or []:
        if isinstance(parent, dict):
            out.extend(parent.get("conditions") or [])
    return out


def analyze_gateway(
    *,
    namespace: str,
    gateway_json: str = "",
    gatewayclass_json: str = "",
    inferencepool_json: str = "",
    httproute_json: str = "",
) -> GatewayReadiness:
    """Fold ``kubectl get gateway|gatewayclass|inferencepool|httproute -o json`` into Gateway-API
    control-plane readiness FACTS (Phase 65). Each argument is the raw stdout of one read-only
    ``get -o json``; any empty/garbage input degrades to "absent" (never raises).

    Extracts, as FACTS only:
      * the Gateway ``Programmed`` condition (``status.conditions``) -> ``programmed``;
      * whether any GatewayClass exists -> ``gatewayclass_exists``;
      * per-InferencePool ``Accepted`` / ``ResolvedRefs`` from ``status.parents[].conditions``;
      * per-HTTPRoute ``Accepted`` / ``Reconciled`` from ``status.parents[].conditions``.

    **Mechanism only**: it reads conditions and copies them through. It does NOT branch on
    wait-vs-stand-up-vs-config-error — that judgment (what ``PROGRAMMED:False`` means, how long it
    should take, what the *fault filter abort* symptom implies) lives in
    ``knowledge/gateway_readiness.md`` and is applied by the LLM over these facts."""
    gateways = _parse_items(gateway_json)
    gatewayclasses = _parse_items(gatewayclass_json)
    pools = _parse_items(inferencepool_json)
    routes = _parse_items(httproute_json)

    # Gateway PROGRAMMED: take the first Gateway whose Programmed condition is stamped; otherwise
    # the first Gateway's (possibly None) status; None when no Gateway exists at all.
    programmed: bool | None = None
    gateway_name: str | None = None
    for gw in gateways:
        gw_name = (gw.get("metadata") or {}).get("name", "") or None
        gw_conds = (gw.get("status") or {}).get("conditions") or []
        prog = _condition_status(gw_conds, "Programmed")
        if gateway_name is None:
            gateway_name = gw_name
            programmed = prog
        if prog is not None:  # prefer a Gateway that actually carries a Programmed verdict
            gateway_name = gw_name
            programmed = prog
            break

    gatewayclass_name = None
    for gc in gatewayclasses:
        gatewayclass_name = (gc.get("metadata") or {}).get("name", "") or None
        if gatewayclass_name:
            break

    inferencepools: list[dict[str, Any]] = []
    for pool in pools:
        conds = _parent_conditions(pool)
        inferencepools.append({
            "name": (pool.get("metadata") or {}).get("name", ""),
            "accepted": _condition_status(conds, "Accepted"),
            "resolved_refs": _condition_status(conds, "ResolvedRefs"),
        })

    httproutes: list[dict[str, Any]] = []
    for route in routes:
        conds = _parent_conditions(route)
        httproutes.append({
            "name": (route.get("metadata") or {}).get("name", ""),
            "accepted": _condition_status(conds, "Accepted"),
            "reconciled": _condition_status(conds, "Reconciled"),
        })

    return GatewayReadiness(
        namespace=namespace,
        programmed=programmed,
        gateway_name=gateway_name,
        gatewayclass_name=gatewayclass_name,
        gatewayclass_exists=bool(gatewayclasses),
        inferencepools=inferencepools,
        httproutes=httproutes,
    )


@dataclass
class ServingReadiness:
    """Model-load serving-readiness FACTS for a Running-but-NotReady inference pod (Phase 59).

    This is **mechanism only** — it carries the raw signals that distinguish "still loading
    model weights (legitimate — keep waiting)" from "wedged/broken (stop waiting)", WITHOUT
    deciding between them. The decision (the failureThreshold*periodSeconds startup budget,
    and "``/health`` 200 + ``/v1/models`` 503 => still loading" vs "``/health`` refused or a
    high restartCount => wedged") lives in ``knowledge/readiness_probes.md`` and is made by the
    LLM reading these facts — there is NO loading-vs-broken if/elif here.

    Per-pod facts (``pods``) come from ``kubectl get pods -o json``: phase, the Ready /
    ContainersReady conditions, the max ``restartCount`` across containers, the pod age, and the
    serving role inferred from the container port (8000 prefill / 8200 decode). The probe facts
    (``health_*`` / ``models_*``) are the verbatim HTTP outcomes of GET ``/health`` (liveness:
    process-alive) and GET ``/v1/models`` (readiness/startup: model-serving-ready)."""

    namespace: str
    pods: list[dict[str, Any]] = field(default_factory=list)
    # The probe facts (copied through verbatim from the constrained curl GETs; None when a probe
    # was not run). ``*_reachable`` is False on a connection-refused / unreachable outcome.
    health_status_code: int | None = None
    health_reachable: bool = True
    models_status_code: int | None = None
    models_reachable: bool = True

    @property
    def max_restart_count(self) -> int:
        """The highest container restartCount across all observed pods (0 when unknown)."""
        return max((int(p.get("restart_count") or 0) for p in self.pods), default=0)

    @property
    def youngest_age_seconds(self) -> int | None:
        """Age (s) of the youngest observed pod, or None when no age could be read. The
        youngest is the relevant one for 'has it had time to load yet?' — a freshly-(re)created
        pod resets the load clock."""
        ages = [p["age_seconds"] for p in self.pods if p.get("age_seconds") is not None]
        return min(ages) if ages else None

    @property
    def roles(self) -> list[str]:
        """The serving roles (prefill/decode) inferred from the pods' container ports."""
        seen: list[str] = []
        for p in self.pods:
            r = p.get("role")
            if r and r not in seen:
                seen.append(r)
        return seen

    def as_dict(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "pods": self.pods,
            "health_status_code": self.health_status_code,
            "health_reachable": self.health_reachable,
            "models_status_code": self.models_status_code,
            "models_reachable": self.models_reachable,
            "max_restart_count": self.max_restart_count,
            "youngest_age_seconds": self.youngest_age_seconds,
            "roles": self.roles,
        }


def _as_int(value: Any) -> int:
    """Coerce a (possibly forged/corrupt) count to a non-negative int — a non-numeric value
    reads as 0 rather than crashing. A real ``kubectl get pods -o json`` always emits integer
    ``restartCount``s, but a corrupt/forged status object must not abort the readiness gate
    (the module's documented 'garbage pods_json degrades, never raises' invariant; same hardening
    class as the orchestrator's BUG-023/029/037)."""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _role_for_ports(ports: list[int]) -> str | None:
    """Infer the serving role from a container's exposed ports (8000 prefill / 8200 decode)."""
    for port in ports:
        role = _ROLE_BY_PORT.get(port)
        if role:
            return role
    return None


def _age_seconds(creation_ts: str | None, *, now: datetime | None = None) -> int | None:
    """Pod age in seconds from an RFC3339 ``creationTimestamp`` (e.g. ``2024-01-02T03:04:05Z``),
    or None if absent/unparseable. ``now`` is injectable so tests are deterministic."""
    if not creation_ts:
        return None
    raw = creation_ts.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        created = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    return max(0, int((current - created).total_seconds()))


def classify_serving_readiness(
    pods_json: str,
    *,
    namespace: str,
    health_status: int | None = None,
    models_status: int | None = None,
    health_reachable: bool = True,
    models_reachable: bool = True,
    now: datetime | None = None,
) -> ServingReadiness:
    """Extract model-load serving-readiness FACTS for an inference namespace (Phase 59).

    Parses ``kubectl get pods -n <ns> -o json`` (``pods_json``) into per-pod facts — phase, the
    Ready / ContainersReady conditions, the max container ``restartCount``, the pod age, and the
    serving role (8000 prefill / 8200 decode) — and folds in the verbatim outcomes of the two
    constrained probes: GET ``/health`` (liveness) and GET ``/v1/models`` (model-serving
    readiness). Empty/garbage ``pods_json`` degrades to no pods (never raises).

    **Mechanism only**: it collects and copies signals through. It does NOT contain a
    loading-vs-broken if/elif — that judgment (the failureThreshold*periodSeconds startup budget;
    "``/health`` 200 + ``/v1/models`` 503 => still loading weights"; "``/health`` refused or a high
    restartCount => wedged/broken") lives entirely in ``knowledge/readiness_probes.md`` and is
    applied by the LLM over these facts. ``now`` is injectable for deterministic age tests."""
    pods: list[dict[str, Any]] = []
    for obj in _parse_items(pods_json):
        meta = obj.get("metadata") or {}
        status = obj.get("status") or {}
        conds = {c.get("type"): c.get("status")
                 for c in (status.get("conditions") or []) if isinstance(c, dict)}
        container_statuses = status.get("containerStatuses") or []
        restart_count = max(
            (_as_int(cs.get("restartCount")) for cs in container_statuses if isinstance(cs, dict)),
            default=0,
        )
        ports: list[int] = []
        for container in (obj.get("spec") or {}).get("containers") or []:
            for p in (container.get("ports") or []):
                cp = p.get("containerPort")
                if isinstance(cp, int):
                    ports.append(cp)
        pods.append({
            "name": meta.get("name", ""),
            "phase": status.get("phase"),
            "ready_condition": conds.get("Ready"),
            "containers_ready_condition": conds.get("ContainersReady"),
            "restart_count": restart_count,
            "age_seconds": _age_seconds(meta.get("creationTimestamp"), now=now),
            "ports": ports,
            "role": _role_for_ports(ports),
        })

    return ServingReadiness(
        namespace=namespace,
        pods=pods,
        health_status_code=health_status,
        health_reachable=health_reachable,
        models_status_code=models_status,
        models_reachable=models_reachable,
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
