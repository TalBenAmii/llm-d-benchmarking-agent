"""compare_reports — side-by-side comparison of multiple Benchmark Reports (A/B + sweeps).

Read-only. Locates each report, validates it against the repo's BR v0.2 schema (reusing
the same validation as ``locate_and_parse_report`` — never scrape logs), then computes
per-metric deltas vs a baseline. The comparison MATH lives in ``validation/report.py``
(pure + tested); this handler is just resolution + wiring. Interpreting the deltas for the
user is the agent's job (see knowledge/sweep_playbook.md).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.tools.context import ToolContext
from app.validation.report import (
    compare_summaries,
    find_reports,
    load_report,
    summarize_report,
    validate_report,
)


def _resolve(
    ctx: ToolContext,
    sources: list[str] | None,
    experiment_dir: str | None,
    labels: list[str] | None,
) -> list[tuple[str, Path | None]]:
    """Resolve the comparison inputs to a list of (label, report_path|None)."""
    if experiment_dir:
        # A DoE experiment writes one report per run treatment — grab them all.
        paths = find_reports([experiment_dir])
        return [(p.parent.name, p) for p in paths]

    resolved: list[tuple[str, Path | None]] = []
    for i, src in enumerate(sources or []):
        p = Path(src)
        report = p if p.is_file() else (find_reports([p], newest_only=True) or [None])[0]
        label = (labels[i] if labels and i < len(labels) else None) or (
            report.parent.name if report else src
        )
        resolved.append((label, report))
    return resolved


async def compare_reports(
    ctx: ToolContext,
    *,
    sources: list[str] | None = None,
    experiment_dir: str | None = None,
    labels: list[str] | None = None,
    baseline_index: int = 0,
) -> dict[str, Any]:
    if not sources and not experiment_dir:
        return {"compared": False, "reason": "provide either `sources` (2+) or `experiment_dir`"}

    entries = _resolve(ctx, sources, experiment_dir, labels)
    schema_path = ctx.settings.benchmark_report_schema_path

    reports: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    valid_entries: list[dict[str, Any]] = []
    valid_orig: list[int] = []  # the input index each valid entry came from

    for orig_i, (label, path) in enumerate(entries):
        if path is None:
            skipped.append({"label": label, "reason": "no benchmark report found"})
            continue
        report = load_report(path)
        validation = validate_report(report, schema_path)
        summary = summarize_report(report)
        reports.append({
            "label": label,
            "report_path": str(path),
            "valid": validation.valid,
            "model": summary.get("model"),
            "run_uid": summary.get("run_uid"),
        })
        if validation.valid:
            valid_entries.append({"label": label, "summary": summary})
            valid_orig.append(orig_i)
        else:
            skipped.append({"label": label, "reason": "report failed schema validation",
                            "errors": validation.errors[:5]})

    if len(valid_entries) < 2:
        return {
            "compared": False,
            "reason": "need at least two valid reports to compare",
            "reports": reports,
            "skipped": skipped,
        }

    # `baseline_index` indexes the inputs the caller passed; map it onto the surviving
    # valid set so a skipped (missing/invalid) report before it doesn't silently shift
    # the baseline to a different run. If the requested baseline was itself skipped, fall
    # back to the first valid run.
    try:
        base = valid_orig.index(baseline_index)
    except ValueError:
        base = 0
    comparison = compare_summaries(valid_entries, baseline_index=base)
    return {
        "compared": True,
        "n": len(valid_entries),
        "baseline": comparison["baseline"],
        "comparison": comparison,
        "reports": reports,
        "skipped": skipped,
    }
