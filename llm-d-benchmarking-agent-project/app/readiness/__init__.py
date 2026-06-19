"""Endpoint/stack readiness — one deep module.

A single seam for "is the inference endpoint actually serving?": cluster-access
probes (``probes.py``) over a pure facts-only analyzer (``diagnostics.py``). The
wait-vs-stand-up-vs-config-error JUDGMENT is never here — it lives in
``knowledge/gateway_readiness.md`` and is applied by the LLM over these facts.

Public surface (import from ``app.readiness``):
- ``check_endpoint_readiness`` — the readiness tool handler.
- the analyzer dataclasses + functions, re-exported for callers/tests that want
  the facts layer directly.
"""

from __future__ import annotations

from app.readiness.diagnostics import (
    EndpointReadiness,
    GatewayReadiness,
    ServingReadiness,
    analyze_endpoints,
    analyze_gateway,
    classify_serving_readiness,
)
from app.readiness.probes import check_endpoint_readiness

__all__ = [
    "EndpointReadiness",
    "GatewayReadiness",
    "ServingReadiness",
    "analyze_endpoints",
    "analyze_gateway",
    "check_endpoint_readiness",
    "classify_serving_readiness",
]
