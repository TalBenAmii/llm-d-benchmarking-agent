"""Phase 4 Results Analyzer tests: SLO filtering, goodput estimation, and Pareto/DoE
analysis — plus the analyze_results tool wiring and the SessionPlan SLO capture.

Hermetic: operates on hand-built report summaries and on real BR v0.2 reports written to
a temp dir; no cluster, no GPU, no live runs.
"""
from __future__ import annotations

import pytest
import yaml

from app.tools.analyze import analyze
from app.tools.registry import dispatch, tool_definitions
from app.tools.schemas import AnalyzeResultsInput
from app.validation.analysis import (
    HistoryContext,
    SLOTargets,
    evaluate_slo,
    pareto_analysis,
    recommend_next_steps,
)
from app.validation.report import load_report
from app.validation.session_plan import SessionPlan
from tests._helpers import write_br_report

# ---- helpers ---------------------------------------------------------------

def _summary(*, ttft_ms=None, tpot_ms=None, out_rate=None, req_lat_ms=None,
             itl_ms=None, total_rate=None, req_rate=None,
             success=100.0, total=500, ttft_ladder=None):
    """Build a summarize_report-shaped dict. Latency stats are in ms (units 'ms')."""
    s: dict = {"model": "m", "run_uid": "u", "duration": 10,
               "requests_total": total, "success_rate_pct": success,
               "latency": {}, "throughput": {}}
    if ttft_ms is not None:
        obj = {"units": "ms", "mean": ttft_ms, "p99": ttft_ms}
        if ttft_ladder:
            obj.update(ttft_ladder)
        s["latency"]["ttft"] = obj
    if tpot_ms is not None:
        s["latency"]["tpot"] = {"units": "ms", "mean": tpot_ms, "p99": tpot_ms}
    if req_lat_ms is not None:
        s["latency"]["request_latency"] = {"units": "ms", "mean": req_lat_ms, "p99": req_lat_ms}
    if itl_ms is not None:
        s["latency"]["itl"] = {"units": "ms", "mean": itl_ms, "p99": itl_ms}
    if out_rate is not None:
        s["throughput"]["output_token_rate"] = {"units": "tokens/s", "mean": out_rate}
    if total_rate is not None:
        s["throughput"]["total_token_rate"] = {"units": "tokens/s", "mean": total_rate}
    if req_rate is not None:
        s["throughput"]["request_rate"] = {"units": "queries/s", "mean": req_rate}
    return s


# ---- SLOTargets model ------------------------------------------------------

def test_slo_requires_at_least_one_target():
    with pytest.raises(ValueError):
        SLOTargets()


def test_slo_rejects_negative_target():
    with pytest.raises(ValueError):
        SLOTargets(ttft_ms=-5)


def test_slo_percentile_defaults_to_p99():
    assert SLOTargets(ttft_ms=200).percentile == "p99"


# ---- evaluate_slo: verdicts -----------------------------------------------

def test_evaluate_slo_pass_and_fail_verdicts():
    s = _summary(ttft_ms=150, out_rate=400)
    slo = SLOTargets(ttft_ms=200, throughput_floor_tok_s=300)
    out = evaluate_slo(s, slo)
    assert out["overall_met"] is True
    by = {v["metric"]: v for v in out["verdicts"]}
    assert by["ttft"]["met"] is True and by["ttft"]["observed"] == 150 and by["ttft"]["units"] == "ms"
    assert by["throughput_floor"]["met"] is True

    # raise the bar so it fails
    out2 = evaluate_slo(s, SLOTargets(ttft_ms=100))
    assert out2["overall_met"] is False
    assert {v["metric"]: v["met"] for v in out2["verdicts"]}["ttft"] is False


def test_evaluate_slo_unit_conversion_seconds_to_ms():
    # report TTFT is in seconds; target is in ms — must convert before comparing.
    s = _summary(ttft_ms=None)
    s["latency"]["ttft"] = {"units": "s", "mean": 0.18, "p99": 0.18}
    out = evaluate_slo(s, SLOTargets(ttft_ms=200))
    v = next(v for v in out["verdicts"] if v["metric"] == "ttft")
    assert v["observed"] == pytest.approx(180.0)   # 0.18s -> 180ms
    assert v["met"] is True

    # ...and the scaling must not leak binary-float noise (2.7387s -> 2738.7, never
    # 2738.7000000000003): the agent quotes `observed` verbatim in its prose.
    s["latency"]["ttft"] = {"units": "s", "mean": 2.7387, "p99": 2.7387}
    noisy = evaluate_slo(s, SLOTargets(ttft_ms=3000))
    assert next(v for v in noisy["verdicts"] if v["metric"] == "ttft")["observed"] == 2738.7


def test_evaluate_slo_missing_metric_is_none_not_pass():
    # SLO on a metric the report doesn't carry -> met is None, and not counted as a pass.
    s = _summary(out_rate=400)  # no ttft
    out = evaluate_slo(s, SLOTargets(ttft_ms=200))
    v = next(v for v in out["verdicts"] if v["metric"] == "ttft")
    assert v["met"] is None
    assert out["overall_met"] is False           # nothing actually passed
    assert out["checked_count"] == 0


def test_evaluate_slo_success_rate_floor_gates_overall():
    s = _summary(ttft_ms=100, success=92.0)
    out = evaluate_slo(s, SLOTargets(ttft_ms=200, min_success_rate_pct=99.0))
    assert out["success_rate_met"] is False
    assert out["overall_met"] is False           # ttft passes but success-rate floor fails


# ---- goodput estimation ----------------------------------------------------

def test_goodput_interpolates_between_percentiles():
    # ladder: p50=100ms, p90=200ms, p99=300ms. target=200ms -> exactly the p90 fraction.
    s = _summary(ttft_ms=300, ttft_ladder={"p50": 100.0, "p90": 200.0, "p99": 300.0})
    out = evaluate_slo(s, SLOTargets(ttft_ms=200, percentile="p99"))
    assert out["goodput"]["is_estimate"] is True
    assert out["goodput"]["estimate_fraction"] == pytest.approx(0.90)
    assert out["goodput"]["estimate_pct"] == pytest.approx(90.0)
    assert "ttft" in out["goodput"]["from_slos"]


def test_goodput_is_min_across_multiple_latency_slos():
    # ttft easily met by ~99%, but request_latency target lands at ~p50 -> ~50%.
    s = _summary(
        ttft_ms=300, ttft_ladder={"p50": 100.0, "p90": 200.0, "p99": 300.0},
        req_lat_ms=1000,
    )
    s["latency"]["request_latency"] = {"units": "ms", "p50": 500.0, "p90": 900.0, "p99": 1000.0}
    out = evaluate_slo(s, SLOTargets(ttft_ms=1000, request_latency_ms=500, percentile="p99"))
    # combined goodput is the MIN of per-SLO estimates (upper bound) -> ~0.50 from req-latency.
    assert out["goodput"]["estimate_fraction"] == pytest.approx(0.50)
    assert set(out["goodput"]["from_slos"]) == {"ttft", "request_latency"}


def test_goodput_none_without_percentiles():
    s = _summary(ttft_ms=150)  # only mean+p99, both equal; still has a percentile (p99)
    # remove percentiles entirely -> only mean present, no ladder
    s["latency"]["ttft"] = {"units": "ms", "mean": 150.0}
    out = evaluate_slo(s, SLOTargets(ttft_ms=200))
    assert out["goodput"]["estimate_fraction"] is None


def test_goodput_uses_low_percentiles_for_sub_p50_target():
    # A target between the LOW percentiles (p25 and p50) must interpolate within that band,
    # not floor to 0% because p0p1..p25 were dropped from the summary. This is the
    # correctness defect: dropping the low ladder makes any sub-p50 target read as 0%.
    s = _summary(
        ttft_ms=50,
        ttft_ladder={
            "p0p1": 27.3, "p1": 27.9, "p5": 30.2, "p10": 31.2, "p25": 33.4,
            "p50": 36.2, "p75": 39.9, "p90": 42.6, "p95": 45.2, "p99": 50.3, "p99p9": 57.0,
        },
    )
    # target 34.8ms sits midway between p25=33.4 and p50=36.2 -> ~37.5% of requests meet it.
    out = evaluate_slo(s, SLOTargets(ttft_ms=34.8, percentile="p99"))
    gp = out["goodput"]["estimate_fraction"]
    assert gp is not None and gp > 0.0
    # linear interp: 0.25 + (34.8-33.4)/(36.2-33.4) * (0.50-0.25) = 0.375
    assert gp == pytest.approx(0.375, abs=0.01)
    assert out["goodput"]["estimate_pct"] == pytest.approx(37.5, abs=1.0)


def test_slo_percentile_p99p9_is_evaluable():
    # p99.9 is advertised in the SLOTargets.percentile Literal; it must actually be a
    # usable statistic, not silently yield observed=None / met=None for every metric.
    s = _summary(
        ttft_ms=50,
        ttft_ladder={"p50": 36.2, "p90": 42.6, "p99": 50.3, "p99p9": 57.0},
    )
    out = evaluate_slo(s, SLOTargets(ttft_ms=60, percentile="p99p9"))
    v = next(v for v in out["verdicts"] if v["metric"] == "ttft")
    assert v["statistic"] == "p99p9"
    assert v["observed"] == pytest.approx(57.0)   # read off p99p9, not None
    assert v["met"] is True
    assert out["overall_met"] is True


def test_unit_tables_are_the_shared_report_tables():
    # analysis.py and report.py each used to carry their own conversion table and had already
    # drifted in coverage. They must now BE the same objects, so drift is impossible.
    from app.validation import analysis as _an
    from app.validation.report import _CANONICAL_CONVERSIONS

    assert _an._TO_MS is _CANONICAL_CONVERSIONS["ms"]
    assert _an._TO_TOK_S is _CANONICAL_CONVERSIONS["tokens/s"]
    # every unit the BR v0.2 Units enum can carry for these families resolves identically
    assert _an._TO_MS["s"] == 1000.0 and _an._TO_MS["ms"] == 1.0
    assert _an._TO_MS["s/token"] == 1000.0 and _an._TO_MS["ms/token"] == 1.0
    assert _an._TO_TOK_S["tokens/s"] == 1.0


def test_unit_tables_are_read_only():
    # The two modules bind the SAME objects (above), so a module-local mutation in either would
    # silently rewrite the other's table with nothing to catch it. Freezing makes it raise.
    from app.validation import analysis as _an
    from app.validation.report import _CANONICAL_CONVERSIONS

    with pytest.raises(TypeError):
        _an._TO_MS["fortnights"] = 1.0            # type: ignore[index]
    with pytest.raises(TypeError):
        _CANONICAL_CONVERSIONS["ms"] = {}          # type: ignore[index]
    assert "fortnights" not in _CANONICAL_CONVERSIONS["ms"]


# ---- pareto / DoE analysis -------------------------------------------------

def test_pareto_frontier_excludes_dominated_run():
    # c1: low latency, low throughput; c16: high latency, high throughput -> both on frontier.
    # cBAD: worse latency than c1 AND worse throughput than c16 -> dominated by neither alone,
    # but dominated if some run beats it on both. Make cMID strictly dominated by c1.
    entries = [
        {"label": "c1", "summary": _summary(ttft_ms=100, out_rate=100)},
        {"label": "c16", "summary": _summary(ttft_ms=400, out_rate=350)},
        {"label": "cBAD", "summary": _summary(ttft_ms=200, out_rate=80)},   # worse than c1 on both
    ]
    out = pareto_analysis(entries)
    assert set(out["frontier"]) == {"c1", "c16"}
    assert "cBAD" not in out["frontier"]
    by = {r["label"]: r for r in out["runs"]}
    assert by["cBAD"]["on_frontier"] is False
    assert by["c1"]["on_frontier"] is True


def _sweep_row(label, ttft, tpot, itl, rl, otr, ttr, rr):
    return {"label": label, "summary": _summary(
        ttft_ms=ttft, tpot_ms=tpot, itl_ms=itl, req_lat_ms=rl,
        out_rate=otr, total_rate=ttr, req_rate=rr)}


def test_pareto_dominance_uses_one_objective_per_family():
    # The 7 objectives are not 7 independent axes. Dominance is judged on one representative
    # per family so the near-collinear measures can't keep a loser alive.
    out = pareto_analysis([
        _sweep_row("c1", 50, 10, 10, 500, 100, 200, 2.0),
        _sweep_row("c8", 300, 25, 25, 2200, 320, 640, 5.9),
    ])
    assert out["deciding_objectives"] == ["ttft", "output_token_rate"]
    by_name = {o["name"]: o for o in out["objectives"]}
    assert by_name["ttft"]["deciding"] is True and by_name["ttft"]["family"] == "latency"
    assert by_name["output_token_rate"]["deciding"] is True
    # every other objective is still reported and compared, just not deciding
    assert by_name["tpot"]["deciding"] is False and by_name["request_rate"]["deciding"] is False
    assert set(by_name) == {"ttft", "tpot", "itl", "request_latency",
                            "output_token_rate", "total_token_rate", "request_rate"}
    # deciding objectives lead the list: app.js plots objectives[0] vs objectives[1], so this
    # is what makes the scatter's axes the same pair the frontier was computed on.
    assert [o["name"] for o in out["objectives"][:2]] == ["ttft", "output_token_rate"]


def test_pareto_drops_run_beaten_on_both_deciding_axes():
    # THE regression this guards: c16 is worse than c8 on BOTH deciding axes (higher ttft,
    # lower output_token_rate) but wins the correlated non-deciding request_rate (shorter
    # outputs). Scoring all 7 objectives left it non-dominated — the "everything is starred"
    # bug. It must now be dominated.
    entries = [
        _sweep_row("c1", 50, 10, 10, 500, 100, 200, 2.0),
        _sweep_row("c4", 140, 15, 15, 1100, 300, 600, 5.6),
        _sweep_row("c8", 300, 25, 25, 2200, 320, 640, 5.9),
        _sweep_row("c16", 700, 45, 45, 4400, 315, 630, 9.9),
    ]
    out = pareto_analysis(entries)
    assert "c16" not in out["frontier"]
    assert set(out["frontier"]) == {"c1", "c4", "c8"}
    by = {r["label"]: r for r in out["runs"]}
    assert by["c16"]["on_frontier"] is False
    # it still wins request_rate, and that fact is still reported — just not deciding.
    assert by["c16"]["objectives"]["request_rate"] == 9.9
    assert out["frontier_degenerate"] is False


def test_pareto_run_missing_a_deciding_objective_cannot_dominate():
    # THE regression narrowing dominance to 2 deciding objectives introduced: dominance used to
    # SKIP an objective either point was missing. Harmless across 7 near-collinear measures (the
    # others vetoed a bogus win); fatal across 2 — C, which reports throughput but no ttft, then
    # dominated on throughput ALONE and deleted A (the best-latency config, a true Pareto point).
    entries = [
        {"label": "A", "summary": _summary(ttft_ms=50, tpot_ms=10, out_rate=100)},
        {"label": "B", "summary": _summary(ttft_ms=60, tpot_ms=11, out_rate=110)},
        {"label": "C", "summary": _summary(tpot_ms=9999, out_rate=105)},   # no ttft
    ]
    out = pareto_analysis(entries)
    assert out["deciding_objectives"] == ["ttft", "output_token_rate"]
    # A survives: C carries no ttft, so it is INCOMPARABLE, not a winner.
    assert out["frontier"] == ["A", "B"]
    by = {r["label"]: r for r in out["runs"]}
    assert by["A"]["on_frontier"] is True
    # C is neither starred nor dominated — and its measured values are still reported.
    assert by["C"]["on_frontier"] is False
    assert by["C"]["objectives"]["output_token_rate"] == 105


def test_pareto_run_with_no_deciding_objective_is_not_placeable():
    # Placeability used to test "has ANY comparable objective", but points are filled from all 7.
    # So C — comparable only on the NON-deciding tpot — counted as placeable, could never be
    # dominated (it shares no deciding key with anyone), was always starred, and flipped
    # frontier_degenerate, making the agent claim "none is strictly worse" about the worst run.
    entries = [
        {"label": "A", "summary": _summary(ttft_ms=50, tpot_ms=10, out_rate=100)},
        {"label": "B", "summary": _summary(ttft_ms=60, tpot_ms=11, out_rate=110)},
        {"label": "C", "summary": _summary(tpot_ms=9999)},   # nothing deciding
    ]
    out = pareto_analysis(entries)
    assert out["frontier"] == ["A", "B"]
    assert {r["label"] for r in out["runs"] if r["on_frontier"]} == {"A", "B"}
    # degeneracy counts only the PLACEABLE runs: A and B genuinely trade off; C doesn't count.
    assert out["frontier_degenerate"] is True


def test_pareto_monotone_sweep_is_flagged_degenerate():
    # A clean concurrency sweep really has no dominated run: every step buys throughput and
    # costs latency. Correct, but useless as a "which wins" signal — so it must be FLAGGED,
    # not silently presented as a narrowed-down answer.
    out = pareto_analysis([
        _sweep_row("c1", 50, 10, 10, 500, 100, 200, 2.0),
        _sweep_row("c2", 80, 12, 12, 700, 180, 360, 3.4),
        _sweep_row("c4", 140, 15, 15, 1100, 300, 600, 5.6),
    ])
    assert set(out["frontier"]) == {"c1", "c2", "c4"}
    assert out["frontier_degenerate"] is True


def test_pareto_slo_frontier_degeneracy_is_reported_separately():
    entries = [
        _sweep_row("c1", 50, 10, 10, 500, 100, 200, 2.0),
        _sweep_row("c2", 80, 12, 12, 700, 180, 360, 3.4),
        _sweep_row("c16", 700, 45, 45, 4400, 315, 630, 9.9),
    ]
    out = pareto_analysis(entries, slo=SLOTargets(ttft_ms=100, percentile="p99"))
    assert out["slo_feasible"] == ["c1", "c2"]
    # the two feasible runs trade off against each other -> degenerate among the feasible set
    assert set(out["slo_frontier"]) == {"c1", "c2"}
    assert out["slo_frontier_degenerate"] is True


def test_pareto_requires_two_runs():
    with pytest.raises(ValueError):
        pareto_analysis([{"label": "x", "summary": _summary(ttft_ms=1, out_rate=1)}])


def test_pareto_no_shared_objective():
    a = _summary(ttft_ms=100)            # latency only
    b = _summary(out_rate=200)           # throughput only
    out = pareto_analysis([{"label": "a", "summary": a}, {"label": "b", "summary": b}])
    # no single objective present in BOTH runs -> nothing comparable
    assert out["objectives"] == [] and out["frontier"] == []
    # No SLOs given -> the SLO keys stay ABSENT. This absence is what tells the results card
    # "no SLOs were given" (frontier_basis "overall"); it must not be faked with empty lists.
    assert "slo_feasible" not in out and "slo_frontier" not in out


def test_pareto_no_shared_objective_still_reports_slo_state():
    """THE distinction: ``slo_frontier`` ABSENT means "no SLOs given", EMPTY means "SLOs given,
    nothing on the frontier". The nothing-comparable early return used to drop both keys even
    when SLOs *were* passed, so the card mislabelled the basis as "no SLOs given"."""
    a = _summary(ttft_ms=100)            # latency only
    b = _summary(out_rate=200)           # throughput only
    out = pareto_analysis(
        [{"label": "a", "summary": a}, {"label": "b", "summary": b}],
        slo=SLOTargets(ttft_ms=250, percentile="p99"),
    )
    assert out["objectives"] == [] and out["frontier"] == []
    # SLOs WERE given -> the keys are present. "a" meets the 250ms target on the evidence it has.
    assert out["slo_feasible"] == ["a"]
    # Present-but-empty: feasibility is known, but nothing is placeable without a comparable
    # objective, so no run can sit on a frontier.
    assert out["slo_frontier"] == []
    assert out["slo_frontier_degenerate"] is False
    # A feasible-but-unrankable run passed the SLOs -> must NOT carry the "nothing passed" note
    # (that note is what would drive the card's misleading "no run met the SLO targets" wording).
    assert out.get("note") != "no run satisfies all SLO targets"


def test_pareto_slo_feasible_frontier():
    # SLO: ttft <= 250ms. c1 (100ms) and cMID(200ms) feasible; c16(400ms) not.
    # Among feasible, cMID has higher throughput than c1 -> both could be on the feasible
    # frontier (c1 better latency, cMID better throughput).
    entries = [
        {"label": "c1", "summary": _summary(ttft_ms=100, out_rate=100)},
        {"label": "cMID", "summary": _summary(ttft_ms=200, out_rate=250)},
        {"label": "c16", "summary": _summary(ttft_ms=400, out_rate=350)},
    ]
    slo = SLOTargets(ttft_ms=250, percentile="p99")
    out = pareto_analysis(entries, slo=slo)
    assert set(out["slo_feasible"]) == {"c1", "cMID"}
    assert "c16" not in out["slo_feasible"]
    assert set(out["slo_frontier"]) == {"c1", "cMID"}
    # per-run SLO tags present
    by = {r["label"]: r for r in out["runs"]}
    assert by["c16"]["slo_met"] is False
    assert by["c1"]["slo_met"] is True


def test_pareto_no_feasible_run_is_reported():
    entries = [
        {"label": "a", "summary": _summary(ttft_ms=300, out_rate=100)},
        {"label": "b", "summary": _summary(ttft_ms=400, out_rate=350)},
    ]
    out = pareto_analysis(entries, slo=SLOTargets(ttft_ms=100))
    assert out["slo_feasible"] == []
    assert out["slo_frontier"] == []
    assert "no run satisfies" in out.get("note", "")


# ---- analyze_results tool (real reports on disk) ---------------------------

def _write_report(dirpath, base: dict, ttft_s: float, out_rate: float):
    write_br_report(dirpath, base, ttft_s=ttft_s, out_rate=out_rate, p99=ttft_s)


async def test_analyze_results_single_run_goodput(tool_ctx, br_example, tmp_path):
    base = load_report(br_example)
    run = tmp_path / "run"
    _write_report(run, base, ttft_s=0.15, out_rate=400.0)   # 150ms ttft, 400 tok/s
    out = await analyze.analyze_results(
        tool_ctx, slo={"ttft_ms": 200, "throughput_floor_tok_s": 300}, sources=[str(run)]
    )
    assert out["analyzed"] is True and out["n"] == 1
    slo = out["runs"][0]["slo"]
    assert slo["overall_met"] is True
    assert slo["goodput"]["is_estimate"] is True
    assert "pareto" not in out                       # single run -> no frontier


async def test_analyze_results_sweep_pareto(tool_ctx, br_example, tmp_path):
    base = load_report(br_example)
    exp = tmp_path / "experiment"
    _write_report(exp / "c1", base, ttft_s=0.10, out_rate=100.0)
    _write_report(exp / "c8", base, ttft_s=0.20, out_rate=250.0)
    _write_report(exp / "c16", base, ttft_s=0.40, out_rate=350.0)
    out = await analyze.analyze_results(
        tool_ctx, slo={"ttft_ms": 250, "percentile": "p99"}, experiment_dir=str(exp)
    )
    assert out["analyzed"] is True and out["n"] == 3
    par = out["pareto"]
    # all three are mutually non-dominated (latency up, throughput up) -> all on frontier
    assert set(par["frontier"]) == {"c1", "c8", "c16"}
    # ttft p99: c1=100ms, c8=200ms feasible (<=250); c16=400ms not
    assert set(par["slo_feasible"]) == {"c1", "c8"}


async def test_analyze_results_sub_p50_target_is_not_floored_to_zero(tool_ctx, br_example, tmp_path):
    # End-to-end on the REAL BR v0.2 example: its TTFT ladder has p25~33.4ms and p50~36.2ms.
    # A 34.8ms target sits between them, so ~25-50% of requests meet it. The headline goodput
    # estimate must reflect that, NOT 0% — which is what happened when the summary dropped the
    # sub-p50 percentiles. (Example reports TTFT in seconds, so this also exercises conversion.)
    base = load_report(br_example)
    run = tmp_path / "run"
    run.mkdir(parents=True, exist_ok=True)
    (run / "benchmark_report_v0.2.yaml").write_text(yaml.safe_dump(base, sort_keys=False))
    out = await analyze.analyze_results(
        tool_ctx, slo={"ttft_ms": 34.8, "percentile": "p99"}, sources=[str(run)]
    )
    assert out["analyzed"] is True
    gp = out["runs"][0]["slo"]["goodput"]["estimate_pct"]
    assert gp is not None
    assert 20.0 < gp < 55.0, f"sub-p50 target floored/misestimated: got {gp}%"


async def test_analyze_results_p99p9_percentile_evaluable(tool_ctx, br_example, tmp_path):
    # Setting the common p99.9 tail SLO must actually evaluate the latency metrics through
    # the full disk path, not silently leave them met=None.
    base = load_report(br_example)
    run = tmp_path / "run"
    run.mkdir(parents=True, exist_ok=True)
    (run / "benchmark_report_v0.2.yaml").write_text(yaml.safe_dump(base, sort_keys=False))
    out = await analyze.analyze_results(
        tool_ctx, slo={"ttft_ms": 100, "percentile": "p99p9"}, sources=[str(run)]
    )
    assert out["analyzed"] is True
    v = next(v for v in out["runs"][0]["slo"]["verdicts"] if v["metric"] == "ttft")
    assert v["statistic"] == "p99p9"
    assert v["observed"] is not None   # p99p9 ~57ms (from 0.057s) -> read off, not None
    assert v["met"] is True


async def test_analyze_results_skips_invalid_report(tool_ctx, tmp_path):
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "benchmark_report_v0.2.yaml").write_text(yaml.safe_dump({"version": "0.2", "run": {}}))
    out = await analyze.analyze_results(tool_ctx, sources=[str(bad)])
    assert out["analyzed"] is False
    assert out["skipped"] and out["skipped"][0]["reason"] == "report failed schema validation"


async def test_analyze_results_requires_input(tool_ctx):
    out = await analyze.analyze_results(tool_ctx)
    assert out["analyzed"] is False


async def test_analyze_results_invalid_slo_rejected(tool_ctx, br_example, tmp_path):
    base = load_report(br_example)
    run = tmp_path / "run"
    _write_report(run, base, 0.1, 100.0)
    # a malformed slo (negative target) is reported gracefully, not raised
    out = await analyze.analyze_results(tool_ctx, slo={"ttft_ms": -5}, sources=[str(run)])
    assert out["analyzed"] is False and "invalid SLO" in out["reason"]


async def test_analyze_results_empty_slo_means_no_slo(tool_ctx, br_example, tmp_path):
    # An empty slo dict == "no targets": analyze still runs (frontier/summary), no SLO tags.
    base = load_report(br_example)
    exp = tmp_path / "experiment"
    _write_report(exp / "a", base, 0.1, 100.0)
    _write_report(exp / "b", base, 0.2, 250.0)
    out = await analyze.analyze_results(tool_ctx, slo={}, experiment_dir=str(exp))
    assert out["analyzed"] is True and out["slo_targets"] is None
    assert "slo_feasible" not in out["pareto"]       # no SLO -> no feasible frontier
    assert "slo" not in out["runs"][0]


async def test_analyze_results_dispatch_and_registered(tool_ctx, br_example, tmp_path):
    assert "analyze_results" in {d["name"] for d in tool_definitions()}
    base = load_report(br_example)
    run = tmp_path / "run"
    _write_report(run, base, 0.15, 400.0)
    out = await dispatch(tool_ctx, "analyze_results",
                         {"slo": {"ttft_ms": 200}, "sources": [str(run)]})
    assert out["analyzed"] is True


# ---- SessionPlan SLO capture ----------------------------------------------

def test_session_plan_captures_slo():
    p = SessionPlan(
        use_case_summary="chat app", spec="cicd/kind", namespace="ns",
        harness="inference-perf", workload="sanity_random.yaml",
        slo={"ttft_ms": 200, "tpot_ms": 50},
    )
    assert isinstance(p.slo, SLOTargets)
    assert p.slo.ttft_ms == 200 and p.slo.tpot_ms == 50
    # round-trips through model_dump (used by propose_session_plan approval payload)
    assert p.model_dump()["slo"]["ttft_ms"] == 200


def test_session_plan_slo_optional():
    p = SessionPlan(
        use_case_summary="x", spec="cicd/kind", namespace="ns",
        harness="inference-perf", workload="sanity_random.yaml",
    )
    assert p.slo is None


def test_analyze_schema_accepts_slo_and_sources():
    m = AnalyzeResultsInput(slo={"ttft_ms": 200}, sources=["/a", "/b"], labels=["a", "b"])
    assert m.slo == {"ttft_ms": 200} and m.sources == ["/a", "/b"]


# ---- post-run next-step recommendations (C3) -------------------------------
#
# Mechanism: a deterministic ranking over the validated analyzer facts + read-only history
# facts, leaning toward save-to-trend / compare-to-baseline. Order in the list IS priority.

def _actions(steps):
    return [s["action"] for s in steps]


def test_next_steps_single_run_nothing_saved_leads_with_save_baseline():
    steps = recommend_next_steps(
        n_runs=1, has_slo=False, any_slo_met=None,
        history=HistoryContext(already_stored=False, comparable_prior=0, total_stored=0),
    )
    acts = _actions(steps)
    # save-to-trend is the FIRST recommendation; teardown is never first.
    assert acts[0] == "save_baseline"
    assert "baseline" in steps[0]["reason"]
    assert acts[-1] == "teardown"
    # priorities are 1..n in order
    assert [s["priority"] for s in steps] == list(range(1, len(steps) + 1))


def test_next_steps_already_saved_skips_save_and_offers_compare():
    # This exact run is already in the store and a comparable prior run exists -> the lead is
    # to COMPARE, not to save again.
    steps = recommend_next_steps(
        n_runs=1, has_slo=False, any_slo_met=None,
        history=HistoryContext(already_stored=True, comparable_prior=1, total_stored=2),
    )
    acts = _actions(steps)
    assert "save_baseline" not in acts          # already saved -> don't re-offer save
    assert acts[0] == "compare_to_baseline"
    assert "trend_metric" in acts               # >=2 comparable saved runs -> trend offered


def test_next_steps_no_comparable_prior_does_not_offer_compare():
    # First-ever run: nothing comparable to compare against yet.
    steps = recommend_next_steps(
        n_runs=1, has_slo=False, any_slo_met=None,
        history=HistoryContext(already_stored=False, comparable_prior=0, total_stored=0),
    )
    assert "compare_to_baseline" not in _actions(steps)


def test_next_steps_missed_slo_invites_rerun_after_save_compare():
    steps = recommend_next_steps(
        n_runs=1, has_slo=True, any_slo_met=False,
        history=HistoryContext(already_stored=False, comparable_prior=1, total_stored=1),
    )
    acts = _actions(steps)
    assert "run_again" in acts
    # save/compare still come BEFORE the operational run-again, which comes before teardown.
    assert acts.index("save_baseline") < acts.index("run_again") < acts.index("teardown")


def test_next_steps_single_met_slo_offers_sweep_not_rerun():
    steps = recommend_next_steps(
        n_runs=1, has_slo=True, any_slo_met=True,
        history=HistoryContext(),
    )
    acts = _actions(steps)
    assert "run_sweep" in acts and "run_again" not in acts


def test_next_steps_sweep_nudges_save_each_treatment_not_recompare():
    steps = recommend_next_steps(
        n_runs=3, has_slo=False, any_slo_met=None,
        history=HistoryContext(),
    )
    acts = _actions(steps)
    # a sweep already compared configs in this call: lead with save, nudge to trend across
    # runs/days, and never re-offer a single-run compare/run-again.
    assert acts[0] == "save_baseline" and "trend_metric" in acts
    assert "compare_to_baseline" not in acts and "run_again" not in acts


def test_next_steps_offers_analyze_with_plots_above_teardown():
    # The richer menu (J2): the analysis-plots step is always available, ranked above teardown
    # (never first), so "what next" is more than "save / run again / tear down".
    for kw in (
        dict(n_runs=1, has_slo=False, any_slo_met=None, history=HistoryContext()),
        dict(n_runs=3, has_slo=False, any_slo_met=None, history=HistoryContext()),
    ):
        steps = recommend_next_steps(**kw)
        acts = _actions(steps)
        assert "analyze_with_plots" in acts
        assert acts.index("analyze_with_plots") < acts.index("teardown")
        assert acts[0] != "analyze_with_plots"          # never leads
        assert acts[-1] == "teardown"                   # teardown still last
        assert steps[acts.index("analyze_with_plots")]["tool"] == "execute_llmdbenchmark"


# ---- next_steps surfaced through the analyze_results tool ------------------

async def test_analyze_results_emits_next_steps_save_first(tool_ctx, br_example, tmp_path):
    base = load_report(br_example)
    run = tmp_path / "run"
    _write_report(run, base, ttft_s=0.15, out_rate=400.0)
    out = await analyze.analyze_results(tool_ctx, sources=[str(run)])
    assert out["analyzed"] is True
    steps = out["next_steps"]
    assert steps and steps[0]["action"] == "save_baseline"   # nothing saved yet -> save leads
    assert steps[0]["tool"] == "result_history"
    assert steps[-1]["action"] == "teardown"                 # teardown never leads


async def test_analyze_results_next_steps_reflect_saved_history(tool_ctx, br_example, tmp_path):
    # Save this run to the cross-session store FIRST, then analyze it: the recommender should
    # see it's already stored and stop leading with save-baseline.
    from app.tools.analyze import history as history_tool
    base = load_report(br_example)
    run = tmp_path / "run"
    _write_report(run, base, ttft_s=0.15, out_rate=400.0)
    stored = await history_tool.result_history(tool_ctx, action="store", source=str(run))
    assert stored["stored"] is True
    out = await analyze.analyze_results(tool_ctx, sources=[str(run)])
    assert "save_baseline" not in {s["action"] for s in out["next_steps"]}


async def test_analyze_results_missed_slo_recommends_rerun(tool_ctx, br_example, tmp_path):
    # An unreachably-tight SLO -> the run misses it -> next_steps includes a run-again offer.
    base = load_report(br_example)
    run = tmp_path / "run"
    _write_report(run, base, ttft_s=2.0, out_rate=10.0)   # slow + low throughput
    out = await analyze.analyze_results(
        tool_ctx, slo={"ttft_ms": 1, "throughput_floor_tok_s": 9000}, sources=[str(run)]
    )
    assert out["analyzed"] is True
    assert out["runs"][0]["slo"]["overall_met"] is False
    assert "run_again" in {s["action"] for s in out["next_steps"]}
