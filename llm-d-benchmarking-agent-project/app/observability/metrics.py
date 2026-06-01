"""A tiny, dependency-free metrics registry with Prometheus text-format exposition.

Why hand-rolled rather than ``prometheus_client``: the agent has a deliberately small,
audited dependency surface (see ``pyproject.toml``); the metrics we export are a handful of
counters/gauges/histograms, and a correct text-format exporter is ~100 lines. This keeps the
package self-contained and the exposition format easy to reason about.

This module is **pure mechanism**: it stores numbers and renders them. It embeds no judgment
about which metrics matter or what a value *means* — that is the agent's job, guided by
``knowledge/observability.md``. Metric *definitions* (names/help/labels) live in
``app.observability.instrument``.

Concurrency: metric mutations are guarded by a lock so the orchestrator's parallel sweeps and
multiple sessions can record concurrently without losing updates. Exposition takes a snapshot
under the same lock.
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from typing import Dict, Iterable, Mapping, Sequence, Tuple

# A label set is an ordered tuple of (name, value) pairs — ordered+hashable so it keys a dict.
LabelKey = Tuple[Tuple[str, str], ...]


def _normalize_labels(labels: Mapping[str, str] | None) -> LabelKey:
    """Canonicalize a label mapping into a sorted, stringified tuple key.

    Sorting makes ``{a:1,b:2}`` and ``{b:2,a:1}`` the same series; stringifying lets callers
    pass ints/enums without ceremony. None/empty → the empty series."""
    if not labels:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


def _escape_label_value(value: str) -> str:
    """Escape a label value per the Prometheus exposition format (backslash, double-quote,
    newline)."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_value(value: float) -> str:
    """Render a number the way Prometheus expects: +Inf/-Inf/NaN spelled out, integers without
    a trailing ``.0`` (cosmetic, keeps counters readable), everything else with repr precision."""
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if math.isnan(value):
        return "NaN"
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


def _render_labels(label_key: LabelKey, extra: Sequence[Tuple[str, str]] = ()) -> str:
    pairs = list(label_key) + list(extra)
    if not pairs:
        return ""
    inner = ",".join(f'{name}="{_escape_label_value(val)}"' for name, val in pairs)
    return "{" + inner + "}"


@dataclass
class _Series:
    """One labelled time-series of a metric (a single value, or histogram buckets+sum+count)."""

    value: float = 0.0
    # Histogram-only: cumulative bucket counts keyed by upper bound, plus sum and count.
    buckets: Dict[float, int] = field(default_factory=dict)
    hist_sum: float = 0.0
    hist_count: int = 0


class _Metric:
    """Base for the three metric types. Holds per-label-set series and its metadata."""

    metric_type = "untyped"

    def __init__(self, name: str, help_text: str, registry: "MetricsRegistry"):
        self.name = name
        self.help_text = help_text
        self._registry = registry
        self._series: Dict[LabelKey, _Series] = {}

    def _series_for(self, label_key: LabelKey) -> _Series:
        s = self._series.get(label_key)
        if s is None:
            s = _Series()
            self._series[label_key] = s
        return s

    def snapshot(self) -> Dict[LabelKey, _Series]:
        # Caller holds the registry lock; return the live mapping (rendered immediately).
        return self._series


class Counter(_Metric):
    """Monotonically increasing total (resets only on process restart)."""

    metric_type = "counter"

    def inc(self, amount: float = 1.0, *, labels: Mapping[str, str] | None = None) -> None:
        if amount < 0:
            raise ValueError("Counter.inc amount must be >= 0")
        key = _normalize_labels(labels)
        with self._registry.lock:
            self._series_for(key).value += amount


class Gauge(_Metric):
    """An instantaneous value that can go up or down (e.g. in-flight runs)."""

    metric_type = "gauge"

    def set(self, value: float, *, labels: Mapping[str, str] | None = None) -> None:
        key = _normalize_labels(labels)
        with self._registry.lock:
            self._series_for(key).value = float(value)

    def inc(self, amount: float = 1.0, *, labels: Mapping[str, str] | None = None) -> None:
        key = _normalize_labels(labels)
        with self._registry.lock:
            self._series_for(key).value += amount

    def dec(self, amount: float = 1.0, *, labels: Mapping[str, str] | None = None) -> None:
        self.inc(-amount, labels=labels)


# Default histogram buckets (seconds) tuned for command/run durations: sub-second probes up to
# multi-hour benchmark runs. +Inf is always appended by render.
DEFAULT_DURATION_BUCKETS: Tuple[float, ...] = (
    0.05, 0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600, 1800, 3600,
)


class Histogram(_Metric):
    """Cumulative histogram: per-bucket counts (<= upper bound), plus sum and count. Renders the
    standard ``_bucket``/``_sum``/``_count`` series Prometheus/Grafana expect."""

    metric_type = "histogram"

    def __init__(self, name: str, help_text: str, registry: "MetricsRegistry",
                 buckets: Sequence[float] = DEFAULT_DURATION_BUCKETS):
        super().__init__(name, help_text, registry)
        # Sorted, de-duplicated finite upper bounds; +Inf is implicit and added at render time.
        self._bounds: Tuple[float, ...] = tuple(sorted({float(b) for b in buckets}))

    @property
    def bounds(self) -> Tuple[float, ...]:
        return self._bounds

    def observe(self, value: float, *, labels: Mapping[str, str] | None = None) -> None:
        key = _normalize_labels(labels)
        with self._registry.lock:
            s = self._series_for(key)
            if not s.buckets:
                s.buckets = {b: 0 for b in self._bounds}
            for b in self._bounds:
                if value <= b:
                    s.buckets[b] += 1
            s.hist_sum += float(value)
            s.hist_count += 1


class MetricsRegistry:
    """Owns a set of named metrics and renders them. A single registry per process is the norm
    (see ``app.observability.instrument.REGISTRY``); tests build throwaway registries."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self._metrics: Dict[str, _Metric] = {}

    def _register(self, metric: _Metric) -> _Metric:
        existing = self._metrics.get(metric.name)
        if existing is not None:
            if type(existing) is not type(metric):
                raise ValueError(
                    f"metric {metric.name!r} already registered as {type(existing).__name__}"
                )
            return existing
        self._metrics[metric.name] = metric
        return metric

    def counter(self, name: str, help_text: str) -> Counter:
        return self._register(Counter(name, help_text, self))  # type: ignore[return-value]

    def gauge(self, name: str, help_text: str) -> Gauge:
        return self._register(Gauge(name, help_text, self))  # type: ignore[return-value]

    def histogram(self, name: str, help_text: str,
                  buckets: Sequence[float] = DEFAULT_DURATION_BUCKETS) -> Histogram:
        return self._register(Histogram(name, help_text, self, buckets))  # type: ignore[return-value]

    def metrics(self) -> Iterable[_Metric]:
        return list(self._metrics.values())

    def render(self) -> str:
        return render_prometheus(self)


def render_prometheus(registry: MetricsRegistry) -> str:
    """Render a registry into the Prometheus text exposition format (v0.0.4). Stable, sorted
    output so a scrape/diff is deterministic. Takes the lock for a consistent snapshot."""
    lines: list[str] = []
    with registry.lock:
        for metric in sorted(registry.metrics(), key=lambda m: m.name):
            lines.append(f"# HELP {metric.name} {metric.help_text}")
            lines.append(f"# TYPE {metric.name} {metric.metric_type}")
            series = metric.snapshot()
            if isinstance(metric, Histogram):
                _render_histogram(lines, metric, series)
            else:
                for label_key in sorted(series):
                    lines.append(f"{metric.name}{_render_labels(label_key)} "
                                 f"{_format_value(series[label_key].value)}")
    return "\n".join(lines) + "\n"


def _render_histogram(lines: list[str], metric: Histogram,
                      series: Dict[LabelKey, _Series]) -> None:
    for label_key in sorted(series):
        s = series[label_key]
        cumulative = 0
        for b in metric.bounds:
            cumulative = s.buckets.get(b, 0)
            lines.append(
                f"{metric.name}_bucket"
                f"{_render_labels(label_key, [('le', _format_value(b))])} "
                f"{_format_value(cumulative)}"
            )
        # The mandatory +Inf bucket equals the total count.
        lines.append(
            f"{metric.name}_bucket"
            f"{_render_labels(label_key, [('le', '+Inf')])} {_format_value(s.hist_count)}"
        )
        lines.append(f"{metric.name}_sum{_render_labels(label_key)} {_format_value(s.hist_sum)}")
        lines.append(f"{metric.name}_count{_render_labels(label_key)} {_format_value(s.hist_count)}")
