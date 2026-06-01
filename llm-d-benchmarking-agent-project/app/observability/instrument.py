"""Agent + orchestrator metric definitions and the record helpers wired into the existing
central mechanism points.

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

from contextlib import contextmanager
from typing import Iterator

from app.observability.metrics import Counter, Gauge, Histogram, MetricsRegistry

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
