"""Benchmark Report v0.2 validation + summary, against the repo's real schema/example."""
from __future__ import annotations

import pytest

from app.validation.report import load_report, summarize_report, validate_report


@pytest.fixture(scope="module")
def example(br_schema, br_example):
    if not br_schema.exists() or not br_example.exists():
        pytest.skip("llm-d-benchmark repo schema/example not present")
    return load_report(br_example)


def test_example_validates_against_schema(example, br_schema):
    v = validate_report(example, br_schema)
    assert v.valid, v.errors
    assert v.schema_version == "0.2"


def test_summary_extracts_key_metrics(example):
    s = summarize_report(example)
    assert s["model"] == "Qwen/Qwen3-0.6B"
    assert s["requests_total"] == 500
    assert s["requests_failures"] == 0
    assert s["success_rate_pct"] == 100.0
    # TTFT present with units (seconds in the example)
    assert s["latency"]["ttft"]["units"] == "s"
    assert "mean" in s["latency"]["ttft"]
    # throughput present
    assert "total_token_rate" in s["throughput"]


def test_invalid_report_is_rejected(br_schema):
    if not br_schema.exists():
        pytest.skip("schema not present")
    broken = {"version": "0.2", "run": {}}  # missing required 'results'
    v = validate_report(broken, br_schema)
    assert not v.valid
    assert v.errors


def test_summary_is_defensive_on_sparse_report():
    # Should not raise even with almost everything missing.
    s = summarize_report({"run": {}, "results": {}})
    assert s["requests_total"] is None
    assert s["latency"] == {} and s["throughput"] == {}


def test_summary_is_defensive_on_malformed_nondict_children():
    # Regression: compare_reports / compare_harness_runs call summarize_report BEFORE the validity
    # check, so a parseable-but-malformed report whose children are present-but-non-dict must NOT
    # crash with AttributeError — every nesting level coerces a non-dict to {}.
    bad = {
        "run": "2026-06-20",                              # scalar instead of a mapping
        "scenario": {"stack": "not-a-list", "load": "x"},
        "results": {"request_performance": "x"},
    }
    s = summarize_report(bad)                             # must not raise
    assert s["model"] is None and s["harness"] is None
    assert s["duration"] is None and s["requests_total"] is None
    # Truthy-non-dict at deeper levels (stack element, run.time, standardized) is tolerated too.
    bad2 = {"run": {"time": "2026"}, "scenario": {"stack": ["pod-a", {"standardized": "x"}]}}
    assert summarize_report(bad2)["duration"] is None
