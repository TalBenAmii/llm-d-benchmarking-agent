"""Benchmark Report v0.2 validation + plain-language summary.

The schema is the repo's own authoritative artifact, loaded at runtime from
``llm-d-benchmark/.../br_v0_2_json_schema.json`` (never vendored). Results shown to
the user are computed from the *validated* report object — never scraped from logs —
which is determinism gate (d).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema
import yaml


class ReportError(RuntimeError):
    pass


# PyYAML turns ISO-8601 timestamps into datetime objects, which then fail JSON-Schema
# `type: string` checks. We keep them as strings so reports validate faithfully.
class _StrTimestampLoader(yaml.SafeLoader):
    pass


_StrTimestampLoader.yaml_implicit_resolvers = {
    ch: [(tag, rx) for (tag, rx) in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for ch, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


@dataclass
class ReportValidation:
    valid: bool
    schema_version: str | None
    errors: list[str] = field(default_factory=list)        # fatal/structural
    deviations: list[str] = field(default_factory=list)    # non-fatal (schema lags repo)


# The full aggregate-statistics ladder carried by a BR v0.2 Statistics object. We keep
# the ENTIRE percentile ladder (not just the round-number ones) so the analyzer's goodput
# interpolation has the resolution it needs: a sub-p50 latency target must land between the
# real low percentiles (p0p1..p25), not get floored to 0% because everything below p50 was
# dropped. ``mean`` is kept for throughput floors and headline comparison; the percentiles
# feed SLO verdicts (any percentile, incl. p99p9) and goodput estimation.
_PCTL_KEYS = (
    "mean",
    "p0p1", "p1", "p5", "p10", "p25", "p50", "p75", "p90", "p95", "p99", "p99p9",
)


def load_report(path: str | Path) -> dict[str, Any]:
    """Load a report from a .json or .yaml file."""
    p = Path(path)
    text = p.read_text()
    if p.suffix in (".yaml", ".yml"):
        return yaml.load(text, Loader=_StrTimestampLoader)
    return json.loads(text)


def validate_report(report: dict[str, Any], schema_path: str | Path) -> ReportValidation:
    """Validate a parsed report against the repo's BR v0.2 JSON Schema."""
    schema_path = Path(schema_path)
    if not schema_path.exists():
        raise ReportError(
            f"Benchmark Report schema not found at {schema_path}. The llm-d-benchmark "
            f"repo may be missing or moved; cannot validate results."
        )
    schema = json.loads(schema_path.read_text())
    # The repo schema declares no $schema; honor it if present, else fall back to
    # Draft 7 (handles array-form `items` tuple validation correctly).
    validator_cls = jsonschema.validators.validator_for(schema, default=jsonschema.Draft7Validator)
    validator = validator_cls(schema)

    fatal: list[str] = []
    deviations: list[str] = []
    for e in sorted(validator.iter_errors(report), key=lambda e: list(e.path)):
        loc = "/".join(str(p) for p in e.path) or "<root>"
        msg = f"{loc}: {e.message}"
        # The committed JSON Schema is generated from pydantic (extra="forbid") and can
        # lag the live models, so "additionalProperties" just means the report is newer
        # than the schema — record it as a non-fatal deviation, not a hard failure.
        if e.validator == "additionalProperties":
            deviations.append(msg)
        else:
            fatal.append(msg)

    return ReportValidation(
        valid=not fatal,
        schema_version=str(report.get("version")) if isinstance(report, dict) else None,
        errors=fatal[:50],
        deviations=deviations[:50],
    )


def _stat(metric: Any) -> dict[str, Any] | None:
    """Extract {units, mean, full percentile ladder} from a metric object, if present.

    Carries the whole ladder (``_PCTL_KEYS``) so downstream SLO evaluation and goodput
    interpolation see every reported percentile, not a lossy subset.
    """
    if not isinstance(metric, dict):
        return None
    out: dict[str, Any] = {}
    if "units" in metric:
        out["units"] = metric["units"]
    for k in _PCTL_KEYS:
        if k in metric:
            out[k] = metric[k]
    return out or None


def summarize_report(report: dict[str, Any]) -> dict[str, Any]:
    """Compute a compact, non-expert-friendly summary from a validated report.

    Defensive: harnesses populate different subsets of fields, so every lookup is
    optional and missing pieces are simply omitted.
    """
    run = report.get("run", {}) if isinstance(report, dict) else {}
    scenario = report.get("scenario", {}) if isinstance(report, dict) else {}
    results = report.get("results", {}) if isinstance(report, dict) else {}
    agg = (
        results.get("request_performance", {}).get("aggregate", {})
        if isinstance(results, dict)
        else {}
    )

    # Model name (first stack component that declares one).
    model = None
    for comp in (scenario.get("stack") or []):
        name = comp.get("standardized", {}).get("model", {}).get("name") if isinstance(comp, dict) else None
        if name:
            model = name
            break

    requests = agg.get("requests", {}) if isinstance(agg, dict) else {}
    total = requests.get("total")
    failures = requests.get("failures")
    success_rate = None
    if isinstance(total, (int, float)) and total and isinstance(failures, (int, float)):
        success_rate = round(100.0 * (total - failures) / total, 2)

    latency = agg.get("latency", {}) if isinstance(agg, dict) else {}
    throughput = agg.get("throughput", {}) if isinstance(agg, dict) else {}

    summary: dict[str, Any] = {
        "model": model,
        "run_uid": run.get("uid"),
        "duration": run.get("time", {}).get("duration"),
        "requests_total": total,
        "requests_failures": failures,
        "success_rate_pct": success_rate,
        "latency": {
            "ttft": _stat(latency.get("time_to_first_token")),
            "tpot": _stat(latency.get("time_per_output_token")),
            "itl": _stat(latency.get("inter_token_latency")),
            "request_latency": _stat(latency.get("request_latency")),
        },
        "throughput": {
            "total_token_rate": _stat(throughput.get("total_token_rate")),
            "output_token_rate": _stat(throughput.get("output_token_rate")),
            "request_rate": _stat(throughput.get("request_rate")),
        },
    }
    # Prune empty latency/throughput entries for a cleaner payload.
    summary["latency"] = {k: v for k, v in summary["latency"].items() if v}
    summary["throughput"] = {k: v for k, v in summary["throughput"].items() if v}
    return summary


# ---- multi-report discovery + comparison (sweeps / A-B) -------------------

_REPORT_GLOBS = (
    "**/benchmark_report_v0.2*.yaml",
    "**/benchmark_report_v0.2*.yml",
    "**/benchmark_report_v0.2*.json",
)


def find_reports(roots: list[str | Path], *, newest_only: bool = False) -> list[Path]:
    """Locate Benchmark Report v0.2 files under the given roots (each a file or dir).

    Returns paths sorted oldest→newest by mtime (stable run order for a sweep). With
    ``newest_only`` returns just the most recent (the common "one report per run dir" case).
    """
    candidates: list[Path] = []
    for root in roots:
        p = Path(root)
        if not p.exists():
            continue
        if p.is_file():
            candidates.append(p)
            continue
        for pat in _REPORT_GLOBS:
            candidates.extend(p.glob(pat))
    uniq = sorted(set(candidates), key=lambda c: c.stat().st_mtime)
    if not uniq:
        return []
    return [uniq[-1]] if newest_only else uniq


# Comparable metrics: (dotted path into a summary, human name, direction).
# "lower"/"higher" = which way is better; used to pick the winning run per metric.
_COMPARE_METRICS: tuple[tuple[str, str, str], ...] = (
    ("latency.ttft", "time to first token", "lower"),
    ("latency.tpot", "time per output token", "lower"),
    ("latency.itl", "inter-token latency", "lower"),
    ("latency.request_latency", "end-to-end request latency", "lower"),
    ("throughput.output_token_rate", "output token throughput", "higher"),
    ("throughput.total_token_rate", "total token throughput", "higher"),
    ("throughput.request_rate", "request throughput", "higher"),
)
_COMPARE_SCALARS: tuple[tuple[str, str, str], ...] = (
    ("success_rate_pct", "success rate", "higher"),
    ("requests_total", "total requests", "none"),
)
_STAT_PREFERENCE = ("mean", "p50", "p90", "p95", "p99")


def _dig(summary: dict[str, Any], dotted: str) -> Any:
    cur: Any = summary
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _stat_value(metric_obj: Any) -> tuple[float | None, str | None, Any]:
    """From a {units, mean, p50, ...} object, pick a representative stat (prefer mean)."""
    if not isinstance(metric_obj, dict):
        return None, None, None
    for s in _STAT_PREFERENCE:
        v = metric_obj.get(s)
        if isinstance(v, (int, float)):
            return float(v), s, metric_obj.get("units")
    return None, None, None


def _build_metric_row(
    key: str, name: str, direction: str, stat: str | None, units: Any,
    labels: list[str], values: list[float | None], baseline_index: int,
) -> dict[str, Any]:
    base = values[baseline_index]
    per_run = []
    for i, v in enumerate(values):
        delta_abs = delta_pct = None
        if v is not None and isinstance(base, (int, float)):
            delta_abs = round(v - base, 6)
            delta_pct = round(100.0 * (v - base) / base, 2) if base else None
        per_run.append({"label": labels[i], "value": v, "delta_abs": delta_abs, "delta_pct": delta_pct})
    best = None
    present = [(labels[i], v) for i, v in enumerate(values) if v is not None]
    if present and direction in ("lower", "higher"):
        chooser = min if direction == "lower" else max
        lbl, val = chooser(present, key=lambda t: t[1])
        best = {"label": lbl, "value": val}
    return {
        "key": key, "name": name, "stat": stat, "units": units, "direction": direction,
        "baseline_value": base, "per_run": per_run, "best": best,
    }


def compare_summaries(entries: list[dict[str, Any]], *, baseline_index: int = 0) -> dict[str, Any]:
    """Compare N report summaries side by side, computing per-metric deltas vs a baseline.

    ``entries`` is a list of ``{"label": str, "summary": <summarize_report output>}``.
    Returns a structured comparison (labels, baseline, per-metric rows with deltas and the
    winning run) plus a short factual headline. Prose is left to the agent; this is the math.
    """
    if len(entries) < 2:
        raise ReportError("need at least two reports to compare")
    if not 0 <= baseline_index < len(entries):
        baseline_index = 0

    labels = [e.get("label") or f"run{i + 1}" for i, e in enumerate(entries)]
    summaries = [e.get("summary") or {} for e in entries]
    rows: list[dict[str, Any]] = []

    for dotted, name, direction in _COMPARE_METRICS:
        values: list[float | None] = []
        stat_used: str | None = None
        units: Any = None
        for s in summaries:
            v, stat, u = _stat_value(_dig(s, dotted))
            values.append(v)
            if v is not None:
                stat_used = stat_used or stat
                units = units if units is not None else u
        if sum(v is not None for v in values) < 2:
            continue  # nothing to compare for this metric
        rows.append(_build_metric_row(dotted, name, direction, stat_used, units, labels, values, baseline_index))

    for dotted, name, direction in _COMPARE_SCALARS:
        raw = [_dig(s, dotted) for s in summaries]
        values = [float(v) if isinstance(v, (int, float)) else None for v in raw]
        if sum(v is not None for v in values) < 2:
            continue
        rows.append(_build_metric_row(dotted, name, direction, "value", None, labels, values, baseline_index))

    return {
        "labels": labels,
        "baseline": labels[baseline_index],
        "entry_meta": [
            {"label": labels[i], "model": s.get("model"),
             "run_uid": s.get("run_uid"), "duration": s.get("duration")}
            for i, s in enumerate(summaries)
        ],
        "metrics": rows,
        "headline": _comparison_headline(rows, labels, baseline_index),
    }


def _comparison_headline(rows: list[dict[str, Any]], labels: list[str], baseline_index: int) -> str:
    wins = [
        f"best {r['name']}: {r['best']['label']} "
        f"({r['best']['value']}{(' ' + str(r['units'])) if r.get('units') else ''})"
        for r in rows
        if r.get("best") and r["direction"] in ("lower", "higher")
    ]
    head = f"Compared {len(labels)} runs (baseline: {labels[baseline_index]})."
    return f"{head} " + "; ".join(wins) + "." if wins else f"{head} No overlapping metrics to compare."
