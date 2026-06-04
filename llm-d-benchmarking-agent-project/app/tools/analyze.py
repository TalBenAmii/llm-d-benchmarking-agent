"""analyze_results — the Results Analyzer tool (proposal §3.4).

Read-only. Given user SLO targets and one-or-more Benchmark Reports (a single run, an
A/B pair, or a whole DoE sweep dir), it:
  * validates each report against the repo's BR v0.2 schema (never scrapes logs),
  * computes a per-run SLO verdict + an honest goodput *estimate* (the key differentiator),
  * for a sweep, identifies the Pareto-optimal configurations and the SLO-feasible frontier.

All judgment (what the numbers mean, which config to pick) is the agent's, grounded in
knowledge/analysis.md — this handler is resolution + the pure math in
``app/validation/analysis.py``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.tools.context import ToolContext
from app.validation.analysis import (
    HistoryContext,
    SLOTargets,
    evaluate_slo,
    pareto_analysis,
    recommend_next_steps,
)
from app.validation.report import (
    find_reports,
    load_report,
    summarize_report,
    validate_report,
)


def _history_context(ctx: ToolContext, valid_entries: list[dict[str, Any]]) -> HistoryContext:
    """Read-only facts the next-step recommender needs from the cross-session trend store.

    For a SINGLE run we look up whether THIS run's report is already saved (by run_uid) and
    how many *comparable* prior runs (same model) are already stored. For a sweep we only
    report the totals (the per-treatment store status isn't the lead there). Best-effort:
    any store error degrades to an empty context (the recommender still leans on save first)."""
    try:
        records = ctx.history_store().list()
    except Exception:  # the store is best-effort; never break analysis on a read error
        return HistoryContext()
    if not valid_entries:
        return HistoryContext(total_stored=len(records))
    summaries = [e["summary"] for e in valid_entries]
    run_uids = {s.get("run_uid") for s in summaries if s.get("run_uid")}
    models = {s.get("model") for s in summaries if s.get("model")}
    already = any(r.run_uid in run_uids for r in records) if run_uids else False
    comparable_prior = sum(
        1 for r in records
        if r.model in models and r.run_uid not in run_uids
    )
    return HistoryContext(
        already_stored=already,
        comparable_prior=comparable_prior,
        total_stored=len(records),
    )


def _resolve(
    sources: list[str] | None,
    experiment_dir: str | None,
    labels: list[str] | None,
) -> list[tuple[str, Path | None]]:
    """Resolve inputs to a list of (label, report_path|None). Mirrors compare_reports so
    the analyzer accepts the same shapes (a sweep dir, or explicit run dirs/files)."""
    if experiment_dir:
        paths = find_reports([experiment_dir])
        return [(p.parent.name, p) for p in paths]
    resolved: list[tuple[str, Path | None]] = []
    for i, src in enumerate(sources or []):
        p = Path(src)
        report: Path | None
        if p.is_file():
            report = p
        else:
            found = find_reports([p], newest_only=True)
            report = found[0] if found else None
        label = (labels[i] if labels and i < len(labels) else None) or (
            report.parent.name if report else src
        )
        resolved.append((label, report))
    return resolved


async def analyze_results(
    ctx: ToolContext,
    *,
    slo: dict[str, Any] | None = None,
    sources: list[str] | None = None,
    experiment_dir: str | None = None,
    labels: list[str] | None = None,
) -> dict[str, Any]:
    if not sources and not experiment_dir:
        return {"analyzed": False, "reason": "provide `sources` (1+ run dirs/files) or `experiment_dir`"}

    slo_targets: SLOTargets | None = None
    if slo:
        try:
            slo_targets = SLOTargets(**slo)
        except Exception as exc:  # pydantic validation error (e.g. no targets set)
            return {"analyzed": False, "reason": f"invalid SLO targets: {exc}"}

    entries = _resolve(sources, experiment_dir, labels)
    schema_path = ctx.settings.benchmark_report_schema_path

    valid_entries: list[dict[str, Any]] = []   # {label, summary}
    runs: list[dict[str, Any]] = []            # per-run report status + SLO verdict
    skipped: list[dict[str, Any]] = []

    for label, path in entries:
        if path is None:
            skipped.append({"label": label, "reason": "no benchmark report found"})
            continue
        report = load_report(path)
        validation = validate_report(report, schema_path)
        if not validation.valid:
            skipped.append({"label": label, "reason": "report failed schema validation",
                            "errors": validation.errors[:5]})
            continue
        summary = summarize_report(report)
        valid_entries.append({"label": label, "summary": summary})
        run_item: dict[str, Any] = {"label": label, "report_path": str(path),
                                    "model": summary.get("model"), "run_uid": summary.get("run_uid")}
        # Surface the §3.4 standard metrics (KV-cache hit rate / schedule delay / GPU util)
        # per run when the report carried them — omitted (None) otherwise, never fabricated.
        # This makes them visible for a SINGLE run too (no sweep -> no Pareto block).
        if summary.get("standard_metrics"):
            run_item["standard_metrics"] = summary["standard_metrics"]
        # Session-level metrics (multi-turn inference-perf): surfaced per run when the report
        # carried results.session_performance — None/omitted for single-turn, never fabricated.
        if summary.get("session_performance"):
            run_item["session_performance"] = summary["session_performance"]
        if slo_targets is not None:
            run_item["slo"] = evaluate_slo(summary, slo_targets)
        runs.append(run_item)

    if not valid_entries:
        return {"analyzed": False, "reason": "no valid benchmark report to analyze",
                "skipped": skipped}

    out: dict[str, Any] = {
        "analyzed": True,
        "n": len(valid_entries),
        "slo_targets": slo_targets.model_dump(exclude_none=True) if slo_targets else None,
        "runs": runs,
        "skipped": skipped,
    }

    # Sweep/DoE analysis needs at least two comparable runs.
    if len(valid_entries) >= 2:
        out["pareto"] = pareto_analysis(valid_entries, slo=slo_targets)

    # Structured post-run next steps over the validated facts: lean toward saving this
    # result to the trend store and comparing it to a baseline (proposal historical-storage
    # value), not just "teardown or run again". Mechanism only — the agent makes the offer
    # (knowledge/conversation_style.md + knowledge/history.md). any_slo_met == True iff a
    # run was checked against SLOs and at least one passed overall.
    any_slo_met: bool | None = None
    if slo_targets is not None:
        verdicts = [r["slo"]["overall_met"] for r in runs if "slo" in r]
        any_slo_met = any(verdicts) if verdicts else None
    out["next_steps"] = recommend_next_steps(
        n_runs=len(valid_entries),
        has_slo=slo_targets is not None,
        any_slo_met=any_slo_met,
        on_sweep=len(valid_entries) >= 2,
        history=_history_context(ctx, valid_entries),
    )

    return out
