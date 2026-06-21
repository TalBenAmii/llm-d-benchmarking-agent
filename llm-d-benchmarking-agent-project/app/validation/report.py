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

from app.dig import dict_or_empty, dig_dotted

# The §3.4-standard-metric + multi-turn session-performance extraction cluster lives in a
# sibling module (report_metrics.py) to keep this file's load/validate/summarize/compare
# concerns separate from the catalog-driven metric pull. We import the cluster back and
# RE-EXPORT it so the established ``from app.validation.report import extract_standard_metrics``
# / ``extract_session_performance`` import sites keep working. The stat-ladder primitives
# ``_stat`` / ``_PCTL_KEYS`` are OWNED by that module (its extractors and this file's
# ``summarize_report`` share them); we import them back here. The import direction is one-way
# (report -> report_metrics) so there is NO cycle. See ``_PERCENTILE_LADDER`` below for the
# percentile-ladder SSOT that stays here.
from app.validation.report_metrics import (
    _PCTL_KEYS,
    _STANDARD_METRICS_CATALOG,  # noqa: F401  (re-export)
    _extract_standard_metric,  # noqa: F401  (re-export for callers/tests)
    _load_catalog_section,  # noqa: F401  (re-export)
    _native_stat,  # noqa: F401  (re-export)
    _stat,
    extract_session_performance,
    extract_standard_metrics,
)


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


# The canonical percentile ladder carried by a BR v0.2 Statistics object, low->high. This is
# the SINGLE SOURCE OF TRUTH for the ladder: ``analysis.py`` imports ``_PERCENTILE_LADDER``
# from here (do NOT re-declare it there — keeping two copies in sync is the documented
# silent-floor drift bug). Each entry pairs a percentile name with the fraction-of-requests
# at-or-below it, which the analyzer uses for goodput interpolation.
#
# We keep the ENTIRE ladder (not just the round-number rungs) so the analyzer's goodput
# interpolation has the resolution it needs: a sub-p50 latency target must land between the
# real low percentiles (p0p1..p25), not get floored to 0% because everything below p50 was
# dropped.
_PERCENTILE_LADDER: tuple[tuple[str, float], ...] = (
    ("p0p1", 0.001), ("p1", 0.01), ("p5", 0.05), ("p10", 0.10), ("p25", 0.25),
    ("p50", 0.50), ("p75", 0.75), ("p90", 0.90), ("p95", 0.95),
    ("p99", 0.99), ("p99p9", 0.999),
)

# Aggregate-statistics keys we read off a Statistics object: ``mean`` (kept first, for
# throughput floors and headline comparison) followed by the full percentile ladder above
# (which feeds SLO verdicts, incl. p99p9, and goodput estimation). The tuple itself is OWNED
# by report_metrics.py (imported above) because both that module's extractors and this file's
# ``summarize_report`` consume it, and report_metrics may not import the ladder back from here
# (that would cycle). We keep the SSOT ladder here and ASSERT the imported key tuple is exactly
# ``mean`` + the ladder names, so the projection can never silently drift from the SSOT — the
# documented sub-p50 silent-floor bug if a low rung were ever dropped on one side.
assert ("mean",) + tuple(name for name, _frac in _PERCENTILE_LADDER) == _PCTL_KEYS, (
    "report_metrics._PCTL_KEYS drifted from report._PERCENTILE_LADDER (SSOT)"
)


def load_report(path: str | Path) -> dict[str, Any]:
    """Load a report from a .json or .yaml file.

    A present-but-corrupt report (e.g. truncated because a benchmark run was OOM-killed mid-write)
    must surface as a typed ``ReportError``, NOT a raw ``json.JSONDecodeError``/``yaml.YAMLError``/
    ``OSError``. Every caller already treats ``ReportError`` as "this report is unusable" — the
    multi-report tools (``compare_reports``/``analyze_results``) skip it into their ``skipped``
    channel; the rest surface a clean, actionable message — whereas a raw parse exception escapes
    the tool as an opaque ``tool '...' raised: ...`` string that names no file. (The earlier
    corrupt-report hardening covered ``summarize_report``; the parse step runs before it.)
    """
    p = Path(path)
    try:
        text = p.read_text()
        if p.suffix in (".yaml", ".yml"):
            return yaml.load(text, Loader=_StrTimestampLoader)
        return json.loads(text)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        # json.JSONDecodeError is a ValueError subclass; OSError covers read failures.
        raise ReportError(f"could not read benchmark report at {p}: {exc}") from exc


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


def summarize_report(report: dict[str, Any]) -> dict[str, Any]:
    """Compute a compact, non-expert-friendly summary from a validated report.

    Defensive: harnesses populate different subsets of fields, so every lookup is
    optional and missing pieces are simply omitted.
    """
    # Defensive: a present-but-non-dict child (e.g. a malformed/partial report summarized BEFORE
    # schema validation — compare_reports / compare_harness_runs summarize ahead of the validity
    # check) must degrade to {} at EVERY nesting level, never crash with AttributeError. `_d`
    # (dig.dict_or_empty) coerces any non-dict to {} so each subsequent `.get` is on a guaranteed mapping.
    _d = dict_or_empty
    report = _d(report)
    run = _d(report.get("run"))
    scenario = _d(report.get("scenario"))
    results = _d(report.get("results"))
    agg = _d(_d(results.get("request_performance")).get("aggregate"))

    # Model name (first stack component that declares one).
    model = None
    stack = scenario.get("stack")
    for comp in (stack if isinstance(stack, list) else []):
        name = _d(_d(_d(comp).get("standardized")).get("model")).get("name")
        if name:
            model = name
            break

    # Which workload generator (harness) produced this report, read straight from the
    # report's own scenario.load.standardized.tool — e.g. "inference-perf" or "guidellm".
    # This is the authoritative provenance used to group/contrast a multi-harness session.
    load_std = _d(_d(scenario.get("load")).get("standardized"))
    harness = load_std.get("tool")
    load_rate_qps = load_std.get("rate_qps")
    load_concurrency = load_std.get("concurrency")

    requests = _d(agg.get("requests"))
    total = requests.get("total")
    failures = requests.get("failures")
    success_rate = None
    if isinstance(total, (int, float)) and total and isinstance(failures, (int, float)):
        success_rate = round(100.0 * (total - failures) / total, 2)

    latency = _d(agg.get("latency"))
    throughput = _d(agg.get("throughput"))

    summary: dict[str, Any] = {
        "model": model,
        "harness": harness,
        "load": {k: v for k, v in (("rate_qps", load_rate_qps), ("concurrency", load_concurrency)) if v is not None} or None,
        "run_uid": run.get("uid"),
        "duration": _d(run.get("time")).get("duration"),
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

    # §3.4 standard resource/serving metrics (KV-cache hit rate, schedule delay, GPU util).
    # Present only when the harness emitted them; gracefully omitted (None-equivalent) otherwise.
    standard = extract_standard_metrics(report)
    summary["standard_metrics"] = standard or None

    # Session-level metrics — present only for multi-turn inference-perf workloads;
    # None for single-turn / non-session runs (never fabricated). See report.py's
    # extract_session_performance + knowledge/standard_metrics.yaml (session_performance).
    summary["session_performance"] = extract_session_performance(report)
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


def resolve_report_inputs(
    sources: list[str] | None,
    experiment_dir: str | None,
    labels: list[str] | None,
) -> list[tuple[str, Path | None]]:
    """Resolve user-supplied report inputs to a list of ``(label, report_path|None)``.

    Accepts the shapes the analyze/compare tools share: a DoE ``experiment_dir`` (one report
    per run treatment) takes precedence; otherwise each entry in ``sources`` is a report file
    or a directory searched for the newest report, paired with an explicit ``labels[i]`` (or a
    derived fallback). Pure report-discovery mechanism — no judgment.
    """
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


# Comparable metrics: (dotted path into a summary, human name, direction, canonical unit).
# "lower"/"higher" = which way is better; used to pick the winning run per metric. The
# canonical unit is what every run's value is normalized to BEFORE the delta/winner math,
# so a run that reported a metric in `s` and another that reported it in `ms` (both are
# schema-valid Units) are compared on the same scale — otherwise 0.5s would read as
# "smaller" (= the winner) than 200ms purely because 0.5 < 200 numerically.
_COMPARE_METRICS: tuple[tuple[str, str, str, str], ...] = (
    ("latency.ttft", "time to first token", "lower", "ms"),
    ("latency.tpot", "time per output token", "lower", "ms"),
    ("latency.itl", "inter-token latency", "lower", "ms"),
    ("latency.request_latency", "end-to-end request latency", "lower", "ms"),
    ("throughput.output_token_rate", "output token throughput", "higher", "tokens/s"),
    ("throughput.total_token_rate", "total token throughput", "higher", "tokens/s"),
    ("throughput.request_rate", "request throughput", "higher", "queries/s"),
)
_COMPARE_SCALARS: tuple[tuple[str, str, str], ...] = (
    ("success_rate_pct", "success rate", "higher"),
    ("requests_total", "total requests", "none"),
)
_STAT_PREFERENCE = ("mean", "p50", "p90", "p95", "p99")

# Per canonical unit, the multipliers that bring a reported (schema-valid) unit onto it.
# A reported unit absent from its canonical family's table is left UNCONVERTED but flagged,
# so we never silently apply a wrong factor; an unknown/missing unit is treated as already
# canonical (the BR Units enum is closed, so this only bites a non-conforming report).
_CANONICAL_CONVERSIONS: dict[str, dict[str, float]] = {
    "ms": {
        "ms": 1.0, "s": 1000.0, "us": 0.001,
        "ms/token": 1.0, "s/token": 1000.0,
    },
    "tokens/s": {
        "tokens/s": 1.0, "tokens/sec": 1.0, "tok/s": 1.0, "tps": 1.0,
        "tokens/min": 1.0 / 60.0,
    },
    "queries/s": {
        "queries/s": 1.0, "queries/sec": 1.0, "req/s": 1.0, "requests/s": 1.0, "qps": 1.0,
    },
}


def _to_canonical(value: float, units: Any, canonical: str) -> tuple[float, bool]:
    """Convert ``value`` (reported in ``units``) onto ``canonical``.

    Returns ``(converted_value, converted)``. ``converted`` is False when the reported
    unit is missing or not in the canonical family (the value is passed through unchanged,
    so a non-conforming report degrades to its raw number rather than a wrong scale)."""
    if units is None:
        return float(value), False
    mult = _CANONICAL_CONVERSIONS.get(canonical, {}).get(str(units).strip().lower())
    if mult is None:
        return float(value), False
    return float(value) * mult, True


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

    for dotted, name, direction, canonical in _COMPARE_METRICS:
        values: list[float | None] = []
        stat_used: str | None = None
        any_converted = False
        for s in summaries:
            v, stat, u = _stat_value(dig_dotted(s, dotted))
            if v is None:
                values.append(None)
                continue
            # Normalize every run onto the metric's canonical unit BEFORE comparing, so a
            # run reporting `s` and one reporting `ms` (both schema-valid) don't crown the
            # wrong winner / produce a nonsense delta by comparing 0.5 against 200.
            conv, converted = _to_canonical(v, u, canonical)
            any_converted = any_converted or converted
            values.append(conv)
            stat_used = stat_used or stat
        if sum(v is not None for v in values) < 2:
            continue  # nothing to compare for this metric
        # Report the canonical unit when we actually normalized at least one value; if no
        # run carried a recognized unit, fall back to None (units unknown) rather than
        # asserting a canonical unit we couldn't verify.
        units = canonical if any_converted else None
        rows.append(_build_metric_row(dotted, name, direction, stat_used, units, labels, values, baseline_index))

    for dotted, name, direction in _COMPARE_SCALARS:
        raw = [dig_dotted(s, dotted) for s in summaries]
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


# ---- cross-harness comparison (Phase 10: multi-harness in one session) -----
#
# ``compare_summaries`` answers "config A vs config B on the SAME harness". This answers
# a different question: a session that ran TWO harnesses (e.g. inference-perf for SLO /
# latency validation and guidellm for a throughput sweep) and wants them contrasted by
# harness. It is still pure mechanism over already-validated report summaries — which
# harness is "for" which job, and how to reconcile their differing methodologies, is the
# agent's judgment (knowledge/multi_harness.md). Here we only:
#   * group the runs by the harness that produced each (summary["harness"]),
#   * surface each harness's headline metrics + which objective fields it actually carries,
#   * note which metrics are measured by MORE THAN ONE harness (so the agent can cross-
#     validate them) vs by only one (so it must report them from that harness alone),
#   * for a metric two harnesses both measured on the same model, expose the per-harness
#     values side by side WITHOUT picking a "winner" (different load generators are not
#     directly comparable — see the knowledge file).

# Headline objective fields, by family, used to characterise what each harness measured.
_HARNESS_LATENCY = ("latency.ttft", "latency.tpot", "latency.itl", "latency.request_latency")
_HARNESS_THROUGHPUT = ("throughput.output_token_rate", "throughput.total_token_rate", "throughput.request_rate")


def _present_metrics(summary: dict[str, Any], paths: tuple[str, ...]) -> list[str]:
    out = []
    for p in paths:
        val, _, _ = _stat_value(dig_dotted(summary, p))
        if val is not None:
            out.append(p)
    return out


def compare_across_harnesses(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Contrast benchmark reports produced by DIFFERENT harnesses in one session.

    ``entries`` is ``[{"label": str, "summary": <summarize_report output>}, ...]``. Each
    summary carries the harness that produced it (``summary["harness"]``). Returns:
      * ``harnesses``: per detected harness, the runs it produced and the metric families
        it measured (latency fields / throughput fields),
      * ``shared_metrics`` / ``unique_metrics``: which objective fields ≥2 harnesses both
        measured vs only one did,
      * ``cross_metrics``: for each shared metric, the per-harness value (representative
        stat + units) laid side by side — facts only, no winner (cross-harness numbers
        aren't directly comparable).
      * ``models``: distinct models across the runs (a cross-harness contrast is only
        meaningful when the SAME model/stack was benchmarked by both harnesses).
    Raises ``ReportError`` unless at least two DISTINCT harnesses are present.
    """
    labels = [e.get("label") or f"run{i + 1}" for i, e in enumerate(entries)]
    summaries = [e.get("summary") or {} for e in entries]

    # Group runs by the harness the report names. Runs whose report doesn't declare a
    # harness are grouped under "unknown" so they're visible, never silently dropped.
    by_harness: dict[str, list[dict[str, Any]]] = {}
    for label, s in zip(labels, summaries, strict=True):
        h = s.get("harness") or "unknown"
        by_harness.setdefault(h, []).append({"label": label, "summary": s})

    distinct = [h for h in by_harness if h != "unknown"]
    if len(distinct) < 2:
        raise ReportError(
            "need reports from at least two DIFFERENT harnesses to contrast "
            f"(saw: {sorted(by_harness)})"
        )

    # Per-harness view: its runs, and which objective fields it measured (union over its runs).
    harness_view: dict[str, dict[str, Any]] = {}
    measured_by: dict[str, set[str]] = {}  # metric path -> set of harnesses that measured it
    for h, runs in by_harness.items():
        lat: set[str] = set()
        thr: set[str] = set()
        for r in runs:
            lat.update(_present_metrics(r["summary"], _HARNESS_LATENCY))
            thr.update(_present_metrics(r["summary"], _HARNESS_THROUGHPUT))
        # The "unknown" pseudo-group (reports whose harness couldn't be read) stays VISIBLE in
        # harness_view, but it must NOT count toward the cross-harness metric intersection: a
        # metric measured by one REAL harness + one unknown-harness report is unique to that real
        # harness, not "shared"/cross-validated by two harnesses. Counting it would tell the agent
        # ttft was cross-validated when only inference-perf measured it.
        if h != "unknown":
            for m in lat | thr:
                measured_by.setdefault(m, set()).add(h)
        harness_view[h] = {
            "runs": [
                {"label": r["label"], "model": r["summary"].get("model"),
                 "load": r["summary"].get("load"), "run_uid": r["summary"].get("run_uid")}
                for r in runs
            ],
            "latency_metrics": sorted(lat),
            "throughput_metrics": sorted(thr),
        }

    shared = sorted(m for m, hs in measured_by.items() if len(hs) >= 2)
    unique = {m: sorted(hs)[0] for m, hs in measured_by.items() if len(hs) == 1}

    # For each shared metric, the per-harness representative value (no winner picked).
    cross_metrics: list[dict[str, Any]] = []
    for m in shared:
        name = next((nm for k, nm, *_ in _COMPARE_METRICS if k == m), m)
        per_harness = []
        for h in sorted(measured_by[m]):
            # representative = first run of that harness that carries the metric.
            for r in by_harness[h]:
                val, stat, units = _stat_value(dig_dotted(r["summary"], m))
                if val is not None:
                    per_harness.append({"harness": h, "label": r["label"], "value": val,
                                        "stat": stat, "units": units})
                    break
        cross_metrics.append({"key": m, "name": name, "per_harness": per_harness})

    models: list[str] = sorted({str(s["model"]) for s in summaries if s.get("model")})
    return {
        "n": len(entries),
        "harnesses": harness_view,
        "harness_names": sorted(by_harness),
        "models": models,
        "same_model": len(models) <= 1,
        "shared_metrics": shared,
        "unique_metrics": unique,
        "cross_metrics": cross_metrics,
        "headline": _cross_harness_headline(harness_view, shared, unique, models),
    }


def _cross_harness_headline(
    harness_view: dict[str, dict[str, Any]],
    shared: list[str],
    unique: dict[str, str],
    models: list[str],
) -> str:
    real = [h for h in harness_view if h != "unknown"]
    parts = [f"Ran {len(real)} harnesses: {', '.join(sorted(real))}."]
    if len(models) > 1:
        parts.append(
            f"WARNING: {len(models)} different models across the runs — a cross-harness "
            "contrast is only meaningful on the same model/stack."
        )
    parts.append(
        f"{len(shared)} metric(s) measured by both; {len(unique)} measured by only one."
        if shared or unique else "No overlapping objective metrics."
    )
    return " ".join(parts)
