"""Observability: a tiny, dependency-free metrics registry + Prometheus text exposition
(Phase 7), structured JSON logging with per-turn correlation ids (Phase 11), instrumentation
of the existing central mechanism points, and a `/metrics` endpoint. Mechanism only — *what*
a metric means, *when* to act on it, and how to read a correlated log trail live in
``knowledge/`` and the agent's reasoning, never in this package.
"""
from __future__ import annotations

from app.observability.logging import (
    JsonFormatter,
    get_corr_id,
    new_corr_id,
    setup_logging,
)
from app.observability.logging import bind as log_bind
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
    "setup_logging",
    "JsonFormatter",
    "log_bind",
    "get_corr_id",
    "new_corr_id",
]
