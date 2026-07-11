"""Phase 7 — the dependency-free metrics registry + Prometheus text exposition.

These pin the EXACT exposition format Prometheus/Grafana parse (HELP/TYPE lines, cumulative
histogram buckets with the mandatory +Inf, _sum/_count, label sorting + escaping). A wrong
format silently breaks every scrape, so the assertions are concrete, not vacuous.
"""
from __future__ import annotations

import pytest

from app.observability.metrics import (
    DEFAULT_DURATION_BUCKETS,
    MetricsRegistry,
    render_prometheus,
)


def _lines(registry: MetricsRegistry) -> list[str]:
    return render_prometheus(registry).splitlines()


def test_counter_aggregates_by_label_set_and_sorts_labels():
    r = MetricsRegistry()
    c = r.counter("c_total", "a counter")
    # Same series regardless of label insertion order; values accumulate.
    c.inc(labels={"b": "2", "a": "1"})
    c.inc(3, labels={"a": "1", "b": "2"})
    # A distinct label set is a distinct series.
    c.inc(labels={"a": "9", "b": "2"})
    out = _lines(r)
    assert "# HELP c_total a counter" in out
    assert "# TYPE c_total counter" in out
    # Labels rendered in sorted order; the two equal label sets merged to 4.
    assert 'c_total{a="1",b="2"} 4' in out
    assert 'c_total{a="9",b="2"} 1' in out


def test_counter_rejects_negative():
    r = MetricsRegistry()
    c = r.counter("c_total", "h")
    with pytest.raises(ValueError):
        c.inc(-1)


def test_gauge_inc_dec_set():
    r = MetricsRegistry()
    g = r.gauge("g", "a gauge")
    g.inc()
    g.inc(2)
    g.dec()          # 1 + 2 - 1 = 2
    assert "g 2" in _lines(r)
    g.set(0)
    assert "g 0" in _lines(r)
    # A gauge may legitimately go negative (set/dec), unlike a counter.
    g.dec(5)
    assert "g -5" in _lines(r)


def test_histogram_buckets_are_cumulative_with_inf_sum_and_count():
    r = MetricsRegistry()
    h = r.histogram("d_seconds", "durations", buckets=[0.1, 1, 10])
    for v in (0.05, 0.5, 5, 50):
        h.observe(v)
    out = _lines(r)
    assert "# TYPE d_seconds histogram" in out
    # Cumulative counts: <=0.1 -> 1, <=1 -> 2, <=10 -> 3, +Inf -> 4 (all observations).
    assert 'd_seconds_bucket{le="0.1"} 1' in out
    assert 'd_seconds_bucket{le="1"} 2' in out
    assert 'd_seconds_bucket{le="10"} 3' in out
    assert 'd_seconds_bucket{le="+Inf"} 4' in out
    assert "d_seconds_sum 55.55" in out
    assert "d_seconds_count 4" in out
    # Buckets must be non-decreasing (the cumulative invariant Prometheus relies on).
    counts = [int(ln.split()[-1]) for ln in out if ln.startswith("d_seconds_bucket")]
    assert counts == sorted(counts)


def test_histogram_with_labels_keeps_series_separate():
    r = MetricsRegistry()
    h = r.histogram("d_seconds", "durations", buckets=[1])
    h.observe(0.5, labels={"exe": "kubectl"})
    h.observe(2.0, labels={"exe": "kind"})
    out = render_prometheus(r)
    assert 'd_seconds_bucket{exe="kubectl",le="1"} 1' in out
    assert 'd_seconds_bucket{exe="kind",le="1"} 0' in out  # 2.0 > 1 → not in this bucket
    assert 'd_seconds_count{exe="kind"} 1' in out


def test_label_values_are_escaped():
    r = MetricsRegistry()
    c = r.counter("c_total", "h")
    c.inc(labels={"msg": 'a"b\\c\nd'})
    out = render_prometheus(r)
    assert r'c_total{msg="a\"b\\c\nd"} 1' in out


def test_render_is_sorted_and_newline_terminated():
    r = MetricsRegistry()
    r.counter("z_total", "z").inc()
    r.counter("a_total", "a").inc()
    text = render_prometheus(r)
    assert text.endswith("\n")
    # a_total's HELP appears before z_total's (metrics rendered in name order).
    assert text.index("# HELP a_total") < text.index("# HELP z_total")


def test_registry_returns_same_handle_and_rejects_type_clash():
    r = MetricsRegistry()
    c1 = r.counter("x_total", "h")
    c2 = r.counter("x_total", "h")
    assert c1 is c2  # idempotent registration
    with pytest.raises(ValueError):
        r.gauge("x_total", "h")  # same name, different type → error


def test_default_buckets_cover_subsecond_to_an_hour():
    # Sanity: the default duration buckets span probe latencies up to long benchmark runs.
    assert DEFAULT_DURATION_BUCKETS[0] <= 0.1
    assert DEFAULT_DURATION_BUCKETS[-1] >= 3600
    assert list(DEFAULT_DURATION_BUCKETS) == sorted(DEFAULT_DURATION_BUCKETS)


def test_concurrent_increments_do_not_lose_updates():
    """The lock must make concurrent inc() from many threads exact (sweeps/sessions record
    in parallel). Without it, a read-modify-write race would drop counts."""
    import threading

    r = MetricsRegistry()
    c = r.counter("c_total", "h")

    def worker():
        for _ in range(1000):
            c.inc()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert "c_total 8000" in _lines(r)
