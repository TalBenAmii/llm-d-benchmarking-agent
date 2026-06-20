"""compare_harness_runs — cross-harness comparison for a multi-harness session (Phase 10).

The proposal's stretch goal: in ONE session the agent recommends + runs both
``inference-perf`` (SLO / latency validation) and ``guidellm`` (throughput sweep), then
compares them. ``compare_reports`` already contrasts configurations of the *same* harness;
this tool contrasts reports produced by *different* harnesses.

Read-only. It locates each report, validates it against the repo's BR v0.2 schema (reusing
the same validation as ``compare_reports`` / ``locate_and_parse_report`` — never scrape
logs), detects which harness produced each from the report's own
``scenario.load.standardized.tool`` field, then groups + contrasts them. The comparison
MATH lives in ``validation/report.py`` (pure + tested); this handler is resolution + wiring.
WHAT each harness is good for, and how to reconcile their differing methodologies, is the
agent's judgment — see ``knowledge/multi_harness.md``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.tools.context import ToolContext
from app.validation.report import (
    ReportError,
    compare_across_harnesses,
    find_reports,
    load_report,
    summarize_report,
    validate_report,
)


def _resolve(sources: list[str], labels: list[str] | None) -> list[tuple[str, Path | None]]:
    """Resolve each source (a report file or a run dir) to (label, report_path|None)."""
    resolved: list[tuple[str, Path | None]] = []
    for i, src in enumerate(sources):
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


async def compare_harness_runs(
    ctx: ToolContext,
    *,
    sources: list[str],
    labels: list[str] | None = None,
) -> dict[str, Any]:
    entries_in = _resolve(sources, labels)
    schema_path = ctx.settings.benchmark_report_schema_path

    valid_entries: list[dict[str, Any]] = []   # {label, summary}
    reports: list[dict[str, Any]] = []          # per-input provenance + validity
    skipped: list[dict[str, Any]] = []

    for label, path in entries_in:
        if path is None:
            skipped.append({"label": label, "reason": "no benchmark report found"})
            continue
        try:
            report = load_report(path)
        except ReportError as exc:
            # Present but corrupt/unreadable (e.g. truncated by an OOM-killed run) → skip this one
            # report and keep contrasting the rest, exactly as compare_reports does (BUG-031),
            # instead of aborting the whole cross-harness comparison.
            skipped.append({"label": label, "reason": "report unreadable", "errors": [str(exc)]})
            continue
        validation = validate_report(report, schema_path)
        summary = summarize_report(report)
        reports.append({
            "label": label,
            "report_path": str(path),
            "valid": validation.valid,
            "harness": summary.get("harness"),
            "model": summary.get("model"),
            "run_uid": summary.get("run_uid"),
        })
        if validation.valid:
            valid_entries.append({"label": label, "summary": summary})
        else:
            skipped.append({"label": label, "reason": "report failed schema validation",
                            "errors": validation.errors[:5]})

    if len(valid_entries) < 2:
        return {
            "compared": False,
            "reason": "need at least two valid reports to contrast",
            "reports": reports,
            "skipped": skipped,
        }

    try:
        cross = compare_across_harnesses(valid_entries)
    except ReportError as exc:
        # All the valid reports came from a single harness — point at compare_reports.
        return {
            "compared": False,
            "reason": str(exc),
            "hint": "all reports are from one harness — use compare_reports for a "
                    "same-harness A/B or sweep instead",
            "reports": reports,
            "skipped": skipped,
        }

    return {
        "compared": True,
        "n": cross["n"],
        "cross": cross,
        "reports": reports,
        "skipped": skipped,
    }
