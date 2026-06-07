"""Deterministic post-run results card (B2 / TODO #3).

After a run, the agent's prose summary is emergent (and varies turn to turn). To give the
non-expert a CONSISTENT, structured view we also emit a ``results_card`` event built HERE —
purely from the Results Analyzer's already-computed SLO/Pareto output
(``app/validation/analysis.py``). No free-form prose, no fabricated numbers: the card carries
only fields the analyzer produced.

Scope note — why ONLY ``analyze_results`` and not ``locate_and_parse_report``: the single-run
benchmark's structured view (the latency/throughput tiles, the percentile ladder, and the
per-run chart thumbnails) is already rendered by the frontend's report-summary card
(``renderReportSummary`` in ``ui/app.js``), driven directly from the same validated
``locate_and_parse_report`` result. Building a second card from that report here only duplicated
those metrics in a separate, chart-less table — so we don't. This card adds the one thing the
report-summary card does NOT carry: the analyzer's exact, deterministic SLO pass/fail verdicts
(single run) and the Pareto frontier (sweep).

This is mechanism — it reshapes already-computed, schema-validated facts into a flat render
model. It makes NO judgment about whether a result is "good" (that stays the agent's prose,
grounded in knowledge/results_interpretation.md + knowledge/analysis.md); the only verdicts it
surfaces are the analyzer's own exact pass/fail SLO verdicts, which are deterministic given the
report.

``build_results_card`` returns ``None`` when the tool result carries nothing renderable (a
non-analysis tool, or an analysis with no valid run), so the loop simply doesn't emit a card —
the agent's prose still stands on its own.
"""
from __future__ import annotations

from typing import Any


def build_results_card(tool_name: str, result: Any) -> dict[str, Any] | None:
    """Build the deterministic results card for an analyze_results tool result, or ``None``
    when there is nothing renderable.

    Single dispatch point (mechanism): the loop calls this after every tool result; only
    ``analyze_results`` yields a card (the single-run report's structured view is the frontend's
    report-summary card, so ``locate_and_parse_report`` deliberately yields ``None`` here to
    avoid duplicating it)."""
    if not isinstance(result, dict):
        return None
    if tool_name == "analyze_results":
        return _card_from_analysis(result)
    return None


def _card_from_analysis(result: dict[str, Any]) -> dict[str, Any] | None:
    """A card from an analyze_results result. One run -> a single-run card with SLO verdicts;
    a sweep -> a multi-run card with the per-run rows + the Pareto frontier."""
    if not result.get("analyzed"):
        return None
    runs = result.get("runs")
    if not isinstance(runs, list) or not runs:
        return None
    slo_targets = result.get("slo_targets")

    if len(runs) == 1:
        run = runs[0]
        # analyze_results doesn't re-embed the full summary on the run row, but DID compute the
        # exact SLO verdicts — surface those alongside whatever scalar metrics the verdicts and
        # standard metrics expose. The single-run latency/throughput table comes from the SLO
        # verdicts' observed values (which are derived from the validated report).
        card = _single_run_analysis_card(run)
        if slo_targets:
            card["slo_targets"] = slo_targets
        return card

    # Sweep: a comparison card.
    sweep_card: dict[str, Any] = {
        "kind": "sweep",
        "n": result.get("n") or len(runs),
        "runs": [{"label": r.get("label"), "model": r.get("model"),
                  "slo_met": (r.get("slo") or {}).get("overall_met")} for r in runs],
    }
    if slo_targets:
        sweep_card["slo_targets"] = slo_targets
    pareto = result.get("pareto")
    if isinstance(pareto, dict):
        sweep_card["frontier"] = pareto.get("frontier") or []
        sweep_card["slo_feasible"] = pareto.get("slo_feasible")
        sweep_card["objectives"] = [o.get("name") for o in (pareto.get("objectives") or [])]
    return sweep_card


def _single_run_analysis_card(run: dict[str, Any]) -> dict[str, Any]:
    """A single-run card from an analyze_results run row (which carries the exact SLO verdicts
    but not the full summary)."""
    card: dict[str, Any] = {
        "kind": "run",
        "model": run.get("model"),
        "run_uid": run.get("run_uid"),
    }
    slo = run.get("slo")
    if isinstance(slo, dict):
        card["slo"] = {
            "overall_met": slo.get("overall_met"),
            "checked_count": slo.get("checked_count"),
            "success_rate_pct": slo.get("success_rate_pct"),
            "goodput": slo.get("goodput"),
            # Each verdict is a deterministic, exact pass/fail at a stated statistic.
            "verdicts": [
                {"metric": v.get("metric"), "statistic": v.get("statistic"),
                 "direction": v.get("direction"), "target": v.get("target"),
                 "observed": v.get("observed"), "units": v.get("units"), "met": v.get("met")}
                for v in (slo.get("verdicts") or [])
            ],
        }
    if run.get("standard_metrics"):
        card["standard_metrics"] = run["standard_metrics"]
    if run.get("session_performance"):
        card["session_performance"] = run["session_performance"]
    return {k: v for k, v in card.items() if v is not None}
