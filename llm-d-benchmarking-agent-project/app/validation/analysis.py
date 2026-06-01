"""Results-analyzer math: SLO-aware filtering, goodput estimation, and Pareto/DoE
analysis over a sweep of Benchmark Reports.

This is the proposal's §3.4 "Results Analyzer" *mechanism* — pure, deterministic
functions over the already-validated report **summaries** produced by
``summarize_report`` (which themselves derive only from a schema-validated Benchmark
Report, never scraped from logs — determinism gate d). It contains NO judgment about
what an SLO *should* be or which config to recommend; that lives in
``knowledge/analysis.md`` and the agent's reasoning (thin code, thick agent).

Key honesty constraint baked into the math: Benchmark Report v0.2 carries only
*aggregate* latency/throughput statistics (mean + percentiles), not per-request data.
So exact per-request goodput ("fraction of requests meeting ALL SLOs") is not directly
computable here. We therefore expose two truthful things instead of inventing a number:

  1. A per-metric **SLO verdict** at the relevant statistic (e.g. is TTFT p99 <= target?),
     which is exact given the report.
  2. A **goodput estimate** *bounded* from the percentile ladder: for a single latency
     SLO we locate the target between two reported percentiles and interpolate the
     fraction of requests under it (a monotonic, clearly-labelled estimate). When several
     SLOs apply, the combined goodput is reported as an upper bound (min across metrics),
     because the report does not tell us how violations correlate across requests.

The estimate is always returned alongside ``method`` and ``is_estimate`` so the agent can
be honest with the user. No extrapolation beyond the reported percentiles.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

# Latency SLO fields map onto the summary's latency.<key>; throughput floors onto
# throughput.<key>. Each carries a canonical unit the target is expressed in.
# (metric_path, target_field, direction, canonical_unit)
_LATENCY_SLOS: tuple[tuple[str, str, str], ...] = (
    ("latency.ttft", "ttft_ms", "ms"),
    ("latency.tpot", "tpot_ms", "ms"),
    ("latency.itl", "itl_ms", "ms"),
    ("latency.request_latency", "request_latency_ms", "ms"),
)
_THROUGHPUT_SLOS: tuple[tuple[str, str, str], ...] = (
    ("throughput.output_token_rate", "throughput_floor_tok_s", "tokens/s"),
)

# Percentile ladder we can read off an aggregate Statistics object, low->high.
# Values are the fraction-of-requests-below that the percentile represents.
_PERCENTILE_LADDER: tuple[tuple[str, float], ...] = (
    ("p0p1", 0.001), ("p1", 0.01), ("p5", 0.05), ("p10", 0.10), ("p25", 0.25),
    ("p50", 0.50), ("p75", 0.75), ("p90", 0.90), ("p95", 0.95),
    ("p99", 0.99), ("p99p9", 0.999),
)

# Multipliers to convert a reported latency unit into milliseconds.
_TO_MS: dict[str, float] = {
    "ms": 1.0, "millisecond": 1.0, "milliseconds": 1.0,
    "s": 1000.0, "sec": 1000.0, "secs": 1000.0, "second": 1000.0, "seconds": 1000.0,
    "s/token": 1000.0, "ms/token": 1.0,
    "us": 0.001, "microsecond": 0.001, "microseconds": 0.001,
}
# Multipliers to convert a reported throughput unit into tokens/s.
_TO_TOK_S: dict[str, float] = {
    "tokens/s": 1.0, "tokens/sec": 1.0, "tok/s": 1.0, "tps": 1.0,
    "tokens/min": 1.0 / 60.0,
}


class SLOTargets(BaseModel):
    """User-specified Quality-of-Service targets (proposal §2.2 #4 / §3.4).

    All optional; only the ones set are checked. Latency targets are *maxima* in
    milliseconds; the throughput floor is a *minimum* in tokens/sec. ``ttft_percentile``
    selects which statistic the latency SLOs are judged at (default p99 — the tail the
    SLO usually constrains).
    """

    ttft_ms: float | None = Field(default=None, ge=0, description="Max time-to-first-token (ms)")
    tpot_ms: float | None = Field(default=None, ge=0, description="Max time-per-output-token / TBT (ms)")
    itl_ms: float | None = Field(default=None, ge=0, description="Max inter-token latency (ms)")
    request_latency_ms: float | None = Field(default=None, ge=0, description="Max end-to-end request latency (ms)")
    throughput_floor_tok_s: float | None = Field(default=None, ge=0, description="Min output token throughput (tokens/s)")
    percentile: Literal["mean", "p50", "p90", "p95", "p99", "p99p9"] = Field(
        default="p99", description="Which statistic the latency SLOs are evaluated at"
    )
    min_success_rate_pct: float | None = Field(
        default=None, ge=0, le=100, description="Min request success rate (%) for the run to count"
    )

    @model_validator(mode="after")
    def _at_least_one(self) -> "SLOTargets":
        if not any(
            v is not None
            for v in (self.ttft_ms, self.tpot_ms, self.itl_ms, self.request_latency_ms,
                      self.throughput_floor_tok_s, self.min_success_rate_pct)
        ):
            raise ValueError("at least one SLO target must be set")
        return self

    def is_empty(self) -> bool:
        return False


@dataclass
class MetricVerdict:
    metric: str          # human name
    path: str            # dotted summary path
    statistic: str       # which stat was checked (e.g. "p99")
    direction: str       # "max" (latency) or "min" (throughput)
    target: float        # in canonical units (ms or tokens/s)
    observed: float | None
    units: str | None    # canonical units of target/observed
    met: bool | None     # None when the metric is absent from the report
    goodput_fraction: float | None = None   # est. fraction of requests meeting THIS slo
    goodput_method: str | None = None        # how goodput_fraction was derived


def _dig(summary: dict[str, Any], dotted: str) -> Any:
    cur: Any = summary
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _convert(value: float, units: str | None, table: dict[str, float]) -> float | None:
    """Convert ``value`` from its reported ``units`` into the table's canonical unit."""
    if units is None:
        # No declared unit: assume the value is already canonical (caller's risk).
        return float(value)
    mult = table.get(str(units).strip().lower())
    if mult is None:
        return None
    return float(value) * mult


def _stat(metric_obj: Any, stat: str) -> float | None:
    if not isinstance(metric_obj, dict):
        return None
    v = metric_obj.get(stat)
    return float(v) if isinstance(v, (int, float)) else None


def _goodput_for_latency(metric_obj: dict[str, Any], target_canonical: float, units: str | None) -> tuple[float | None, str]:
    """Estimate the fraction of requests with latency <= target, from the percentile ladder.

    Walks the reported percentiles (converted to canonical units). If the target falls
    between two reported percentiles we linearly interpolate the fraction between them
    (a monotonic estimate); if it's below the smallest reported value the fraction is ~0;
    above the largest it's ~1 (bounded by what the report actually contains). Returns
    ``(fraction, method)``; fraction is None if no usable percentiles exist.
    """
    ladder: list[tuple[float, float]] = []  # (latency_canonical, cumulative_fraction)
    for key, frac in _PERCENTILE_LADDER:
        raw = metric_obj.get(key)
        if isinstance(raw, (int, float)):
            conv = _convert(float(raw), units, _TO_MS)
            if conv is not None:
                ladder.append((conv, frac))
    if not ladder:
        return None, "no percentiles available"
    ladder.sort(key=lambda t: t[0])

    # Target at or below the lowest reported latency: at most that percentile met it.
    if target_canonical <= ladder[0][0]:
        return 0.0 if target_canonical < ladder[0][0] else ladder[0][1], "percentile-interpolation"
    # Target at or above the highest reported latency: at least that percentile met it.
    if target_canonical >= ladder[-1][0]:
        return ladder[-1][1], "percentile-interpolation (>= max reported percentile)"
    # Interpolate between the bracketing percentiles.
    for (lat_lo, frac_lo), (lat_hi, frac_hi) in zip(ladder, ladder[1:]):
        if lat_lo <= target_canonical <= lat_hi:
            if lat_hi == lat_lo:
                return frac_hi, "percentile-interpolation"
            t = (target_canonical - lat_lo) / (lat_hi - lat_lo)
            return frac_lo + t * (frac_hi - frac_lo), "percentile-interpolation"
    return ladder[-1][1], "percentile-interpolation"


def evaluate_slo(summary: dict[str, Any], slo: SLOTargets) -> dict[str, Any]:
    """Check one run's summary against the SLO targets.

    Returns the per-metric verdicts, an overall pass/fail (all *present* SLOs met AND
    success-rate floor satisfied), and an estimated combined goodput (the min of the
    per-latency-SLO goodput estimates — an upper bound, since the report doesn't reveal
    how per-request violations correlate). Throughput floors are a run-level gate, not a
    per-request fraction, so they gate ``met`` but don't enter the goodput estimate.
    """
    verdicts: list[MetricVerdict] = []
    latency_goodputs: list[float] = []

    for path, field_name, unit in _LATENCY_SLOS:
        target = getattr(slo, field_name)
        if target is None:
            continue
        metric_obj = _dig(summary, path)
        observed_raw = _stat(metric_obj, slo.percentile)
        units = metric_obj.get("units") if isinstance(metric_obj, dict) else None
        observed = _convert(observed_raw, units, _TO_MS) if observed_raw is not None else None
        met = (observed <= target) if observed is not None else None
        gp, method = (None, None)
        if isinstance(metric_obj, dict):
            gp, method = _goodput_for_latency(metric_obj, target, units)
            if gp is not None:
                latency_goodputs.append(gp)
        verdicts.append(MetricVerdict(
            metric=field_name.removesuffix("_ms"), path=path, statistic=slo.percentile,
            direction="max", target=target, observed=observed, units="ms", met=met,
            goodput_fraction=gp, goodput_method=method,
        ))

    for path, field_name, unit in _THROUGHPUT_SLOS:
        target = getattr(slo, field_name)
        if target is None:
            continue
        metric_obj = _dig(summary, path)
        observed_raw = _stat(metric_obj, "mean")
        units = metric_obj.get("units") if isinstance(metric_obj, dict) else None
        observed = _convert(observed_raw, units, _TO_TOK_S) if observed_raw is not None else None
        met = (observed >= target) if observed is not None else None
        verdicts.append(MetricVerdict(
            metric=field_name.removesuffix("_tok_s"), path=path, statistic="mean",
            direction="min", target=target, observed=observed, units="tokens/s", met=met,
        ))

    success_rate = summary.get("success_rate_pct")
    success_ok: bool | None = None
    if slo.min_success_rate_pct is not None:
        success_ok = (
            success_rate >= slo.min_success_rate_pct
            if isinstance(success_rate, (int, float)) else None
        )

    checked = [v for v in verdicts if v.met is not None]
    gates = [v.met for v in checked]
    if success_ok is not None:
        gates.append(success_ok)
    # "met overall" only if every checked gate passed and at least one thing was checked.
    overall = bool(gates) and all(gates)

    goodput_estimate = min(latency_goodputs) if latency_goodputs else None

    return {
        "overall_met": overall,
        "checked_count": len(checked) + (1 if success_ok is not None else 0),
        "verdicts": [v.__dict__ for v in verdicts],
        "success_rate_pct": success_rate,
        "success_rate_met": success_ok,
        "goodput": {
            "estimate_fraction": goodput_estimate,
            "estimate_pct": round(goodput_estimate * 100.0, 2) if goodput_estimate is not None else None,
            "is_estimate": True,
            "method": (
                "min over per-SLO percentile-interpolation (upper bound; "
                "report carries only aggregate percentiles, not per-request data)"
                if goodput_estimate is not None else None
            ),
            "from_slos": [v.metric for v in verdicts if v.goodput_fraction is not None],
        },
    }


# ---- Pareto / DoE analysis over a sweep ------------------------------------

# The objective space for the frontier: (summary path, human name, direction).
# "min" = smaller is better (latency); "max" = larger is better (throughput).
_OBJECTIVES: tuple[tuple[str, str, str], ...] = (
    ("latency.ttft", "ttft", "min"),
    ("latency.tpot", "tpot", "min"),
    ("latency.itl", "itl", "min"),
    ("latency.request_latency", "request_latency", "min"),
    ("throughput.output_token_rate", "output_token_rate", "max"),
    ("throughput.total_token_rate", "total_token_rate", "max"),
    ("throughput.request_rate", "request_rate", "max"),
)
_STAT_PREFERENCE = ("mean", "p50", "p90", "p95", "p99")


def _objective_value(summary: dict[str, Any], path: str) -> float | None:
    obj = _dig(summary, path)
    if not isinstance(obj, dict):
        return None
    for s in _STAT_PREFERENCE:
        v = obj.get(s)
        if isinstance(v, (int, float)):
            return float(v)
    return None


def _dominates(a: dict[str, float], b: dict[str, float], dirs: dict[str, str]) -> bool:
    """Does point ``a`` Pareto-dominate ``b`` across the shared objectives ``dirs``?
    a dominates b iff a is no worse on every objective and strictly better on at least one.
    """
    no_worse_all = True
    strictly_better_any = False
    for key, direction in dirs.items():
        av, bv = a.get(key), b.get(key)
        if av is None or bv is None:
            continue
        if direction == "min":
            if av > bv:
                no_worse_all = False
            if av < bv:
                strictly_better_any = True
        else:  # max
            if av < bv:
                no_worse_all = False
            if av > bv:
                strictly_better_any = True
    return no_worse_all and strictly_better_any


def pareto_analysis(
    entries: list[dict[str, Any]],
    *,
    slo: SLOTargets | None = None,
) -> dict[str, Any]:
    """Identify Pareto-optimal configurations across a sweep matrix (proposal §3.4 DoE).

    ``entries`` is ``[{"label": str, "summary": <summarize_report output>}, ...]``.
    Computes, for the set of objectives present in 2+ runs, which runs are on the Pareto
    frontier (no other run dominates them). If ``slo`` is given, each run is also tagged
    with whether it satisfies the SLOs, an estimated goodput, and the SLO-feasible subset
    gets its own frontier (so "best throughput at a given latency constraint" is answerable).
    Returns facts only; the recommendation is the agent's job (knowledge/analysis.md).
    """
    if len(entries) < 2:
        raise ValueError("need at least two runs for a Pareto/DoE analysis")

    labels = [e.get("label") or f"run{i + 1}" for i, e in enumerate(entries)]
    summaries = [e.get("summary") or {} for e in entries]

    # Which objectives are actually present in >=2 runs (only those are comparable).
    points: list[dict[str, float]] = [{} for _ in entries]
    dirs: dict[str, str] = {}
    obj_meta: dict[str, dict[str, Any]] = {}
    for path, name, direction in _OBJECTIVES:
        vals = [_objective_value(s, path) for s in summaries]
        present = sum(v is not None for v in vals)
        if present < 2:
            continue
        dirs[name] = direction
        units = next(
            (_dig(s, path).get("units") for s in summaries
             if isinstance(_dig(s, path), dict) and _dig(s, path).get("units") is not None),
            None,
        )
        obj_meta[name] = {"path": path, "direction": direction, "units": units}
        for i, v in enumerate(vals):
            if v is not None:
                points[i][name] = v

    if not dirs:
        return {
            "objectives": [], "runs": [], "frontier": [],
            "note": "no objective metric is present in two or more runs — nothing to compare",
        }

    # SLO tagging (optional).
    slo_eval: list[dict[str, Any] | None] = [None] * len(entries)
    if slo is not None:
        slo_eval = [evaluate_slo(s, slo) for s in summaries]

    # Frontier over ALL runs.
    frontier = _frontier_labels(points, dirs, labels)

    runs_out: list[dict[str, Any]] = []
    for i, label in enumerate(labels):
        item: dict[str, Any] = {
            "label": label,
            "objectives": points[i],
            "on_frontier": label in frontier,
        }
        if slo_eval[i] is not None:
            item["slo_met"] = slo_eval[i]["overall_met"]
            item["goodput_pct"] = slo_eval[i]["goodput"]["estimate_pct"]
            item["slo_eval"] = slo_eval[i]
        runs_out.append(item)

    result: dict[str, Any] = {
        "objectives": [{"name": k, **obj_meta[k]} for k in dirs],
        "n": len(entries),
        "runs": runs_out,
        "frontier": frontier,
    }

    # SLO-feasible frontier: best trade-offs among only the runs that meet the SLOs.
    if slo is not None:
        feasible_idx = [i for i in range(len(entries)) if slo_eval[i] and slo_eval[i]["overall_met"]]
        feasible_labels = [labels[i] for i in feasible_idx]
        result["slo_feasible"] = feasible_labels
        if len(feasible_idx) >= 1:
            fpoints = [points[i] for i in feasible_idx]
            result["slo_frontier"] = _frontier_labels(fpoints, dirs, feasible_labels)
        else:
            result["slo_frontier"] = []
            result["note"] = "no run satisfies all SLO targets"

    return result


def _frontier_labels(points: list[dict[str, float]], dirs: dict[str, str], labels: list[str]) -> list[str]:
    """Return the labels of the Pareto-non-dominated points."""
    frontier: list[str] = []
    for i, pi in enumerate(points):
        if not pi:
            continue  # a run with no comparable objective can't be placed
        dominated = any(
            j != i and points[j] and _dominates(points[j], pi, dirs)
            for j in range(len(points))
        )
        if not dominated:
            frontier.append(labels[i])
    return frontier
