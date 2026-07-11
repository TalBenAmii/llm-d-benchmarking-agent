"""Phase 25: §3.4 standard-metric completeness — KV-cache hit rate, schedule delay,
GPU utilization extracted + surfaced from Benchmark Report v0.2 / harness-native output.

Hermetic: pure report fixtures (and the repo's real BR v0.2 example), no cluster, no GPU,
no network. Asserts:
  * a report carrying the three §3.4 metrics has all three extracted + surfaced in the
    summary, from BOTH the standardized ResourceMetrics shape and the harness-native shape,
    in catalog preference order;
  * a report lacking them degrades gracefully (None/omitted, no crash);
  * the metrics appear as an INFORMATIONAL Pareto objective without touching the
    goodput/SLO/frontier behavior;
  * the analyze_results tool surfaces them end-to-end on the real example report.
"""
from __future__ import annotations

import copy

import pytest
import yaml

from app.tools.analyze import analyze
from app.validation.analysis import SLOTargets, pareto_analysis
from app.validation.report import (
    extract_standard_metrics,
    load_report,
    summarize_report,
)

# ---- fixtures: minimal reports WITH / WITHOUT the §3.4 metrics --------------


def _stat(mean, **extra):
    return {"units": "percent", "mean": mean, "p50": mean, "p99": mean, **extra}


def _report_with_standardized():
    """A report carrying the metrics in the BR v0.2 STANDARDIZED ResourceMetrics shape
    (results.observability.components[].aggregate.<field>)."""
    return {
        "version": "0.2",
        "run": {"uid": "u1"},
        "results": {
            "observability": {
                "components": [
                    {
                        "component_label": "vllm-svc-0",
                        "aggregate": {
                            "cache_hit_rate": _stat(64.0),
                            "gpu_utilization": _stat(77.0),
                            "waiting_requests": {"units": "count", "mean": 3.5, "p99": 18.0},
                        },
                    }
                ]
            }
        },
    }


def _report_with_native():
    """A report carrying the metrics in the HARNESS-NATIVE per-metric observability shape
    (results.observability.<vendor_key> with components[].statistics + aggregated)."""
    return {
        "version": "0.2",
        "run": {"uid": "u2"},
        "results": {
            "observability": {
                "vllm_prefix_cache_hit_rate": {
                    "components": [
                        {"component_id": "decode", "statistics": {"units": "percent", "mean": 65.3, "p99": 92.4}}
                    ],
                    "aggregated": {"units": "percent", "mean": 60.1, "p99": 88.0},
                },
                "vllm_num_requests_waiting": {
                    "components": [
                        {"component_id": "decode", "statistics": {"units": "count", "mean": 4.0, "p99": 20.0}}
                    ]
                },
            }
        },
    }


def _report_without():
    """A valid-shaped report with an observability block but none of the §3.4 metrics."""
    return {
        "version": "0.2",
        "run": {"uid": "u3"},
        "results": {"observability": {"drop_rate": {"units": "percent", "mean": 0.0}}},
    }


# ---- extraction: standardized shape ----------------------------------------


def test_extract_standardized_all_three():
    sm = extract_standard_metrics(_report_with_standardized())
    assert set(sm) == {"kv_cache_hit_rate", "schedule_delay", "gpu_utilization"}
    # standardized cache_hit_rate is preferred over any native fallback
    assert sm["kv_cache_hit_rate"]["source"] == "standardized"
    assert sm["kv_cache_hit_rate"]["field"] == "cache_hit_rate"
    assert sm["kv_cache_hit_rate"]["value"]["mean"] == 64.0
    assert sm["kv_cache_hit_rate"]["component_label"] == "vllm-svc-0"
    # GPU util
    assert sm["gpu_utilization"]["field"] == "gpu_utilization"
    assert sm["gpu_utilization"]["value"]["mean"] == 77.0
    # schedule delay is a labelled queue-depth proxy
    assert sm["schedule_delay"]["proxy"] is True
    assert sm["schedule_delay"]["field"] == "waiting_requests"
    assert sm["schedule_delay"]["value"]["mean"] == 3.5
    assert "proxy" in sm["schedule_delay"]["label"].lower()


# ---- extraction: harness-native shape --------------------------------------


def test_extract_native_shape():
    sm = extract_standard_metrics(_report_with_native())
    assert sm["kv_cache_hit_rate"]["source"] == "native"
    assert sm["kv_cache_hit_rate"]["field"] == "vllm_prefix_cache_hit_rate"
    # prefers the cluster-wide `aggregated` block over a per-component statistics block
    assert sm["kv_cache_hit_rate"]["value"]["mean"] == 60.1
    # schedule-delay proxy via native vllm_num_requests_waiting (only components[] present)
    assert sm["schedule_delay"]["source"] == "native"
    assert sm["schedule_delay"]["field"] == "vllm_num_requests_waiting"
    assert sm["schedule_delay"]["value"]["mean"] == 4.0
    # this fixture has no GPU util in any shape -> omitted, not fabricated
    assert "gpu_utilization" not in sm


def test_standardized_preferred_over_native_when_both_present():
    # A report carrying cache_hit_rate BOTH ways: the standardized field must win.
    rep = _report_with_standardized()
    rep["results"]["observability"]["vllm_prefix_cache_hit_rate"] = {
        "aggregated": {"units": "percent", "mean": 99.0}
    }
    sm = extract_standard_metrics(rep)
    assert sm["kv_cache_hit_rate"]["source"] == "standardized"
    assert sm["kv_cache_hit_rate"]["value"]["mean"] == 64.0


# ---- graceful degradation --------------------------------------------------


def test_extract_absent_is_empty_not_crash():
    assert extract_standard_metrics(_report_without()) == {}


@pytest.mark.parametrize("report", [
    {},
    {"results": {}},
    {"results": {"observability": {}}},
    {"results": {"observability": None}},
    {"results": None},
    {"run": {}, "results": {"observability": {"components": "not-a-list"}}},
    {"results": {"observability": {"components": [None, 7, {}]}}},
])
def test_extract_is_defensive_on_garbage(report):
    # Never raises; returns {} when nothing usable is present.
    assert extract_standard_metrics(report) == {}


# ---- summary surfacing -----------------------------------------------------


def test_summary_surfaces_standard_metrics_when_present():
    s = summarize_report(_report_with_standardized())
    assert s["standard_metrics"] is not None
    assert set(s["standard_metrics"]) == {"kv_cache_hit_rate", "schedule_delay", "gpu_utilization"}


def test_summary_standard_metrics_none_when_absent():
    s = summarize_report(_report_without())
    assert s["standard_metrics"] is None
    # existing summary behavior is unchanged (defensive, no crash)
    assert s["latency"] == {} and s["throughput"] == {}


def test_summary_on_real_example_surfaces_all_three(br_example):
    if not br_example.exists():
        pytest.skip("BR v0.2 example not present")
    s = summarize_report(load_report(br_example))
    sm = s["standard_metrics"]
    assert sm is not None
    assert set(sm) == {"kv_cache_hit_rate", "schedule_delay", "gpu_utilization"}
    # example carries prefix-cache hit-rate native + standardized gpu_util/waiting_requests
    assert sm["kv_cache_hit_rate"]["value"]["mean"] == pytest.approx(65.3)
    assert sm["gpu_utilization"]["source"] == "standardized"
    assert sm["schedule_delay"]["proxy"] is True


# ---- informational Pareto objective (does not change frontier/SLO) ---------


def _perf_summary(*, ttft_ms, out_rate, kv_hit=None, gpu=None, qdepth=None):
    s = {
        "model": "m", "run_uid": "u", "duration": 10,
        "requests_total": 500, "success_rate_pct": 100.0,
        "latency": {"ttft": {"units": "ms", "mean": ttft_ms, "p99": ttft_ms}},
        "throughput": {"output_token_rate": {"units": "tokens/s", "mean": out_rate}},
    }
    std = {}
    if kv_hit is not None:
        std["kv_cache_hit_rate"] = {"label": "KV-cache hit rate", "value": {"units": "percent", "mean": kv_hit}, "direction": "higher"}
    if gpu is not None:
        std["gpu_utilization"] = {"label": "GPU utilization", "value": {"units": "percent", "mean": gpu}, "direction": "higher"}
    if qdepth is not None:
        std["schedule_delay"] = {"label": "schedule delay (queue depth proxy)", "value": {"units": "count", "mean": qdepth}, "direction": "lower", "proxy": True}
    s["standard_metrics"] = std or None
    return s


def test_pareto_surfaces_informational_objectives():
    entries = [
        {"label": "a", "summary": _perf_summary(ttft_ms=100, out_rate=100, kv_hit=20.0, gpu=50.0, qdepth=2.0)},
        {"label": "b", "summary": _perf_summary(ttft_ms=200, out_rate=250, kv_hit=70.0, gpu=88.0, qdepth=9.0)},
    ]
    out = pareto_analysis(entries)
    info = {o["name"]: o for o in out["informational_objectives"]}
    assert set(info) == {"kv_cache_hit_rate", "gpu_utilization", "schedule_delay"}
    kv = info["kv_cache_hit_rate"]
    assert kv["informational"] is True
    assert kv["direction"] == "max" and kv["units"] == "percent"
    assert kv["leader"] == {"label": "b", "value": 70.0}
    # schedule delay: lower-better -> leader is the smaller queue depth
    assert info["schedule_delay"]["leader"]["label"] == "a"
    # informational metrics MUST NOT be among the hard Pareto objectives
    assert {o["name"] for o in out["objectives"]} == {"ttft", "output_token_rate"}


def test_informational_objectives_do_not_change_frontier_or_slo():
    # Identical sweep, one variant carrying KV-cache hit rate and one without. The frontier,
    # slo_feasible/frontier, and per-run slo_met must be byte-identical (informational only).
    def build(with_kv):
        return [
            {"label": "c1", "summary": _perf_summary(ttft_ms=100, out_rate=100, kv_hit=20.0 if with_kv else None)},
            {"label": "cMID", "summary": _perf_summary(ttft_ms=200, out_rate=250, kv_hit=70.0 if with_kv else None)},
            {"label": "c16", "summary": _perf_summary(ttft_ms=400, out_rate=350, kv_hit=80.0 if with_kv else None)},
        ]
    slo = SLOTargets(ttft_ms=250, percentile="p99")
    without = pareto_analysis(build(False), slo=slo)
    with_ = pareto_analysis(build(True), slo=slo)
    assert set(with_["frontier"]) == set(without["frontier"])
    assert set(with_["slo_feasible"]) == set(without["slo_feasible"])
    assert set(with_["slo_frontier"]) == set(without["slo_frontier"])
    assert [r["slo_met"] for r in with_["runs"]] == [r["slo_met"] for r in without["runs"]]
    assert [r["goodput_pct"] for r in with_["runs"]] == [r["goodput_pct"] for r in without["runs"]]
    # but the informational block is only populated when the metric is present
    assert with_["informational_objectives"]
    assert without["informational_objectives"] == []


def test_informational_objectives_present_even_with_no_comparable_hard_objective():
    # Two runs sharing NO hard objective (one latency-only, one throughput-only) but both
    # carrying KV-cache hit rate -> the informational block still surfaces it.
    a = _perf_summary(ttft_ms=100, out_rate=100, kv_hit=20.0)
    a["throughput"] = {}
    b = _perf_summary(ttft_ms=100, out_rate=200, kv_hit=70.0)
    b["latency"] = {}
    out = pareto_analysis([{"label": "a", "summary": a}, {"label": "b", "summary": b}])
    assert out["objectives"] == [] and out["frontier"] == []
    names = {o["name"] for o in out["informational_objectives"]}
    assert "kv_cache_hit_rate" in names


# ---- end-to-end through the analyze_results tool ---------------------------


async def test_analyze_results_surfaces_standard_metrics(tool_ctx, br_example, tmp_path):
    if not br_example.exists():
        pytest.skip("BR v0.2 example not present")
    base = load_report(br_example)
    a = copy.deepcopy(base)
    b = copy.deepcopy(base)
    # vary the native KV-cache hit rate across the two runs so the informational leader differs.
    # The example's vllm_prefix_cache_hit_rate carries only a components[].statistics block
    # (no cluster-wide `aggregated`), which is the native fallback path.
    a["results"]["observability"]["vllm_prefix_cache_hit_rate"]["components"][0]["statistics"]["mean"] = 30.0
    b["results"]["observability"]["vllm_prefix_cache_hit_rate"]["components"][0]["statistics"]["mean"] = 80.0
    exp = tmp_path / "experiment"
    for name, rep in (("c1", a), ("c2", b)):
        d = exp / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "benchmark_report_v0.2.yaml").write_text(yaml.safe_dump(rep, sort_keys=False))
    out = await analyze.analyze_results(
        tool_ctx, slo={"ttft_ms": 200, "percentile": "p99"}, experiment_dir=str(exp)
    )
    assert out["analyzed"] is True and out["n"] == 2
    info = {o["name"]: o for o in out["pareto"]["informational_objectives"]}
    assert "kv_cache_hit_rate" in info
    # the run with the 80% aggregated hit rate leads
    assert info["kv_cache_hit_rate"]["leader"]["value"] == pytest.approx(80.0)
    # goodput/SLO surface untouched
    assert all("slo" in r for r in out["runs"])
