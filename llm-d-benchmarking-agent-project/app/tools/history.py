"""result_history — persist validated Benchmark Reports across sessions and read trends.

Proposal stretch: "historical storage + trend visualization". This is the agent-facing
**mechanism**: a single tool that dispatches on ``action`` (store / list / get / trend /
delete) over the cross-session :class:`HistoryStore`. It is READ-ONLY except for ``store``
and ``delete``, neither of which touches the cluster or any repo — they only read a report
file the user already produced and persist its already-validated summary into the agent's
own workspace. So all actions auto-run (no approval gate): there's nothing destructive to a
deployment here. The interpretation of a trend (regression? acceptable drift?) is the
agent's, grounded in ``knowledge/history.md`` (thin code, thick agent).
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.storage.history import HistoryRecord, available_metrics, trend
from app.tools.context import ToolContext
from app.validation.report import (
    find_reports,
    load_report,
    summarize_report,
    validate_report,
)

# Filters the LIST/TREND actions support server-side. Advertised in every list/trend result
# so the agent can surface a limitation IMMEDIATELY (e.g. "no harness filter") instead of
# discovering it a turn later. start_date/end_date filter on each record's stored_at (when it
# was persisted) — the only timestamp every record carries (real-1 11:30).
_SUPPORTED_LIST_FILTERS = ["tag", "model", "start_date", "end_date"]


def _parse_bound(raw: str, *, end_of_day: bool) -> float:
    """Parse a user date/datetime bound to an epoch-seconds float (UTC).

    Accepts an ISO-8601 date ('2026-05-01') or datetime. A bare date becomes 00:00:00 (start
    bound) or 23:59:59.999999 (end bound) so an end_date day is inclusive. A naive datetime is
    assumed UTC. Raises ValueError on an unparseable string (the caller turns it into a clean,
    self-correctable error)."""
    s = raw.strip()
    dt = datetime.fromisoformat(s)
    # A bare date parses to midnight with no time component in the original string.
    if "T" not in s and " " not in s and end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def _filter_by_date(
    records: list[HistoryRecord], start_date: str | None, end_date: str | None
) -> tuple[list[HistoryRecord], dict[str, Any]]:
    """Filter records to those whose stored_at falls within [start_date, end_date] (inclusive).

    Returns ``(filtered, applied)`` where ``applied`` echoes the resolved bounds (or an
    ``error`` for an unparseable date — the caller short-circuits and the agent self-corrects).
    No-op (returns the input) when neither bound is set."""
    applied: dict[str, Any] = {}
    if start_date is None and end_date is None:
        return records, applied
    lo = hi = None
    try:
        if start_date is not None:
            lo = _parse_bound(start_date, end_of_day=False)
            applied["start_date"] = start_date
        if end_date is not None:
            hi = _parse_bound(end_date, end_of_day=True)
            applied["end_date"] = end_date
    except ValueError as exc:
        applied["error"] = f"could not parse date bound: {exc}"
        return records, applied
    out = [
        r for r in records
        if (lo is None or r.stored_at >= lo) and (hi is None or r.stored_at <= hi)
    ]
    return out, applied


def _record_view(rec: HistoryRecord) -> dict[str, Any]:
    """A compact, list-friendly view of a record (no full summary body)."""
    return {
        "id": rec.id,
        "stored_at": rec.stored_at,
        "label": rec.label,
        "tags": rec.tags,
        "model": rec.model,
        "run_uid": rec.run_uid,
        "spec": rec.spec,
        "harness": rec.harness,
        "workload": rec.workload,
        "namespace": rec.namespace,
        "report_path": rec.report_path,
        "session_id": rec.session_id,
    }


async def _store(
    ctx: ToolContext,
    *,
    source: str | None,
    label: str | None,
    tags: list[str] | None,
    spec: str | None,
    harness: str | None,
    workload: str | None,
    namespace: str | None,
    session_id: str | None,
) -> dict[str, Any]:
    if not source:
        return {"stored": False, "reason": "store requires `source` (a report file or run dir)"}
    p = Path(source)
    report_path: Path | None
    if p.is_file():
        report_path = p
    else:
        found = find_reports([p], newest_only=True)
        report_path = found[0] if found else None
    if report_path is None:
        return {"stored": False, "reason": f"no Benchmark Report found under {source!r}"}

    report = load_report(report_path)
    validation = validate_report(report, ctx.settings.benchmark_report_schema_path)
    if not validation.valid:
        # Never persist an unvalidated report (determinism gate d).
        return {
            "stored": False,
            "reason": "report failed schema validation — not stored",
            "report_path": str(report_path),
            "errors": validation.errors[:5],
        }

    summary = summarize_report(report)
    record, created = ctx.history_store().add(
        summary,
        label=label,
        tags=tags,
        session_id=session_id,
        report_path=str(report_path),
        spec=spec,
        harness=harness,
        workload=workload,
        namespace=namespace,
    )
    return {
        "stored": True,
        "created": created,  # False == this exact report was already in history (idempotent)
        "record": _record_view(record),
        "summary": record.summary,
    }


async def result_history(
    ctx: ToolContext,
    *,
    action: str,
    source: str | None = None,
    label: str | None = None,
    tags: list[str] | None = None,
    spec: str | None = None,
    harness: str | None = None,
    workload: str | None = None,
    namespace: str | None = None,
    session_id: str | None = None,
    record_id: str | None = None,
    metric: str | None = None,
    filter_tag: str | None = None,
    filter_model: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    store = ctx.history_store()

    if action == "store":
        return await _store(
            ctx, source=source, label=label, tags=tags, spec=spec, harness=harness,
            workload=workload, namespace=namespace, session_id=session_id,
        )

    if action == "list":
        records = store.list(tag=filter_tag, model=filter_model)
        records, date_applied = _filter_by_date(records, start_date, end_date)
        return {
            "action": "list",
            "n": len(records),
            "records": [_record_view(r) for r in records],
            "filters": {
                "tag": filter_tag, "model": filter_model,
                "start_date": start_date, "end_date": end_date,
                "date_filter": date_applied or None,
            },
            "supported_filters": _SUPPORTED_LIST_FILTERS,
            "date_filter_basis": "stored_at (when the result was persisted to history)",
        }

    if action == "get":
        if not record_id:
            return {"found": False, "reason": "get requires `record_id`"}
        rec = store.get(record_id)
        if rec is None:
            return {"found": False, "reason": f"no stored record {record_id!r}"}
        return {"found": True, "record": _record_view(rec), "summary": rec.summary}

    if action == "trend":
        if not metric:
            return {"error": "trend requires `metric`", "available_metrics": available_metrics()}
        records = store.list(tag=filter_tag, model=filter_model)
        records, date_applied = _filter_by_date(records, start_date, end_date)
        result = trend(records, metric)
        result["action"] = "trend"
        result["filters"] = {
            "tag": filter_tag, "model": filter_model,
            "start_date": start_date, "end_date": end_date,
            "date_filter": date_applied or None,
        }
        result["supported_filters"] = _SUPPORTED_LIST_FILTERS
        result["date_filter_basis"] = "stored_at (when the result was persisted to history)"
        return result

    if action == "delete":
        if not record_id:
            return {"deleted": False, "reason": "delete requires `record_id`"}
        return {"deleted": store.delete(record_id), "record_id": record_id}

    return {
        "error": f"unknown action {action!r}",
        "valid_actions": ["store", "list", "get", "trend", "delete"],
    }
