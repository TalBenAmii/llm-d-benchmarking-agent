"""Deterministic post-run results card (B2 / TODO #3).

After a run, the agent's prose summary is emergent (and varies turn to turn). To give the
non-expert a CONSISTENT, structured view of "what the benchmark actually measured" we also
emit a ``results_card`` event built HERE — purely from the already-validated Benchmark Report
v0.2 summary (``summarize_report``) and, when present, the Results Analyzer's SLO/Pareto output
(``app/validation/analysis.py``). No free-form prose, no fabricated numbers: the card carries
only fields the validated report/analyzer produced.

This is mechanism — it reshapes already-computed, schema-validated facts into a flat render
model. It makes NO judgment about whether a result is "good" (that stays the agent's prose,
grounded in knowledge/results_interpretation.md + knowledge/analysis.md); the only verdicts it
surfaces are the analyzer's own exact pass/fail SLO verdicts, which are deterministic given the
report.

``build_results_card`` returns ``None`` when the tool result carries nothing renderable (an
unfound/invalid report, or an analysis with no valid run), so the loop simply doesn't emit a
card — the agent's prose still stands on its own.
"""
from __future__ import annotations

from typing import Any

# The metric rows the card renders, in display order: (summary path, human label, unit hint).
# Pulled from the validated summary's latency/throughput blocks (mean + percentile ladder).
_LATENCY_ROWS: tuple[tuple[str, str], ...] = (
    ("ttft", "Time to first token"),
    ("tpot", "Time per output token"),
    ("itl", "Inter-token latency"),
    ("request_latency", "End-to-end request latency"),
)
_THROUGHPUT_ROWS: tuple[tuple[str, str], ...] = (
    ("output_token_rate", "Output token throughput"),
    ("total_token_rate", "Total token throughput"),
    ("request_rate", "Request throughput"),
)

# Which representative statistic to show per metric (prefer the tail the SLO usually
# constrains, then degrade through the ladder, then the mean). Deterministic — never a guess.
_STAT_PREFERENCE = ("mean", "p50", "p90", "p95", "p99")


def build_results_card(tool_name: str, result: Any) -> dict[str, Any] | None:
    """Build the deterministic results card for a locate_and_parse_report / analyze_results
    tool result, or ``None`` when there is nothing renderable.

    Single dispatch point (mechanism): the loop calls this for those two tools only and emits a
    ``results_card`` event when it returns a dict."""
    if not isinstance(result, dict):
        return None
    if tool_name == "locate_and_parse_report":
        return _card_from_report(result)
    if tool_name == "analyze_results":
        return _card_from_analysis(result)
    return None


def _card_from_report(result: dict[str, Any]) -> dict[str, Any] | None:
    """A single-run card from a locate_and_parse_report result."""
    if not result.get("found") or not result.get("valid"):
        return None
    summary = result.get("summary")
    if not isinstance(summary, dict):
        return None
    card = _run_card(summary)
    if result.get("simulated"):
        card["simulated"] = True
    charts = result.get("charts")
    if isinstance(charts, list) and charts:
        card["charts"] = charts
    card["report_path"] = result.get("report_path")
    return card


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


def _run_card(summary: dict[str, Any]) -> dict[str, Any]:
    """The shared single-run render model from a validated report summary."""
    card: dict[str, Any] = {
        "kind": "run",
        "model": summary.get("model"),
        "harness": summary.get("harness"),
        "run_uid": summary.get("run_uid"),
        "duration": summary.get("duration"),
        "requests_total": summary.get("requests_total"),
        "requests_failures": summary.get("requests_failures"),
        "success_rate_pct": summary.get("success_rate_pct"),
        "load": summary.get("load"),
        "metrics": _metric_rows(summary),
    }
    std = summary.get("standard_metrics")
    if std:
        card["standard_metrics"] = std
    if summary.get("session_performance"):
        card["session_performance"] = summary["session_performance"]
    return {k: v for k, v in card.items() if v is not None}


def _metric_rows(summary: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten the latency + throughput blocks into ordered {label, value, units, stat} rows.

    Only rows the report actually carried are emitted (never fabricated). Each row shows the
    representative statistic per ``_STAT_PREFERENCE``."""
    rows: list[dict[str, Any]] = []
    raw_latency = summary.get("latency")
    raw_throughput = summary.get("throughput")
    latency: dict[str, Any] = raw_latency if isinstance(raw_latency, dict) else {}
    throughput: dict[str, Any] = raw_throughput if isinstance(raw_throughput, dict) else {}
    for key, label in _LATENCY_ROWS:
        row = _stat_row(latency.get(key), label, "lower is better")
        if row is not None:
            rows.append(row)
    for key, label in _THROUGHPUT_ROWS:
        row = _stat_row(throughput.get(key), label, "higher is better")
        if row is not None:
            rows.append(row)
    return rows


def _stat_row(metric_obj: Any, label: str, direction: str) -> dict[str, Any] | None:
    if not isinstance(metric_obj, dict):
        return None
    for stat in _STAT_PREFERENCE:
        v = metric_obj.get(stat)
        if isinstance(v, (int, float)):
            return {"label": label, "value": v, "stat": stat,
                    "units": metric_obj.get("units"), "direction": direction}
    return None


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
