"""Tests for the sweeps & A/B comparison feature:
- compare_summaries (pure comparison math)
- find_reports (multi-report discovery)
- the compare_reports tool (sources + experiment_dir modes)
- execute build_argv for the `experiment` subcommand
- allowlist coverage for `experiment` and `run --experiments`
"""
from __future__ import annotations

import copy

import pytest
import yaml

from app.security.allowlist import MUTATING, READ_ONLY
from app.tools import compare
from app.tools.execute import build_argv
from app.tools.registry import dispatch, tool_definitions
from app.tools.schemas import ExecuteInput
from app.validation.report import ReportError, compare_summaries, find_reports, load_report


# ---- compare_summaries (pure math) ----------------------------------------

def _summary(ttft_mean: float, out_rate_mean: float, *, success: float = 100.0, total: int = 500):
    return {
        "model": "m", "run_uid": "u", "duration": 10,
        "requests_total": total, "success_rate_pct": success,
        "latency": {"ttft": {"units": "s", "mean": ttft_mean}},
        "throughput": {"output_token_rate": {"units": "tokens/s", "mean": out_rate_mean}},
    }


def test_compare_summaries_deltas_and_winners():
    entries = [
        {"label": "c1", "summary": _summary(0.10, 100.0)},
        {"label": "c16", "summary": _summary(0.40, 350.0)},
    ]
    out = compare_summaries(entries, baseline_index=0)
    assert out["baseline"] == "c1"
    by_key = {m["key"]: m for m in out["metrics"]}

    ttft = by_key["latency.ttft"]
    assert ttft["direction"] == "lower"
    assert ttft["best"]["label"] == "c1"          # lower latency wins
    c16 = next(p for p in ttft["per_run"] if p["label"] == "c16")
    assert c16["delta_abs"] == pytest.approx(0.30)
    assert c16["delta_pct"] == pytest.approx(300.0)

    thr = by_key["throughput.output_token_rate"]
    assert thr["direction"] == "higher"
    assert thr["best"]["label"] == "c16"          # higher throughput wins


def test_compare_summaries_requires_two():
    with pytest.raises(ReportError):
        compare_summaries([{"label": "x", "summary": _summary(1.0, 1.0)}])


def test_compare_summaries_skips_unshared_metric():
    a = _summary(0.1, 100.0)
    b = _summary(0.2, 200.0)
    b["latency"] = {}  # ttft present in only one run -> not comparable
    out = compare_summaries(
        [{"label": "a", "summary": a}, {"label": "b", "summary": b}]
    )
    keys = {m["key"] for m in out["metrics"]}
    assert "latency.ttft" not in keys
    assert "throughput.output_token_rate" in keys


def test_compare_summaries_clamps_bad_baseline_index():
    out = compare_summaries(
        [{"label": "a", "summary": _summary(1, 1)}, {"label": "b", "summary": _summary(2, 2)}],
        baseline_index=99,
    )
    assert out["baseline"] == "a"


# ---- find_reports + compare_reports tool ----------------------------------

def _write_report(dirpath, base: dict, ttft: float, out_rate: float):
    rep = copy.deepcopy(base)
    agg = rep["results"]["request_performance"]["aggregate"]
    agg["latency"]["time_to_first_token"]["mean"] = ttft
    agg["throughput"]["output_token_rate"]["mean"] = out_rate
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / "benchmark_report_v0.2.yaml").write_text(yaml.safe_dump(rep, sort_keys=False))


def test_find_reports_globs_and_newest(tmp_path, br_example):
    base = load_report(br_example)
    _write_report(tmp_path / "a", base, 0.1, 100.0)
    _write_report(tmp_path / "b", base, 0.2, 200.0)
    found = find_reports([tmp_path])
    assert len(found) == 2
    assert len(find_reports([tmp_path], newest_only=True)) == 1


async def test_compare_reports_sources_ab(tool_ctx, br_example, tmp_path):
    base = load_report(br_example)
    a, b = tmp_path / "runA", tmp_path / "runB"
    _write_report(a, base, ttft=0.10, out_rate=100.0)
    _write_report(b, base, ttft=0.40, out_rate=350.0)

    out = await compare.compare_reports(
        tool_ctx, sources=[str(a), str(b)], labels=["A", "B"], baseline_index=0
    )
    assert out["compared"] is True and out["n"] == 2
    assert out["baseline"] == "A"
    by_key = {m["key"]: m for m in out["comparison"]["metrics"]}
    assert by_key["latency.ttft"]["best"]["label"] == "A"
    assert by_key["throughput.output_token_rate"]["best"]["label"] == "B"


async def test_compare_reports_experiment_dir(tool_ctx, br_example, tmp_path):
    base = load_report(br_example)
    exp = tmp_path / "experiment"
    _write_report(exp / "conc1", base, 0.10, 100.0)
    _write_report(exp / "conc8", base, 0.20, 250.0)
    _write_report(exp / "conc16", base, 0.40, 350.0)

    out = await compare.compare_reports(tool_ctx, experiment_dir=str(exp))
    assert out["compared"] is True and out["n"] == 3
    assert {"conc1", "conc8", "conc16"} <= set(out["comparison"]["labels"])


async def test_compare_reports_needs_two_valid(tool_ctx, tmp_path):
    out = await compare.compare_reports(tool_ctx, sources=[str(tmp_path / "missing")])
    assert out["compared"] is False


async def test_compare_reports_dispatch_requires_input(tool_ctx):
    out = await dispatch(tool_ctx, "compare_reports", {})
    assert out["compared"] is False


def test_compare_reports_registered():
    assert "compare_reports" in {d["name"] for d in tool_definitions()}


# ---- execute build_argv + schema for `experiment` -------------------------

def test_build_argv_experiment_layout():
    argv = build_argv(
        "experiment", spec="cicd/kind",
        flags={"workspace": "/ws", "experiments": "exp.yaml",
               "skip_teardown": True, "parallelism": 2, "stop_on_error": True},
    )
    assert "--workspace" in argv and "/ws" in argv
    assert argv.index("--workspace") < argv.index("experiment")   # global flag precedes subcommand
    assert "-e" in argv and "exp.yaml" in argv
    assert "--skip-teardown" in argv and "--stop-on-error" in argv
    assert "-j" in argv and "2" in argv


def test_build_argv_run_unaffected_by_experiment_flags():
    argv = build_argv("run", spec="cicd/kind", harness="inference-perf", workload="sanity_random.yaml")
    assert "-e" not in argv and "--skip-teardown" not in argv and "--workspace" not in argv


def test_execute_schema_accepts_experiment():
    m = ExecuteInput(subcommand="experiment", spec="cicd/kind", flags={"experiments": "exp.yaml"})
    assert m.subcommand == "experiment"


# ---- allowlist coverage ----------------------------------------------------

def test_experiment_allowed_and_mutating(allowlist, catalog):
    d = allowlist.validate(
        ["llmdbenchmark", "--spec", "cicd/kind", "experiment",
         "-e", "workspace/exp.yaml", "-l", "inference-perf", "-w", "sanity_random.yaml"],
        catalog=catalog,
    )
    assert d.allowed and d.mode == MUTATING and d.requires_approval


def test_experiment_dry_run_downgrades_to_read_only(allowlist, catalog):
    d = allowlist.validate(
        ["llmdbenchmark", "--spec", "cicd/kind", "experiment", "-e", "workspace/exp.yaml", "--dry-run"],
        catalog=catalog,
    )
    assert d.allowed and d.mode == READ_ONLY


def test_experiment_two_namespaces_allowed(allowlist, catalog):
    d = allowlist.validate(
        ["llmdbenchmark", "--spec", "cicd/kind", "experiment", "-e", "x.yaml", "-p", "deploy-ns,bench-ns"],
        catalog=catalog,
    )
    assert d.allowed


def test_experiment_unknown_flag_denied(allowlist, catalog):
    d = allowlist.validate(
        ["llmdbenchmark", "--spec", "cicd/kind", "experiment", "--bogus"], catalog=catalog
    )
    assert not d.allowed


def test_run_experiments_flag_allowed(allowlist, catalog):
    d = allowlist.validate(
        ["llmdbenchmark", "--spec", "cicd/kind", "run",
         "-l", "inference-perf", "-w", "sanity_random.yaml", "-e", "workspace/sweep.yaml"],
        catalog=catalog,
    )
    assert d.allowed and d.mode == MUTATING
