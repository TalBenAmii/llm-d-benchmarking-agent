"""Phase 10 — multi-harness orchestration in one session.

Covers:
  * summarize_report now surfaces the harness (scenario.load.standardized.tool) + load point,
  * compare_across_harnesses (pure cross-harness math): grouping by harness, shared vs unique
    metrics, the no-winner cross_metrics, the same-model guard, and the single-harness refusal,
  * the compare_harness_runs tool end-to-end on real BR v0.2 reports on disk (mixed harnesses,
    skipped invalid/missing, the single-harness hint), and its registration + dispatch.

Hermetic: hand-built summaries + real BR v0.2 reports written to a temp dir; no cluster,
no GPU, no live runs.
"""
from __future__ import annotations

import copy

import pytest
import yaml

from app.tools import multiharness
from app.tools.registry import dispatch, tool_definitions
from app.tools.schemas import CompareHarnessRunsInput
from app.validation.report import (
    ReportError,
    compare_across_harnesses,
    load_report,
    summarize_report,
)

# ---- helpers ---------------------------------------------------------------

def _summary(harness, *, model="m", ttft_ms=None, out_rate=None, req_rate=None,
             rate_qps=None, concurrency=None):
    s: dict = {"model": model, "harness": harness, "run_uid": f"u-{harness}",
               "duration": 10, "latency": {}, "throughput": {}}
    load = {}
    if rate_qps is not None:
        load["rate_qps"] = rate_qps
    if concurrency is not None:
        load["concurrency"] = concurrency
    s["load"] = load or None
    if ttft_ms is not None:
        s["latency"]["ttft"] = {"units": "ms", "mean": ttft_ms, "p99": ttft_ms}
    if out_rate is not None:
        s["throughput"]["output_token_rate"] = {"units": "tokens/s", "mean": out_rate}
    if req_rate is not None:
        s["throughput"]["request_rate"] = {"units": "req/s", "mean": req_rate}
    return s


def _write_report(dirpath, base: dict, *, harness: str, ttft_s: float, out_rate: float,
                  model: str | None = None):
    rep = copy.deepcopy(base)
    rep["scenario"]["load"]["standardized"]["tool"] = harness
    if model is not None:
        rep["scenario"]["stack"][0]["standardized"]["model"]["name"] = model
    agg = rep["results"]["request_performance"]["aggregate"]
    agg["latency"]["time_to_first_token"]["mean"] = ttft_s
    agg["latency"]["time_to_first_token"]["p99"] = ttft_s
    agg["throughput"]["output_token_rate"]["mean"] = out_rate
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / "benchmark_report_v0.2.yaml").write_text(yaml.safe_dump(rep, sort_keys=False))


# ---- summarize_report harness extraction -----------------------------------

def test_summarize_report_surfaces_harness_and_load(br_example):
    s = summarize_report(load_report(br_example))
    assert s["harness"] == "inference-perf"
    # the example declares rate_qps=10 — it must come through on the summary's load point.
    assert s["load"] is not None and s["load"].get("rate_qps") == 10


def test_summarize_report_harness_absent_is_none():
    # a report with no scenario.load must not crash and must report harness=None.
    s = summarize_report({"version": "0.2", "run": {"uid": "x"}, "scenario": {"stack": []}})
    assert s["harness"] is None


# ---- compare_across_harnesses (pure math) ----------------------------------

def test_cross_harness_groups_by_harness():
    entries = [
        {"label": "ip", "summary": _summary("inference-perf", ttft_ms=120, out_rate=200)},
        {"label": "gl", "summary": _summary("guidellm", ttft_ms=180, out_rate=400)},
    ]
    out = compare_across_harnesses(entries)
    assert set(out["harness_names"]) == {"inference-perf", "guidellm"}
    assert out["n"] == 2
    assert out["harnesses"]["inference-perf"]["runs"][0]["label"] == "ip"
    assert out["harnesses"]["guidellm"]["runs"][0]["label"] == "gl"


def test_cross_harness_shared_metrics_are_cross_validated_without_a_winner():
    # both harnesses measured ttft AND output_token_rate -> both are "shared".
    entries = [
        {"label": "ip", "summary": _summary("inference-perf", ttft_ms=120, out_rate=200)},
        {"label": "gl", "summary": _summary("guidellm", ttft_ms=180, out_rate=400)},
    ]
    out = compare_across_harnesses(entries)
    assert "latency.ttft" in out["shared_metrics"]
    assert "throughput.output_token_rate" in out["shared_metrics"]
    # cross_metrics lists per-harness values side by side and picks NO winner.
    ttft_row = next(r for r in out["cross_metrics"] if r["key"] == "latency.ttft")
    harnesses = {ph["harness"]: ph["value"] for ph in ttft_row["per_harness"]}
    assert harnesses == {"inference-perf": 120.0, "guidellm": 180.0}
    assert "best" not in ttft_row and "winner" not in ttft_row


def test_cross_harness_unique_metric_attributed_to_one_harness():
    # only guidellm measured request_rate -> it's unique, attributed to guidellm.
    entries = [
        {"label": "ip", "summary": _summary("inference-perf", ttft_ms=120)},
        {"label": "gl", "summary": _summary("guidellm", out_rate=400, req_rate=12.0)},
    ]
    out = compare_across_harnesses(entries)
    assert out["unique_metrics"]["latency.ttft"] == "inference-perf"
    assert out["unique_metrics"]["throughput.request_rate"] == "guidellm"
    # nothing was measured by both here -> no shared metrics, no cross rows.
    assert out["shared_metrics"] == [] and out["cross_metrics"] == []


def test_cross_harness_multiple_runs_per_harness_grouped():
    # a guidellm sweep contributes several runs; they all land under guidellm.
    entries = [
        {"label": "ip", "summary": _summary("inference-perf", ttft_ms=120, out_rate=200)},
        {"label": "gl-c1", "summary": _summary("guidellm", ttft_ms=150, out_rate=300)},
        {"label": "gl-c8", "summary": _summary("guidellm", ttft_ms=300, out_rate=600)},
    ]
    out = compare_across_harnesses(entries)
    gl_runs = {r["label"] for r in out["harnesses"]["guidellm"]["runs"]}
    assert gl_runs == {"gl-c1", "gl-c8"}
    assert out["harnesses"]["inference-perf"]["runs"][0]["label"] == "ip"


def test_cross_harness_flags_different_models():
    entries = [
        {"label": "ip", "summary": _summary("inference-perf", model="A", ttft_ms=120)},
        {"label": "gl", "summary": _summary("guidellm", model="B", out_rate=400)},
    ]
    out = compare_across_harnesses(entries)
    assert out["same_model"] is False
    assert set(out["models"]) == {"A", "B"}
    assert "WARNING" in out["headline"]


def test_cross_harness_requires_two_distinct_harnesses():
    # two runs, but BOTH inference-perf -> not a cross-harness comparison.
    entries = [
        {"label": "a", "summary": _summary("inference-perf", ttft_ms=120, out_rate=200)},
        {"label": "b", "summary": _summary("inference-perf", ttft_ms=180, out_rate=400)},
    ]
    with pytest.raises(ReportError):
        compare_across_harnesses(entries)


def test_cross_harness_unknown_harness_is_visible_not_dropped():
    # a report whose harness can't be read is grouped under "unknown", still counted.
    entries = [
        {"label": "ip", "summary": _summary("inference-perf", ttft_ms=120)},
        {"label": "gl", "summary": _summary("guidellm", out_rate=400)},
        {"label": "mystery", "summary": _summary(None, ttft_ms=99)},
    ]
    out = compare_across_harnesses(entries)
    assert "unknown" in out["harness_names"]
    assert out["harnesses"]["unknown"]["runs"][0]["label"] == "mystery"
    assert out["n"] == 3


# ---- compare_harness_runs tool (real reports on disk) ----------------------

async def test_compare_harness_runs_mixed_harnesses(tool_ctx, br_example, tmp_path):
    base = load_report(br_example)
    ip, gl = tmp_path / "ip", tmp_path / "gl"
    _write_report(ip, base, harness="inference-perf", ttft_s=0.12, out_rate=200.0)
    _write_report(gl, base, harness="guidellm", ttft_s=0.18, out_rate=400.0)

    out = await multiharness.compare_harness_runs(
        tool_ctx, sources=[str(ip), str(gl)], labels=["SLO", "sweep"]
    )
    assert out["compared"] is True and out["n"] == 2
    assert set(out["cross"]["harness_names"]) == {"inference-perf", "guidellm"}
    # provenance: each input's harness was read from the report, not the label.
    by_label = {r["label"]: r for r in out["reports"]}
    assert by_label["SLO"]["harness"] == "inference-perf"
    assert by_label["sweep"]["harness"] == "guidellm"
    # both reports use the same model -> contrast is meaningful.
    assert out["cross"]["same_model"] is True


async def test_compare_harness_runs_single_harness_points_at_compare_reports(tool_ctx, br_example, tmp_path):
    base = load_report(br_example)
    a, b = tmp_path / "a", tmp_path / "b"
    _write_report(a, base, harness="inference-perf", ttft_s=0.10, out_rate=100.0)
    _write_report(b, base, harness="inference-perf", ttft_s=0.40, out_rate=350.0)
    out = await multiharness.compare_harness_runs(tool_ctx, sources=[str(a), str(b)])
    assert out["compared"] is False
    assert "compare_reports" in out["hint"]


async def test_compare_harness_runs_skips_missing_and_invalid(tool_ctx, br_example, tmp_path):
    base = load_report(br_example)
    ip = tmp_path / "ip"
    _write_report(ip, base, harness="inference-perf", ttft_s=0.12, out_rate=200.0)
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "benchmark_report_v0.2.yaml").write_text(yaml.safe_dump({"version": "0.2", "run": {}}))
    # only one valid report (the invalid one is skipped, the missing dir too) -> refuse.
    out = await multiharness.compare_harness_runs(
        tool_ctx, sources=[str(ip), str(bad), str(tmp_path / "missing")]
    )
    assert out["compared"] is False
    reasons = {s["reason"] for s in out["skipped"]}
    assert "report failed schema validation" in reasons
    assert "no benchmark report found" in reasons


async def test_compare_harness_runs_dispatch_and_registered(tool_ctx, br_example, tmp_path):
    assert "compare_harness_runs" in {d["name"] for d in tool_definitions()}
    base = load_report(br_example)
    ip, gl = tmp_path / "ip", tmp_path / "gl"
    _write_report(ip, base, harness="inference-perf", ttft_s=0.12, out_rate=200.0)
    _write_report(gl, base, harness="guidellm", ttft_s=0.18, out_rate=400.0)
    out = await dispatch(tool_ctx, "compare_harness_runs",
                         {"sources": [str(ip), str(gl)]})
    assert out["compared"] is True


def test_compare_harness_runs_schema_requires_two_sources():
    with pytest.raises(ValueError):
        CompareHarnessRunsInput(sources=["/only-one"])
    m = CompareHarnessRunsInput(sources=["/a", "/b"], labels=["x", "y"])
    assert m.sources == ["/a", "/b"]
