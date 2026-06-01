"""Observability (Phase 7): a tiny, dependency-free metrics registry + Prometheus text
exposition, instrumentation of the existing central mechanism points, and a `/metrics`
endpoint. Mechanism only — *what* a metric means and *when* to act on it lives in
``knowledge/observability.md`` and the agent's reasoning, never in this package.
"""
from __future__ import annotations

from app.observability.metrics import (
    Counter,
    Gauge,
    Histogram,
    MetricsRegistry,
    render_prometheus,
)

__all__ = [
    "Counter",
    "Gauge",
    "Histogram",
    "MetricsRegistry",
    "render_prometheus",
]
