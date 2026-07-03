"""A tiny, dependency-free metrics registry with Prometheus text-format exposition, plus the
agent + orchestrator metric definitions and record helpers wired into the existing central
mechanism points.

Why hand-rolled rather than ``prometheus_client``: the agent has a deliberately small,
audited dependency surface (see ``pyproject.toml``); the metrics we export are a handful of
counters/gauges/histograms, and a correct text-format exporter is ~100 lines. This keeps the
package self-contained and the exposition format easy to reason about.

This module is **pure mechanism**: it stores numbers and renders them. It embeds no judgment
about which metrics matter or what a value *means* — that is the agent's job, guided by
``knowledge/observability.md``. Metric *definitions* (names/help/labels) live in the
"metric definitions + record helpers" section below.

Concurrency: metric mutations are guarded by a lock so the orchestrator's parallel sweeps and
multiple sessions can record concurrently without losing updates. Exposition takes a snapshot
under the same lock.

Metric definitions + record helpers
-----------------------------------
The recorders here are deliberately dumb: they translate a fact that *already happened* (a
command executed; a run reached a terminal outcome; a fault was classified) into a metric
update. They contain no ``if/elif`` that decides anything — the label values (command mode,
fault kind, run outcome) are produced by the security/orchestrator layers, and what to *do*
about the numbers is the agent's judgment (``knowledge/observability.md``). This keeps the
"thin code, thick agent" line: mechanism counts, the agent reasons.

A single process-wide :data:`REGISTRY` backs the ``/metrics`` endpoint. Tests can build an
isolated registry and call :func:`bind_registry` to redirect the module-level metrics, so
recording is assertable without touching global state permanently.
"""
from __future__ import annotations

import math
import threading
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field

# A label set is an ordered tuple of (name, value) pairs — ordered+hashable so it keys a dict.
LabelKey = tuple[tuple[str, str], ...]


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


def _render_labels(label_key: LabelKey, extra: Sequence[tuple[str, str]] = ()) -> str:
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
    buckets: dict[float, int] = field(default_factory=dict)
    hist_sum: float = 0.0
    hist_count: int = 0


class _Metric:
    """Base for the three metric types. Holds per-label-set series and its metadata."""

    metric_type = "untyped"

    def __init__(self, name: str, help_text: str, registry: MetricsRegistry):
        self.name = name
        self.help_text = help_text
        self._registry = registry
        self._series: dict[LabelKey, _Series] = {}

    def _series_for(self, label_key: LabelKey) -> _Series:
        s = self._series.get(label_key)
        if s is None:
            s = _Series()
            self._series[label_key] = s
        return s

    def snapshot(self) -> dict[LabelKey, _Series]:
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
DEFAULT_DURATION_BUCKETS: tuple[float, ...] = (
    0.05, 0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600, 1800, 3600,
)


class Histogram(_Metric):
    """Cumulative histogram: per-bucket counts (<= upper bound), plus sum and count. Renders the
    standard ``_bucket``/``_sum``/``_count`` series Prometheus/Grafana expect."""

    metric_type = "histogram"

    def __init__(self, name: str, help_text: str, registry: MetricsRegistry,
                 buckets: Sequence[float] = DEFAULT_DURATION_BUCKETS):
        super().__init__(name, help_text, registry)
        # Sorted, de-duplicated finite upper bounds; +Inf is implicit and added at render time.
        self._bounds: tuple[float, ...] = tuple(sorted({float(b) for b in buckets}))

    @property
    def bounds(self) -> tuple[float, ...]:
        return self._bounds

    def observe(self, value: float, *, labels: Mapping[str, str] | None = None) -> None:
        key = _normalize_labels(labels)
        with self._registry.lock:
            s = self._series_for(key)
            if not s.buckets:
                s.buckets = dict.fromkeys(self._bounds, 0)
            for b in self._bounds:
                if value <= b:
                    s.buckets[b] += 1
            s.hist_sum += float(value)
            s.hist_count += 1


class MetricsRegistry:
    """Owns a set of named metrics and renders them. A single registry per process is the norm
    (see the process-wide ``REGISTRY`` below); tests build throwaway registries."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self._metrics: dict[str, _Metric] = {}

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
                      series: dict[LabelKey, _Series]) -> None:
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


# --- metric definitions + record helpers -------------------------------------

# Process-wide registry. main.py renders this at /metrics.
REGISTRY = MetricsRegistry()

# --- metric handles (rebound by bind_registry) -------------------------------
# Commands executed by the agent (every execution, including auto-run read-only probes —
# mirrors the Phase-1 `command` event so the metric trail == the executed-command trail).
commands_total: Counter
command_duration_seconds: Histogram
# Orchestrator (Phase 3) Job lifecycle.
runs_submitted_total: Counter
run_attempts_total: Counter
runs_terminal_total: Counter          # labelled outcome=succeeded|dead_lettered
run_faults_total: Counter             # labelled kind=oom|timeout|evicted|...
runs_in_flight: Gauge                 # currently-watched runs (a live gauge during runs)


def _define(registry: MetricsRegistry) -> None:
    """(Re)create every metric handle against ``registry`` and publish them at module scope."""
    global commands_total, command_duration_seconds
    global runs_submitted_total, run_attempts_total, runs_terminal_total
    global run_faults_total, runs_in_flight

    commands_total = registry.counter(
        "llmdbench_agent_commands_total",
        "Total commands executed by the agent, by executable, allowlist mode, and whether "
        "they auto-ran (read-only) or were approval-gated (mutating).",
    )
    command_duration_seconds = registry.histogram(
        "llmdbench_agent_command_duration_seconds",
        "Wall-clock duration of executed commands, by executable and mode.",
    )
    runs_submitted_total = registry.counter(
        "llmdbench_orchestrator_runs_submitted_total",
        "Benchmark Jobs submitted to the cluster by the orchestrator.",
    )
    run_attempts_total = registry.counter(
        "llmdbench_orchestrator_run_attempts_total",
        "Benchmark Job attempts that reached a terminal phase, by phase "
        "(succeeded|failed|absent|active|pending).",
    )
    runs_terminal_total = registry.counter(
        "llmdbench_orchestrator_runs_terminal_total",
        "Logical benchmark runs that reached a terminal outcome, by outcome "
        "(succeeded|dead_lettered).",
    )
    run_faults_total = registry.counter(
        "llmdbench_orchestrator_run_faults_total",
        "Classified benchmark run faults, by kind (oom|timeout|unschedulable|evicted|"
        "image_error|run_error|unknown).",
    )
    runs_in_flight = registry.gauge(
        "llmdbench_orchestrator_runs_in_flight",
        "Benchmark runs currently being watched to completion by the orchestrator.",
    )


_define(REGISTRY)


def bind_registry(registry: MetricsRegistry) -> None:
    """Point the module-level metric handles at ``registry`` (used by tests for isolation)."""
    _define(registry)


@contextmanager
def use_registry(registry: MetricsRegistry) -> Iterator[MetricsRegistry]:
    """Temporarily bind a registry, restoring the process default on exit. Keeps tests from
    leaking metric state into each other or the global REGISTRY."""
    bind_registry(registry)
    try:
        yield registry
    finally:
        bind_registry(REGISTRY)


# --- record helpers (called from the central mechanism points) ---------------

def record_command(*, exe: str, mode: str, auto_run: bool, duration_s: float | None = None) -> None:
    """One executed command. ``exe``/``mode``/``auto_run`` come straight from the allowlist
    Decision (already classified there) — this just files the fact."""
    commands_total.inc(labels={"exe": exe, "mode": mode, "auto_run": str(auto_run).lower()})
    if duration_s is not None:
        command_duration_seconds.observe(duration_s, labels={"exe": exe, "mode": mode})


def record_run_submitted() -> None:
    runs_submitted_total.inc()


def record_attempt(phase: str) -> None:
    run_attempts_total.inc(labels={"phase": phase})


def record_run_outcome(*, succeeded: bool, dead_lettered: bool, fault_kind: str | None = None) -> None:
    """A logical run's terminal outcome (after any retries). ``fault_kind`` is the classifier's
    output (faults.py) — recorded as-is when the run did not succeed."""
    if succeeded:
        runs_terminal_total.inc(labels={"outcome": "succeeded"})
    elif dead_lettered:
        runs_terminal_total.inc(labels={"outcome": "dead_lettered"})
    if not succeeded and fault_kind and fault_kind != "none":
        run_faults_total.inc(labels={"kind": fault_kind})
