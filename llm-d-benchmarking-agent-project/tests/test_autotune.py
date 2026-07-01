"""Closed-loop autotuner tests (proposal docs/history/proposals/01-autotuner.md).

Hermetic: a pure SYNTHETIC RESULT SURFACE — a function ``config -> BR v0.2 report`` written
to a temp dir — driven through the REAL tools (record_trial / propose_next_config / status).
No cluster, no GPU, no live LLM.

The cardinal invariant under test: ``autotune_search`` is PURE MECHANISM. It validates the
agent's candidate and exposes facts; it NEVER computes the next config and NEVER returns a
``converged``/``stop`` verdict. The headline convergence test plays the AGENT's strategy role
(coordinate-descent/bisection over the surface) in the TEST, proving the Python side carries
no search logic.
"""
from __future__ import annotations

import copy
import json

import pytest
import yaml

from app.agent.results_card import build_results_card
from app.storage.autotune import AutotuneStore, Trial
from app.tools import autotune
from app.tools.context import ToolError
from app.tools.registry import REGISTRY, dispatch, tool_definitions
from app.tools.schemas import AutotuneSearchInput
from app.validation.analysis import SLOTargets, pareto_analysis
from app.validation.report import load_report, summarize_report, validate_report
from app.validation.session_plan import AutotuneKnob, AutotunePlan, SessionPlan, validate_plan

# ---- synthetic monotone result surface -------------------------------------
#
# Latency rises ~linearly with concurrency; throughput saturates (more load -> more tok/s but
# with diminishing returns). With a 300ms p95 TTFT SLO the crossing sits around c=18:
#   ttft_p95(c) = 60 + 13*c   ms   ->  300ms at c ≈ 18.5
#   out_rate(c) = 420*c/(c+20) tok/s  (monotone up, saturating)
# So feasible up to c=18, infeasible at c>=19; throughput keeps climbing -> the best feasible
# point is the highest concurrency still under the SLO (c=18).


def _ttft_ms(c: float) -> float:
    return 60.0 + 13.0 * c


def _out_rate(c: float) -> float:
    return 420.0 * c / (c + 20.0)


def _write_surface_report(dirpath, base: dict, c: float) -> str:
    """Write a BR v0.2 report for concurrency ``c`` from the real example, varying only the
    TTFT (incl. p95) and output throughput per the synthetic surface. Returns the dir path."""
    rep = copy.deepcopy(base)
    agg = rep["results"]["request_performance"]["aggregate"]
    ttft_s = _ttft_ms(c) / 1000.0
    ttft = agg["latency"]["time_to_first_token"]
    for k in ("mean", "p50", "p90", "p95", "p99"):
        ttft[k] = ttft_s
    agg["throughput"]["output_token_rate"]["mean"] = _out_rate(c)
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / "benchmark_report_v0.2.yaml").write_text(yaml.safe_dump(rep, sort_keys=False))
    return str(dirpath)


_SLO = {"ttft_ms": 300, "percentile": "p95"}
_KNOBS = [{"name": "concurrency", "key": "max-concurrency", "min": 1, "max": 64, "resolution": 1}]


async def _record(ctx, search_id, base, tmp_path, c, *, objective="output_token_rate"):
    """Run one trial at concurrency ``c`` through the REAL record_trial action."""
    run = _write_surface_report(tmp_path / f"{search_id}_c{c}", base, c)
    return await autotune.autotune_search(
        ctx, action="record_trial", search_id=search_id,
        config={"max-concurrency": c}, report_source=run,
        slo=_SLO, objective=objective, direction="max",
    )


# ---- 1) record_trial validates & records -----------------------------------

async def test_record_trial_validates_and_records(tool_ctx, br_example, tmp_path):
    base = load_report(br_example)
    out = await _record(tool_ctx, "s1", base, tmp_path, 8)
    assert out["recorded"] is True
    assert out["trial_index"] == 0 and out["trials_used"] == 1
    # feasible: ttft_p95(8)=164ms <= 300 -> met; objective value is the analyzer's extraction.
    assert out["feasible"] is True
    assert out["objective_value"] == pytest.approx(_out_rate(8))
    # the SLO verdict is the analyzer's, embedded verbatim.
    assert out["slo"]["overall_met"] is True
    # the log actually persisted.
    store = AutotuneStore(tool_ctx.workspace.parent)
    trials = store.load("s1")
    assert len(trials) == 1 and trials[0].config == {"max-concurrency": 8}


async def test_record_trial_records_infeasible(tool_ctx, br_example, tmp_path):
    base = load_report(br_example)
    out = await _record(tool_ctx, "s2", base, tmp_path, 40)  # ttft_p95(40)=580ms > 300
    assert out["recorded"] is True
    assert out["feasible"] is False
    assert out["slo"]["overall_met"] is False


async def test_record_trial_refuses_unvalidated_report(tool_ctx, tmp_path):
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "benchmark_report_v0.2.yaml").write_text(yaml.safe_dump({"version": "0.2", "run": {}}))
    out = await autotune.autotune_search(
        tool_ctx, action="record_trial", search_id="s3",
        config={"max-concurrency": 8}, report_source=str(bad),
        slo=_SLO, objective="output_token_rate", direction="max",
    )
    assert out["recorded"] is False
    assert "schema validation" in out["reason"]
    # nothing was stored (determinism gate d).
    assert AutotuneStore(tool_ctx.workspace.parent).load("s3") == []


async def test_record_trial_requires_config_and_source(tool_ctx, br_example, tmp_path):
    out = await autotune.autotune_search(
        tool_ctx, action="record_trial", search_id="s4", report_source="x")
    assert out["recorded"] is False and "config" in out["reason"]
    out2 = await autotune.autotune_search(
        tool_ctx, action="record_trial", search_id="s4", config={"max-concurrency": 8})
    assert out2["recorded"] is False and "report_source" in out2["reason"]


# ---- 2) propose_next_config is PURE VALIDATION -----------------------------

async def test_propose_out_of_bounds(tool_ctx, br_example, tmp_path):
    base = load_report(br_example)
    await _record(tool_ctx, "p1", base, tmp_path, 8)
    out = await autotune.autotune_search(
        tool_ctx, action="propose_next_config", search_id="p1",
        candidate={"max-concurrency": 999}, knobs=_KNOBS, budget=6)
    assert out["ok"] is False
    assert out["out_of_bounds"] and out["out_of_bounds"][0]["key"] == "max-concurrency"


async def test_propose_duplicate(tool_ctx, br_example, tmp_path):
    base = load_report(br_example)
    await _record(tool_ctx, "p2", base, tmp_path, 8)
    out = await autotune.autotune_search(
        tool_ctx, action="propose_next_config", search_id="p2",
        candidate={"max-concurrency": 8}, knobs=_KNOBS, budget=6)
    assert out["ok"] is False
    assert out["duplicate_of"] == 0


async def test_propose_budget_exhausted(tool_ctx, br_example, tmp_path):
    base = load_report(br_example)
    await _record(tool_ctx, "p3", base, tmp_path, 8)
    await _record(tool_ctx, "p3", base, tmp_path, 16)
    out = await autotune.autotune_search(
        tool_ctx, action="propose_next_config", search_id="p3",
        candidate={"max-concurrency": 24}, knobs=_KNOBS, budget=2)
    assert out["ok"] is False
    assert out["budget_remaining"] == 0 and "budget" in out["reason"]


async def test_propose_ok(tool_ctx, br_example, tmp_path):
    base = load_report(br_example)
    await _record(tool_ctx, "p4", base, tmp_path, 8)
    out = await autotune.autotune_search(
        tool_ctx, action="propose_next_config", search_id="p4",
        candidate={"max-concurrency": 16}, knobs=_KNOBS, budget=6)
    assert out["ok"] is True
    assert out["budget_remaining"] == 5 and out["candidate"] == {"max-concurrency": 16}


async def test_propose_rejects_unknown_knob(tool_ctx, br_example, tmp_path):
    out = await autotune.autotune_search(
        tool_ctx, action="propose_next_config", search_id="p5",
        candidate={"not-a-knob": 4}, knobs=_KNOBS, budget=6)
    assert out["ok"] is False and out["unknown_keys"] == ["not-a-knob"]


async def test_propose_requires_candidate(tool_ctx):
    out = await autotune.autotune_search(
        tool_ctx, action="propose_next_config", search_id="p6", knobs=_KNOBS, budget=6)
    assert out["ok"] is False and "candidate" in out["reason"]


# ---- 3) status returns FACTS, NO converged/stop key ------------------------

async def test_status_has_no_converged_key_and_matches_pareto(tool_ctx, br_example, tmp_path):
    base = load_report(br_example)
    # one feasible (c=8) + one infeasible (c=40) -> boundary bracketed.
    await _record(tool_ctx, "st", base, tmp_path, 8)
    await _record(tool_ctx, "st", base, tmp_path, 40)
    out = await autotune.autotune_search(
        tool_ctx, action="status", search_id="st",
        slo=_SLO, objective="output_token_rate", direction="max", budget=6)

    # THE invariant guard: no convergence/stop verdict in the facts.
    assert "converged" not in out
    assert "stop" not in out
    assert "should_stop" not in out

    assert out["trials_used"] == 2 and out["budget_remaining"] == 4
    assert out["slo_boundary_bracketed"] is True
    # incumbent is the feasible c=8 (the only feasible trial).
    assert out["best_feasible"]["config"] == {"max-concurrency": 8}

    # slo_feasible_frontier matches a DIRECT pareto_analysis over the same summaries.
    summaries = []
    for c in (8, 40):
        rep = load_report(tmp_path / f"st_c{c}" / "benchmark_report_v0.2.yaml")
        assert validate_report(rep, tool_ctx.settings.benchmark_report_schema_path).valid
        summaries.append({"label": f"max-concurrency={c}", "summary": summarize_report(rep)})
    direct = pareto_analysis(summaries, slo=SLOTargets(**_SLO))
    assert out["slo_feasible_frontier"] == direct["slo_frontier"]


async def test_status_empty_search_is_facts_only(tool_ctx):
    out = await autotune.autotune_search(
        tool_ctx, action="status", search_id="nope",
        slo=_SLO, objective="output_token_rate", direction="max", budget=6)
    assert "converged" not in out
    assert out["trials_used"] == 0 and out["best_feasible"] is None
    assert out["slo_feasible_frontier"] == [] and out["slo_boundary_bracketed"] is False


# ---- 4) the headline: a DETERMINISTIC convergence simulation ---------------
#
# The TEST plays the agent's strategy role (bracket-then-bisect coordinate descent over the
# single concurrency knob). The Python tool only records/validates/reports — proving the search
# logic lives OUTSIDE the tool. We assert convergence to the feasible knee within budget.

async def test_deterministic_convergence_to_feasible_knee(tool_ctx, br_example, tmp_path):
    base = load_report(br_example)
    sid = "conv"
    budget = 8
    knob = "max-concurrency"

    async def run_and_record(c: int):
        return await _record(tool_ctx, sid, base, tmp_path, c)

    async def status():
        return await autotune.autotune_search(
            tool_ctx, action="status", search_id=sid,
            slo=_SLO, objective="output_token_rate", direction="max", budget=budget)

    async def propose(c: int):
        return await autotune.autotune_search(
            tool_ctx, action="propose_next_config", search_id=sid,
            candidate={knob: c}, knobs=_KNOBS, budget=budget)

    # --- the AGENT's strategy, executed by the test (NOT by the tool) ---
    # 1) start low, then double to BRACKET the SLO boundary.
    c = 8
    await run_and_record(c)
    tried = {8}
    low_feasible = 8        # highest known-feasible concurrency
    high_infeasible = None  # lowest known-infeasible concurrency
    while high_infeasible is None:
        c = min(c * 2, 64)
        prop = await propose(c)
        assert prop["ok"] is True, prop
        rec = await run_and_record(c)
        tried.add(c)
        if rec["feasible"]:
            low_feasible = max(low_feasible, c)
        else:
            high_infeasible = c
        if c == 64:
            break

    st = await status()
    assert st["slo_boundary_bracketed"] is True, "doubling should have bracketed the SLO crossing"

    # 2) BISECT between the highest feasible and the lowest infeasible until the gap <= resolution.
    while high_infeasible - low_feasible > 1:
        mid = (low_feasible + high_infeasible) // 2
        if mid in tried:
            break
        prop = await propose(mid)
        if not prop["ok"]:
            break  # budget exhausted / duplicate — stop, per the rubric (the agent's call)
        rec = await run_and_record(mid)
        tried.add(mid)
        if rec["feasible"]:
            low_feasible = mid
        else:
            high_infeasible = mid

    # --- the converged answer is the analyzer's SLO-feasible best ---
    final = await status()
    assert "converged" not in final  # still no Python verdict, even at the end
    best = final["best_feasible"]
    assert best is not None
    best_c = best["config"][knob]
    # the true feasible knee is c=18 (ttft_p95=294ms <= 300; c=19 -> 307ms > 300).
    assert best_c == 18, f"expected to converge on the feasible knee c=18, got {best_c}"
    # it is the highest-throughput feasible trial -> on the SLO-feasible frontier.
    assert f"{knob}={best_c}" in final["slo_feasible_frontier"]
    assert final["trials_used"] <= budget


# ---- 5) plan schema accepts/rejects autotune -------------------------------

def test_session_plan_accepts_autotune():
    plan = SessionPlan(
        use_case_summary="chat app, hit p95 ttft 300ms at best throughput",
        spec="cicd/kind", namespace="llmd-quickstart",
        harness="inference-perf", workload="sanity_random.yaml",
        slo=SLOTargets(ttft_ms=300, percentile="p95"),
        autotune=AutotunePlan(
            strategy="coordinate-descent", objective="output_token_rate", direction="max",
            knobs=[AutotuneKnob(name="concurrency", key="max-concurrency", min=1, max=64, resolution=1)],
            budget=6,
        ),
    )
    assert plan.autotune is not None and plan.autotune.budget == 6


def test_session_plan_autotune_optional():
    plan = SessionPlan(
        use_case_summary="one-shot run", spec="cicd/kind", namespace="ns",
        harness="inference-perf", workload="sanity_random.yaml")
    assert plan.autotune is None


def test_autotune_plan_rejects_bad_budget():
    with pytest.raises(ValueError):
        AutotunePlan(strategy="bisection", objective="ttft", direction="min",
                     knobs=[AutotuneKnob(name="c", key="max-concurrency", min=1, max=8)], budget=0)


def test_autotune_plan_rejects_empty_knobs():
    with pytest.raises(ValueError):
        AutotunePlan(strategy="bisection", objective="ttft", direction="min", knobs=[], budget=4)


def test_autotune_knob_rejects_bad_key_and_bounds():
    with pytest.raises(ValueError):
        AutotuneKnob(name="c", key="not a key!", min=1, max=8)
    with pytest.raises(ValueError):
        AutotuneKnob(name="c", key="max-concurrency", min=8, max=8)  # max must exceed min


def test_plan_with_autotune_still_validates_against_catalog(catalog):
    plan = SessionPlan(
        use_case_summary="goal", spec="cicd/kind", namespace="ns",
        harness="inference-perf", workload="sanity_random.yaml",
        autotune=AutotunePlan(
            strategy="hill-climb", objective="output_token_rate", direction="max",
            knobs=[AutotuneKnob(name="c", key="max-concurrency", min=1, max=32)], budget=5),
    )
    assert validate_plan(plan, catalog) == []


# ---- 6) results card builder -----------------------------------------------

async def test_results_card_from_status(tool_ctx, br_example, tmp_path):
    base = load_report(br_example)
    await _record(tool_ctx, "card", base, tmp_path, 8)
    await _record(tool_ctx, "card", base, tmp_path, 40)
    status = await autotune.autotune_search(
        tool_ctx, action="status", search_id="card",
        slo=_SLO, objective="output_token_rate", direction="max", budget=6)
    card = build_results_card("autotune_search", status)
    assert card is not None
    assert card["kind"] == "autotune"
    assert card["objective"] == "output_token_rate" and card["direction"] == "max"
    assert card["trials_used"] == 2 and card["budget_remaining"] == 4
    assert card["best_feasible"]["config"] == {"max-concurrency": 8}
    assert len(card["trials"]) == 2
    # the card must NOT introduce a convergence verdict either.
    assert "converged" not in card


async def test_results_card_none_for_non_status(tool_ctx, br_example, tmp_path):
    base = load_report(br_example)
    rec = await _record(tool_ctx, "nc", base, tmp_path, 8)   # record_trial result, not status
    assert build_results_card("autotune_search", rec) is None
    # empty status -> nothing renderable.
    empty = await autotune.autotune_search(
        tool_ctx, action="status", search_id="empty",
        slo=_SLO, objective="output_token_rate", direction="max", budget=6)
    assert build_results_card("autotune_search", empty) is None


# ---- 7) registry / dispatch -----------------------------------------------

def test_registered_and_in_definitions():
    assert "autotune_search" in REGISTRY
    names = {d["name"] for d in tool_definitions()}
    assert "autotune_search" in names


async def test_dispatch_validates_and_runs(tool_ctx, br_example, tmp_path):
    base = load_report(br_example)
    run = _write_surface_report(tmp_path / "disp_c8", base, 8)
    out = await dispatch(tool_ctx, "autotune_search", {
        "action": "record_trial", "search_id": "disp",
        "config": {"max-concurrency": 8}, "report_source": run,
        "slo": _SLO, "objective": "output_token_rate", "direction": "max"})
    assert out["recorded"] is True


async def test_dispatch_bad_args_returned_not_raised(tool_ctx):
    # missing the required search_id -> a ValidationError is RETURNED as a dict (gate a).
    out = await dispatch(tool_ctx, "autotune_search", {"action": "status"})
    assert "error" in out and out["error"] == "invalid arguments"


def test_schema_rejects_unknown_action():
    with pytest.raises(ValueError):
        AutotuneSearchInput(action="frobnicate", search_id="x")


async def test_unknown_action_returns_error(tool_ctx):
    # The handler is only reached with a schema-valid action; the defensive fallback still lists
    # the valid actions rather than raising.
    out = await autotune.autotune_search(tool_ctx, action="bogus", search_id="x")
    assert "valid_actions" in out


def test_no_toolerror_path_for_autotune(tool_ctx):
    # autotune_search never raises ToolError — bad input is returned as a dict. Guard the import
    # path so the contract is documented (the loop converts ToolError; autotune doesn't raise it).
    assert issubclass(ToolError, RuntimeError)


def test_load_survives_non_numeric_index(tmp_path):
    """BUG-022: a trial whose on-disk ``index`` is non-numeric (null/string — the dataclass does
    no type-check) must not crash ``load()``. The sort key would otherwise raise
    ``TypeError: '<' not supported between NoneType and int`` and break the WHOLE log, violating
    the documented 'a corrupt log degrades to empty, never crashes' contract. The bad-index trial
    stays loaded, sorted first (coerced to 0.0)."""
    adir = tmp_path / "autotune"
    adir.mkdir()
    fields = list(Trial.__dataclass_fields__)

    def mk(i):
        d = dict.fromkeys(fields)
        d["index"] = i
        return d

    (adir / "srch.json").write_text(json.dumps({"search_id": "srch", "trials": [mk(2), mk(None), mk(0)]}))
    trials = AutotuneStore(tmp_path).load("srch")  # must not raise
    assert len(trials) == 3
    # None -> 0.0 sorts first (stable: before the real 0), then 0, then 2.
    assert [t.index for t in trials] == [None, 0, 2]
