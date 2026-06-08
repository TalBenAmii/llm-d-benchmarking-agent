"""Historical result storage — disk-backed persistence of *validated* Benchmark Report
summaries across sessions, plus pure trend math over the stored series.

Proposal stretch: "historical storage + trend visualization". This is the **mechanism**
only — append/list/get records and compute a metric's time-series. It contains NO
judgment about whether a change is a regression, what an acceptable drift is, or which
metric matters; that lives in ``knowledge/history.md`` and the agent's reasoning
(thin code, thick agent).

Storage model
-------------
Records live under ``<workspace>/history/`` as one JSON file per record
(``<record_id>.json``). The workspace root is shared by all sessions (sessions get
``<workspace>/sessions/<id>``), so the history persists across sessions and reloads.
A record is keyed by a content hash of the report's identity (run uid + report path +
the headline metric values), so storing the SAME report twice is idempotent rather than
duplicating it. Each record carries only the already-validated, log-free
``summarize_report`` summary plus a little provenance (label, tags, when it was stored) —
never raw logs, never an unvalidated report (determinism gate d holds upstream of here).
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Metrics we can build a trend over, mirroring the analyzer/comparator objective space.
# (dotted summary path, direction-better). "lower"/"higher" = which way is an improvement;
# used only to LABEL the trend factually, never to decide pass/fail.
#
# The last three are the §3.4 standard/serving metrics (KV-cache hit rate, GPU utilization,
# schedule-delay queue-depth proxy) surfaced for context. They live at the nested
# ``standard_metrics.<key>.value`` stat path that report.summarize_report fills, and are
# present ONLY when the run was done with monitoring on (Phase 27 / flags.monitoring) so
# results.observability was populated; on runs without it those points are simply absent
# (trend() skips records that lack the metric — see knowledge/results_interpretation.md and
# knowledge/observability.md for how to read them). Their direction here is the same
# informational label as the analyzer's Pareto objectives; they never affect dominance.
_TREND_METRICS: dict[str, tuple[str, str]] = {
    "ttft": ("latency.ttft", "lower"),
    "tpot": ("latency.tpot", "lower"),
    "itl": ("latency.itl", "lower"),
    "request_latency": ("latency.request_latency", "lower"),
    "output_token_rate": ("throughput.output_token_rate", "higher"),
    "total_token_rate": ("throughput.total_token_rate", "higher"),
    "request_rate": ("throughput.request_rate", "higher"),
    "success_rate_pct": ("success_rate_pct", "higher"),
    "kv_cache_hit_rate": ("standard_metrics.kv_cache_hit_rate.value", "higher"),
    "gpu_utilization": ("standard_metrics.gpu_utilization.value", "higher"),
    "schedule_delay": ("standard_metrics.schedule_delay.value", "lower"),
}
_STAT_PREFERENCE = ("mean", "p50", "p90", "p95", "p99")

# Filesystem-safe id (we build paths from it). Recompute on load; never trust an id from disk.
_ID_LEN = 16


@dataclass
class HistoryRecord:
    """One stored, validated benchmark result."""

    id: str
    stored_at: float
    label: str | None                     # human label, e.g. "concurrency=16 baseline"
    tags: list[str] = field(default_factory=list)
    session_id: str | None = None
    report_path: str | None = None        # where the report was read from (provenance)
    model: str | None = None
    run_uid: str | None = None
    spec: str | None = None
    harness: str | None = None
    workload: str | None = None
    namespace: str | None = None
    summary: dict[str, Any] = field(default_factory=dict)  # summarize_report() output
    # Reproducibility (provenance bundle): the id of the bundle capturing the EXACT inputs that
    # produced this result, plus a compact provenance dict (repo SHAs + dirty + regenerate
    # command). Both ADDITIVE + optional — old records (written before this field existed) still
    # load, because _read reconstructs only known fields and tolerates their absence.
    bundle_id: str | None = None
    provenance: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _dig(obj: Any, dotted: str) -> Any:
    cur: Any = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _representative_value(summary: dict[str, Any], dotted: str) -> tuple[float | None, str | None, Any]:
    """Pull a single representative number for a metric out of a summary.

    Scalars (e.g. success_rate_pct) come straight through; metric objects use the first
    available preferred statistic (mean -> p50 -> ...). Returns (value, stat, units)."""
    node = _dig(summary, dotted)
    if isinstance(node, (int, float)):
        return float(node), "value", None
    if isinstance(node, dict):
        for s in _STAT_PREFERENCE:
            v = node.get(s)
            if isinstance(v, (int, float)):
                return float(v), s, node.get("units")
    return None, None, None


def compute_record_id(summary: dict[str, Any], report_path: str | None) -> str:
    """Stable, content-addressed id so re-storing the same report is idempotent.

    Keyed by the report's run uid (if any), its source path, and the headline metric
    values — so two genuinely-different runs never collide, but the same run stored twice
    maps to one record."""
    run_uid = summary.get("run_uid")
    headline = {
        name: _representative_value(summary, path)[0]
        for name, (path, _dir) in _TREND_METRICS.items()
    }
    basis = json.dumps(
        {"run_uid": run_uid, "path": report_path, "headline": headline},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:_ID_LEN]


class HistoryStore:
    """Append/list/get validated result records under ``<root>/history``.

    All I/O is best-effort and defensive: a corrupt or partial record is skipped on read,
    never crashing the agent. The store never mutates a report or the cluster — it only
    persists the summary the analyzer already validated."""

    def __init__(self, root: Path):
        # ``root`` is the shared workspace root (settings.resolved_workspace_dir).
        self._dir = Path(root) / "history"

    @property
    def dir(self) -> Path:
        return self._dir

    def add(
        self,
        summary: dict[str, Any],
        *,
        label: str | None = None,
        tags: list[str] | None = None,
        session_id: str | None = None,
        report_path: str | None = None,
        spec: str | None = None,
        harness: str | None = None,
        workload: str | None = None,
        namespace: str | None = None,
        bundle_id: str | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> tuple[HistoryRecord, bool]:
        """Persist ``summary`` as a record. Returns ``(record, created)`` where
        ``created`` is False when an identical record already existed (idempotent).

        ``bundle_id``/``provenance`` (optional) attach a reproducibility provenance bundle's id
        + compact provenance dict to the record — additive, so callers that don't set them keep
        the prior behavior exactly."""
        rid = compute_record_id(summary, report_path)
        path = self._dir / f"{rid}.json"
        existing = self._read(path)
        if existing is not None:
            return existing, False
        record = HistoryRecord(
            id=rid,
            stored_at=time.time(),
            label=label,
            tags=list(tags or []),
            session_id=session_id,
            report_path=report_path,
            model=summary.get("model"),
            run_uid=summary.get("run_uid"),
            spec=spec,
            harness=harness,
            workload=workload,
            namespace=namespace,
            summary=summary,
            bundle_id=bundle_id,
            provenance=provenance,
        )
        self._dir.mkdir(parents=True, exist_ok=True)
        # Write atomically-ish: temp then replace, so a list() never sees a half file.
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record.to_json(), indent=2))
        tmp.replace(path)
        return record, True

    def get(self, record_id: str) -> HistoryRecord | None:
        if not _safe_id(record_id):
            return None
        return self._read(self._dir / f"{record_id}.json")

    def delete(self, record_id: str) -> bool:
        if not _safe_id(record_id):
            return False
        path = self._dir / f"{record_id}.json"
        if path.exists():
            path.unlink()
            return True
        return False

    def list(self, *, tag: str | None = None, model: str | None = None) -> list[HistoryRecord]:
        """All stored records, newest first, optionally filtered by tag and/or model."""
        out: list[HistoryRecord] = []
        if not self._dir.exists():
            return out
        for p in self._dir.glob("*.json"):
            rec = self._read(p)
            if rec is None:
                continue
            if tag is not None and tag not in rec.tags:
                continue
            if model is not None and rec.model != model:
                continue
            out.append(rec)
        out.sort(key=lambda r: r.stored_at, reverse=True)
        return out

    def _read(self, path: Path) -> HistoryRecord | None:
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or "summary" not in data:
            return None
        # Reconstruct only known fields; ignore anything extra a newer writer added.
        known: dict[str, Any] = {k: data.get(k) for k in HistoryRecord.__dataclass_fields__}
        known["id"] = path.stem  # never trust an id baked into the file; the path is truth
        try:
            return HistoryRecord(**known)
        except TypeError:
            return None


def _safe_id(rid: str | None) -> bool:
    return isinstance(rid, str) and rid.isalnum() and 0 < len(rid) <= 64


# ---- trend math over a stored series ---------------------------------------


def trend(
    records: list[HistoryRecord],
    metric: str,
) -> dict[str, Any]:
    """Build a chronological time-series of ONE metric across stored records.

    Returns the ordered points (oldest -> newest, by stored_at), the metric's
    better-direction (factual label only), and a factual first-vs-last delta. NO verdict
    about whether a change is good/bad/regression — the agent decides that with
    knowledge/history.md. ``metric`` must be one of ``available_metrics()``."""
    if metric not in _TREND_METRICS:
        return {
            "metric": metric,
            "error": f"unknown metric {metric!r}",
            "available_metrics": available_metrics(),
        }
    dotted, direction = _TREND_METRICS[metric]
    ordered = sorted(records, key=lambda r: r.stored_at)  # oldest -> newest

    points: list[dict[str, Any]] = []
    stat_used: str | None = None
    units: Any = None
    for rec in ordered:
        value, stat, u = _representative_value(rec.summary, dotted)
        if value is None:
            continue  # this run didn't report the metric — skip, don't fabricate
        stat_used = stat_used or stat
        units = units if units is not None else u
        points.append({
            "id": rec.id,
            "label": rec.label,
            "stored_at": rec.stored_at,
            "value": value,
            "model": rec.model,
            "run_uid": rec.run_uid,
            "tags": rec.tags,
        })

    delta_abs = delta_pct = None
    if len(points) >= 2:
        first, last = points[0]["value"], points[-1]["value"]
        delta_abs = round(last - first, 6)
        delta_pct = round(100.0 * (last - first) / first, 2) if first else None

    return {
        "metric": metric,
        "path": dotted,
        "better": direction,           # "lower" or "higher" — a label, not a judgment
        "stat": stat_used,
        "units": units,
        "n": len(points),
        "points": points,
        "first_to_last": {"delta_abs": delta_abs, "delta_pct": delta_pct},
    }


def available_metrics() -> list[str]:
    return list(_TREND_METRICS)
