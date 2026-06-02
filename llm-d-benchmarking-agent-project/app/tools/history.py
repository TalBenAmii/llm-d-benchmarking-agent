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
) -> dict[str, Any]:
    store = ctx.history_store()

    if action == "store":
        return await _store(
            ctx, source=source, label=label, tags=tags, spec=spec, harness=harness,
            workload=workload, namespace=namespace, session_id=session_id,
        )

    if action == "list":
        records = store.list(tag=filter_tag, model=filter_model)
        return {
            "action": "list",
            "n": len(records),
            "records": [_record_view(r) for r in records],
            "filters": {"tag": filter_tag, "model": filter_model},
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
        result = trend(records, metric)
        result["action"] = "trend"
        result["filters"] = {"tag": filter_tag, "model": filter_model}
        return result

    if action == "delete":
        if not record_id:
            return {"deleted": False, "reason": "delete requires `record_id`"}
        return {"deleted": store.delete(record_id), "record_id": record_id}

    return {
        "error": f"unknown action {action!r}",
        "valid_actions": ["store", "list", "get", "trend", "delete"],
    }
