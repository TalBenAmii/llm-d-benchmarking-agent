"""Phase 48: parse + surface results.session_performance (multi-turn workloads).

Hermetic: pure report fixtures (and the repo's real BR v0.2 example), no cluster, no GPU,
no network. Asserts:
  * a multi-turn report (the session_performance.sessions block present) has its session
    scalars copied through and its per-session distributions extracted via the _stat ladder,
    with the catalogued label/unit_hint/direction attached as provenance;
  * a single-turn report (no session_performance) yields None — no fabrication, no crash;
  * the field-name discovery is DATA in knowledge/standard_metrics.yaml (catalog-driven);
  * summarize_report surfaces session_performance (None when absent), single-turn unchanged;
  * the analyze_results tool surfaces it per run end-to-end on a multi-turn report, and a
    multi-turn report still passes validation (session_performance is a NON-FATAL
    additionalProperties deviation, not a hard error).
"""
from __future__ import annotations

import copy

import pytest
import yaml

from app.tools.analyze import analyze
from app.validation.report import (
    extract_session_performance,
    load_report,
    summarize_report,
    validate_report,
)

# ---- fixtures: multi-turn (with session block) / single-turn (without) ------


def _sessions_block():
    """A results.session_performance.sessions block in the BR v0.2 SessionRequests shape:
    six integer counts + six per-session Statistics distributions (with min/max, which the
    _stat ladder intentionally drops)."""
    return {
        "total": 112,
        "succeeded": 110,
        "failed": 2,
        "total_events": 1340,
        "total_events_completed": 1320,
        "total_events_cancelled": 20,
        "session_rate": {"units": "queries/s", "mean": 2.24},
        "session_duration": {
            "units": "s", "mean": 48.3, "min": 12.1, "p50": 47.9,
            "p90": 61.2, "p99": 74.5, "max": 80.1,
        },
        "events_per_session": {"units": "count", "mean": 11.96, "min": 1.0, "p50": 12.0,
                               "p99": 20.0, "max": 20.0},
        "events_cancelled_per_session": {"units": "count", "mean": 0.18, "min": 0.0,
                                         "p50": 0.0, "p99": 2.0, "max": 3.0},
        "input_tokens_per_session": {"units": "count", "mean": 25612.4, "min": 2148.0,
                                     "p50": 25400.0, "p99": 43020.0, "max": 46900.0},
        "output_tokens_per_session": {"units": "count", "mean": 11800.2, "min": 980.0,
                                      "p50": 11700.0, "p99": 20100.0, "max": 22000.0},
    }


def _report_multi_turn():
    return {
        "version": "0.2",
        "run": {"uid": "u-multi"},
        "results": {"session_performance": {"sessions": _sessions_block()}},
    }


def _report_single_turn():
    """A valid-shaped report with request results but NO session_performance block."""
    return {
        "version": "0.2",
        "run": {"uid": "u-single"},
        "results": {
            "request_performance": {
                "aggregate": {
                    "requests": {"total": 500, "failures": 0},
                    "latency": {"time_to_first_token": {"units": "s", "mean": 0.13}},
                }
            }
        },
    }


# ---- extraction: multi-turn -------------------------------------------------


def test_extract_multi_turn_scalars_passed_through():
    sp = extract_session_performance(_report_multi_turn())
    assert sp is not None
    sc = sp["scalars"]
    assert sc == {
        "total": 112, "succeeded": 110, "failed": 2,
        "total_events": 1340, "total_events_completed": 1320, "total_events_cancelled": 20,
    }
    # scalars are copied through unchanged (pure pass-through, no math).
    assert all(isinstance(v, int) for v in sc.values())


def test_extract_multi_turn_distributions_via_stat_ladder():
    sp = extract_session_performance(_report_multi_turn())
    dists = sp["distributions"]
    assert set(dists) == {
        "session_rate", "session_duration", "events_per_session",
        "events_cancelled_per_session", "input_tokens_per_session", "output_tokens_per_session",
    }
    dur = dists["session_duration"]
    # provenance comes from the catalog (DATA), not hard-coded in Python.
    assert dur["label"] == "session duration"
    assert dur["unit_hint"] == "s"
    assert dur["direction"] == "lower"
    # value is the _stat ladder: units + mean + percentiles, with min/max dropped.
    val = dur["value"]
    assert val["units"] == "s"
    assert val["mean"] == 48.3
    assert val["p50"] == 47.9 and val["p90"] == 61.2 and val["p99"] == 74.5
    assert "min" not in val and "max" not in val
    # session_rate carries the queries/s throughput direction.
    rate = dists["session_rate"]
    assert rate["direction"] == "higher" and rate["unit_hint"] == "queries/s"
    assert rate["value"]["mean"] == 2.24


def test_catalog_drives_field_discovery_not_python():
    # Swap in a custom catalog that knows about only ONE scalar and ONE distribution:
    # the extractor must surface exactly those (proves it reads the catalog as DATA).
    import tempfile
    from pathlib import Path

    custom = {
        "session_performance": {
            "scalars": ["failed"],
            "distributions": {
                "session_duration": {"label": "dur", "unit_hint": "s", "direction": "lower"}
            },
        }
    }
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "cat.yaml"
        p.write_text(yaml.safe_dump(custom))
        sp = extract_session_performance(_report_multi_turn(), catalog_path=p)
    assert sp == {
        "scalars": {"failed": 2},
        "distributions": {
            "session_duration": {
                "label": "dur", "unit_hint": "s", "direction": "lower",
                "value": {"units": "s", "mean": 48.3, "p50": 47.9, "p90": 61.2, "p99": 74.5},
            }
        },
    }


# ---- single-turn / graceful degradation: None, never fabricated -------------


def test_extract_single_turn_is_none():
    assert extract_session_performance(_report_single_turn()) is None


@pytest.mark.parametrize("report", [
    {},
    {"results": {}},
    {"results": None},
    {"results": {"session_performance": None}},
    {"results": {"session_performance": {}}},
    {"results": {"session_performance": {"sessions": None}}},
    {"results": {"session_performance": {"sessions": "not-a-dict"}}},
    {"results": {"session_performance": {"sessions": {}}}},   # present but empty -> nothing -> None
    {"results": {"session_performance": {"sessions": {"unknown_field": 5}}}},  # no catalogued field
    "not-a-dict",
])
def test_extract_is_defensive_and_none(report):
    # Never raises; returns None (not {}) when nothing catalogued is present.
    assert extract_session_performance(report) is None


def test_partial_block_surfaces_only_present_fields():
    # A multi-turn run that reported only counts + session_rate (no other distributions):
    # surface exactly what's there, nothing fabricated.
    rep = {"results": {"session_performance": {"sessions": {
        "total": 7, "session_rate": {"units": "queries/s", "mean": 1.0},
    }}}}
    sp = extract_session_performance(rep)
    assert sp == {
        "scalars": {"total": 7},
        "distributions": {
            "session_rate": {
                "label": "session rate", "unit_hint": "queries/s", "direction": "higher",
                "value": {"units": "queries/s", "mean": 1.0},
            }
        },
    }


def test_bool_scalar_is_rejected_not_coerced_to_int():
    # bool is an int subclass; a stray bool must NOT be surfaced as a session count.
    rep = {"results": {"session_performance": {"sessions": {"total": True, "failed": 0}}}}
    sp = extract_session_performance(rep)
    assert sp == {"scalars": {"failed": 0}}


# ---- summary surfacing ------------------------------------------------------


def test_summary_surfaces_session_performance_when_present():
    s = summarize_report(_report_multi_turn())
    assert s["session_performance"] is not None
    assert s["session_performance"]["scalars"]["total"] == 112
    assert "session_duration" in s["session_performance"]["distributions"]


def test_summary_session_performance_none_when_absent():
    s = summarize_report(_report_single_turn())
    assert s["session_performance"] is None
    # the single-turn summary is otherwise unchanged (request results still surfaced).
    assert s["requests_total"] == 500
    assert s["latency"]  # request-level latency still parsed


def test_summary_real_example_surfaces_session_performance(br_example):
    if not br_example.exists():
        pytest.skip("BR v0.2 example not present")
    s = summarize_report(load_report(br_example))
    sp = s["session_performance"]
    assert sp is not None
    # the committed example carries the full session block.
    assert sp["scalars"]["total"] == 112
    assert sp["scalars"]["succeeded"] == 110
    assert sp["distributions"]["session_duration"]["value"]["mean"] == pytest.approx(48.3)
    assert sp["distributions"]["session_rate"]["value"]["units"] == "queries/s"


# ---- validation: multi-turn report still passes (non-fatal deviation) -------


def test_multi_turn_report_validates_as_nonfatal_deviation(br_example, br_schema):
    # The committed JSON Schema doesn't declare session_performance, so it surfaces as a
    # NON-FATAL additionalProperties deviation — the report is still valid.
    if not br_example.exists() or not br_schema.exists():
        pytest.skip("BR v0.2 example/schema not present")
    report = load_report(br_example)
    assert "session_performance" in report["results"]
    v = validate_report(report, br_schema)
    assert v.valid is True
    # the session block shows up as a deviation, not a fatal error.
    assert any("session_performance" in d for d in v.deviations)
    assert not any("session_performance" in e for e in v.errors)


# ---- end-to-end through the analyze_results tool ----------------------------


async def test_analyze_results_surfaces_session_performance(tool_ctx, br_example, tmp_path):
    if not br_example.exists():
        pytest.skip("BR v0.2 example not present")
    rep = load_report(br_example)
    d = tmp_path / "run-multi"
    d.mkdir(parents=True, exist_ok=True)
    (d / "benchmark_report_v0.2.yaml").write_text(yaml.safe_dump(rep, sort_keys=False))
    out = await analyze.analyze_results(tool_ctx, sources=[str(d)])
    assert out["analyzed"] is True and out["n"] == 1
    run = out["runs"][0]
    assert "session_performance" in run
    assert run["session_performance"]["scalars"]["total"] == 112
    assert "session_duration" in run["session_performance"]["distributions"]


async def test_analyze_results_omits_session_performance_for_single_turn(tool_ctx, tmp_path):
    # A single-turn report must NOT carry a session_performance key on the run item.
    single = copy.deepcopy(_report_single_turn())
    d = tmp_path / "run-single"
    d.mkdir(parents=True, exist_ok=True)
    (d / "benchmark_report_v0.2.yaml").write_text(yaml.safe_dump(single, sort_keys=False))
    out = await analyze.analyze_results(tool_ctx, sources=[str(d)])
    assert out["analyzed"] is True and out["n"] == 1
    assert "session_performance" not in out["runs"][0]
