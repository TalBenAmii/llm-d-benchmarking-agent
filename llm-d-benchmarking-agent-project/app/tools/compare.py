"""compare_reports — side-by-side comparison of multiple Benchmark Reports (A/B + sweeps).

Read-only. Locates each report, validates it against the repo's BR v0.2 schema (reusing
the same validation as ``locate_and_parse_report`` — never scrape logs), then computes
per-metric deltas vs a baseline. The comparison MATH lives in ``validation/report.py``
(pure + tested); this handler is just resolution + wiring. Interpreting the deltas for the
user is the agent's job (see knowledge/sweep_playbook.md).
"""
from __future__ import annotations

from typing import Any

from app.tools.context import ToolContext
from app.validation.report import (
    ReportError,
    compare_across_harnesses,
    compare_summaries,
    load_report,
    resolve_report_inputs,
    summarize_report,
    validate_report,
)


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

    entries = resolve_report_inputs(sources, experiment_dir, labels)
    schema_path = ctx.settings.benchmark_report_schema_path

    reports: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    valid_entries: list[dict[str, Any]] = []
    valid_orig: list[int] = []  # the input index each valid entry came from

    for orig_i, (label, path) in enumerate(entries):
        if path is None:
            skipped.append({"label": label, "reason": "no benchmark report found"})
            continue
        try:
            report = load_report(path)
        except ReportError as exc:
            # Present but corrupt/unreadable (e.g. truncated by an OOM-killed run) → skip this one
            # report and keep comparing the rest, instead of failing the whole comparison.
            skipped.append({"label": label, "reason": "report unreadable", "errors": [str(exc)]})
            continue
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


# ── compare_harness_runs (merged from app/tools/multiharness.py) ──────────────
# compare_harness_runs — cross-harness comparison for a multi-harness session (Phase 10).
#
# The proposal's stretch goal: in ONE session the agent recommends + runs both
# ``inference-perf`` (SLO / latency validation) and ``guidellm`` (throughput sweep), then
# compares them. ``compare_reports`` already contrasts configurations of the *same* harness;
# this tool contrasts reports produced by *different* harnesses.
#
# Read-only. It locates each report, validates it against the repo's BR v0.2 schema (reusing
# the same validation as ``compare_reports`` / ``locate_and_parse_report`` — never scrape
# logs), detects which harness produced each from the report's own
# ``scenario.load.standardized.tool`` field, then groups + contrasts them. The comparison
# MATH lives in ``validation/report.py`` (pure + tested); this handler is resolution + wiring.
# WHAT each harness is good for, and how to reconcile their differing methodologies, is the
# agent's judgment — see ``knowledge/multi_harness.md``.


async def compare_harness_runs(
    ctx: ToolContext,
    *,
    sources: list[str],
    labels: list[str] | None = None,
) -> dict[str, Any]:
    entries_in = resolve_report_inputs(sources, None, labels)
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
