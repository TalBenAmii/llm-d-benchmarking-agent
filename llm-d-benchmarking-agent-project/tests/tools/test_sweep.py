"""Tests for the sweeps & A/B comparison feature:
- compare_summaries (pure comparison math)
- find_reports (multi-report discovery)
- the compare_reports tool (sources + experiment_dir modes)
- execute build_argv for the `experiment` subcommand
- policy coverage for `experiment` and `run --experiments`
"""
from __future__ import annotations

import pytest

from app.security.policy import MUTATING, READ_ONLY
from app.tools.analyze import compare
from app.tools.registry import dispatch, tool_definitions
from app.tools.run.execute import _result_location, build_argv
from app.tools.schemas import ExecuteInput
from app.validation.report import ReportError, compare_summaries, find_reports, load_report
from tests._helpers import write_br_report

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
    # values are normalized to the canonical unit (ms): 0.10s -> 100ms, 0.40s -> 400ms.
    assert ttft["units"] == "ms"
    c16 = next(p for p in ttft["per_run"] if p["label"] == "c16")
    assert c16["value"] == pytest.approx(400.0)
    assert c16["delta_abs"] == pytest.approx(300.0)   # 400ms - 100ms (canonicalized)
    assert c16["delta_pct"] == pytest.approx(300.0)

    thr = by_key["throughput.output_token_rate"]
    assert thr["direction"] == "higher"
    assert thr["best"]["label"] == "c16"          # higher throughput wins


def test_compare_summaries_normalizes_mixed_latency_units_for_winner():
    # Two schema-valid reports of the SAME metric in DIFFERENT units (the BR Units enum
    # allows both `s` and `ms`): A reports TTFT as 0.5 s (= 500 ms), B as 200 ms. B is
    # genuinely faster (200ms < 500ms). Without unit normalization the comparison crowned
    # A as the winner (0.5 < 200 numerically) and reported a ~39900% delta — a wrong winner
    # AND a nonsense delta. After the fix both are normalized to ms before comparing.
    A = {"label": "A", "summary": {"model": "m", "run_uid": "a",
         "latency": {"ttft": {"units": "s", "mean": 0.5}}, "throughput": {}}}
    B = {"label": "B", "summary": {"model": "m", "run_uid": "b",
         "latency": {"ttft": {"units": "ms", "mean": 200.0}}, "throughput": {}}}
    out = compare_summaries([A, B], baseline_index=0)
    ttft = next(m for m in out["metrics"] if m["key"] == "latency.ttft")
    assert ttft["best"]["label"] == "B"          # 200ms beats 500ms (NOT A by raw 0.5 < 200)
    assert ttft["units"] == "ms"                  # canonicalized unit reported
    by_label = {p["label"]: p for p in ttft["per_run"]}
    assert by_label["A"]["value"] == pytest.approx(500.0)   # 0.5s -> 500ms
    assert by_label["B"]["value"] == pytest.approx(200.0)
    assert by_label["B"]["delta_pct"] == pytest.approx(-60.0)   # not +39900%


def test_compare_summaries_unit_conversion_has_no_float_noise():
    # A report in `s` is scaled by 1000 to canonical ms — binary float arithmetic, so
    # 2.7387s came out as 2738.7000000000003 and reached the user verbatim (the row, the
    # headline, and the agent's prose, which quotes the tool result as-is).
    A = {"label": "A", "summary": {"model": "m", "run_uid": "a",
         "latency": {"ttft": {"units": "s", "mean": 2.7387}}, "throughput": {}}}
    B = {"label": "B", "summary": {"model": "m", "run_uid": "b",
         "latency": {"ttft": {"units": "s", "mean": 3.0}}, "throughput": {}}}
    out = compare_summaries([A, B], baseline_index=0)
    ttft = next(m for m in out["metrics"] if m["key"] == "latency.ttft")
    by_label = {p["label"]: p for p in ttft["per_run"]}
    assert by_label["A"]["value"] == 2738.7          # exact, not 2738.7000000000003
    assert ttft["baseline_value"] == 2738.7
    # the headline interpolates the winning value RAW — no UI formatter sees it.
    assert ttft["best"]["label"] == "A" and "2738.7 ms" in out["headline"]


def test_compare_summaries_keeps_small_magnitude_rates_exact():
    # `queries/s` needs no scaling (mult == 1.0), so there is no float noise to remove — yet
    # canonicalizing to a fixed 3 decimals truncated the family anyway: 0.0017 q/s -> 0.002 (17%
    # off) and 0.0004 -> 0.0, with delta_abs derived from the wrecked values. De-noising to
    # significant figures instead is magnitude-independent.
    def _rate(label: str, mean: float):
        return {"label": label, "summary": {"model": "m", "run_uid": label, "latency": {},
                "throughput": {"request_rate": {"units": "queries/s", "mean": mean}}}}

    out = compare_summaries([_rate("A", 0.0017), _rate("B", 0.0004)], baseline_index=0)
    rate = next(m for m in out["metrics"] if m["key"] == "throughput.request_rate")
    by_label = {p["label"]: p for p in rate["per_run"]}
    assert by_label["A"]["value"] == 0.0017
    assert by_label["B"]["value"] == 0.0004          # not 0.0
    assert by_label["B"]["delta_abs"] == -0.0013     # not -0.002
    assert rate["best"]["label"] == "A"              # higher request rate wins


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
    write_br_report(dirpath, base, ttft_s=ttft, out_rate=out_rate)


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


async def test_compare_reports_baseline_survives_skipped_input(tool_ctx, br_example, tmp_path):
    """`baseline_index` indexes the inputs; a skipped report *before* it must not shift it."""
    base = load_report(br_example)
    b, c = tmp_path / "B", tmp_path / "C"
    _write_report(b, base, ttft=0.10, out_rate=100.0)
    _write_report(c, base, ttft=0.40, out_rate=350.0)
    # input 0 ("A") is missing -> skipped; the caller's baseline is input 1 ("B").
    out = await compare.compare_reports(
        tool_ctx,
        sources=[str(tmp_path / "missing"), str(b), str(c)],
        labels=["A", "B", "C"],
        baseline_index=1,
    )
    assert out["compared"] is True and out["n"] == 2
    assert out["baseline"] == "B"                       # not "C" (the old off-by-skip bug)
    assert [s["label"] for s in out["skipped"]] == ["A"]


async def test_compare_reports_baseline_falls_back_when_itself_skipped(tool_ctx, br_example, tmp_path):
    """If the requested baseline is the one that gets skipped, fall back to the first valid run."""
    base = load_report(br_example)
    b, c = tmp_path / "B", tmp_path / "C"
    _write_report(b, base, 0.1, 100.0)
    _write_report(c, base, 0.4, 350.0)
    out = await compare.compare_reports(
        tool_ctx,
        sources=[str(b), str(c), str(tmp_path / "missing")],
        labels=["B", "C", "A"],
        baseline_index=2,   # "A" is missing
    )
    assert out["compared"] is True
    assert out["baseline"] == "B"                       # first surviving valid run


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


def test_result_location_experiment_returns_workspace():
    """An experiment writes one report per treatment under the anchored workspace, so that
    dir (not a scraped per-treatment path) is what compare_reports(experiment_dir=...) needs."""
    flags = {"workspace": "/ws/sess/experiment", "experiments": "exp.yaml"}
    assert _result_location("experiment", flags, None, "/ws/sess/results") == "/ws/sess/experiment"
    # a stdout-scraped per-treatment path does NOT override the known experiment workspace
    assert (
        _result_location("experiment", flags, "/ws/sess/experiment/t1/results", "/ws/sess/results")
        == "/ws/sess/experiment"
    )


def test_result_location_run_unchanged():
    # run: a scraped path wins; otherwise the output dir; nothing without an output dir.
    assert _result_location("run", {"output": "/o"}, "/scraped/results", "/o") == "/scraped/results"
    assert _result_location("run", {"output": "/o"}, None, "/o") == "/o"
    assert _result_location("run", {}, None, "/o") is None


# ---- policy coverage ----------------------------------------------------

def test_experiment_allowed_and_mutating(policy, catalog):
    d = policy.validate(
        ["llmdbenchmark", "--spec", "cicd/kind", "experiment",
         "-e", "workspace/exp.yaml", "-l", "inference-perf", "-w", "sanity_random.yaml"],
        catalog=catalog,
    )
    assert d.allowed and d.mode == MUTATING and d.requires_approval


def test_experiment_dry_run_downgrades_to_read_only(policy, catalog):
    d = policy.validate(
        ["llmdbenchmark", "--spec", "cicd/kind", "experiment", "-e", "workspace/exp.yaml", "--dry-run"],
        catalog=catalog,
    )
    assert d.allowed and d.mode == READ_ONLY


def test_experiment_two_namespaces_allowed(policy, catalog):
    d = policy.validate(
        ["llmdbenchmark", "--spec", "cicd/kind", "experiment", "-e", "x.yaml", "-p", "deploy-ns,bench-ns"],
        catalog=catalog,
    )
    assert d.allowed


def test_experiment_unknown_flag_now_allowed(policy, catalog):
    # Relaxed policy: an unrecognized flag on an policy-allowed subcommand is accepted...
    assert policy.validate(
        ["llmdbenchmark", "--spec", "cicd/kind", "experiment", "--bogus"], catalog=catalog
    ).allowed
    # ...but a metachar-laden value is still rejected by the screen.
    assert not policy.validate(
        ["llmdbenchmark", "--spec", "cicd/kind", "experiment", "--bogus", "a;b"], catalog=catalog
    ).allowed


def test_run_experiments_flag_allowed(policy, catalog):
    d = policy.validate(
        ["llmdbenchmark", "--spec", "cicd/kind", "run",
         "-l", "inference-perf", "-w", "sanity_random.yaml", "-e", "workspace/sweep.yaml"],
        catalog=catalog,
    )
    assert d.allowed and d.mode == MUTATING
