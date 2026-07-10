"""§3.4 standard-metric + multi-turn session-performance extraction (BR v0.2).

Split out of ``report.py`` (behavior-preserving): this is the metric-EXTRACTION
cluster — the pure mechanism that reads a *validated* Benchmark Report's
``results.observability`` / ``results.session_performance`` blocks and surfaces
catalogued metrics, NEVER fabricating a value the report does not carry. WHICH field
name carries WHICH metric (the discovery judgment) lives as DATA in
``knowledge/standard_metrics.yaml``; the code here is thin mechanism over that catalog.

Import direction is one-way: ``report.py`` imports from here and re-exports these names
(``from app.validation.report import extract_standard_metrics`` still works). This module
therefore imports NOTHING from ``report.py`` — that would create an import cycle, since
``report.py`` pulls these symbols in at its top. Consequently the shared stat-ladder
primitives ``_PCTL_KEYS`` / ``_stat`` are OWNED here and imported BACK by ``report.py``
(its ``summarize_report`` reuses them). The percentile-ladder SINGLE SOURCE OF TRUTH —
``_PERCENTILE_LADDER`` (name + fraction-of-requests, used by ``analysis.py`` for goodput
interpolation) — stays in ``report.py``; ``report.py`` asserts the names here match it, so
the two never drift (the documented silent-floor bug).
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.dig import dict_or_empty

# Aggregate-statistics keys we read off a Statistics object: ``mean`` (kept first, for
# throughput floors and headline comparison) followed by the full percentile ladder. This
# is the ENTIRE ladder (not just the round-number rungs) so downstream SLO evaluation and
# goodput interpolation see every reported percentile, not a lossy subset — dropping a low
# rung (p0p1, p1) silently floors sub-p50 SLO targets to 0% goodput. ``report.py`` owns the
# ladder's SSOT (``_PERCENTILE_LADDER``, which also carries each rung's fraction) and asserts
# these names stay in lockstep with it on import, so the projection here can never drift.
_PCTL_KEYS = (
    "mean",
    "p0p1", "p1", "p5", "p10", "p25",
    "p50", "p75", "p90", "p95",
    "p99", "p99p9",
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


# ---- §3.4 standard metrics (KV-cache hit rate / schedule delay / GPU util) --
#
# The proposal lists these among the Results Analyzer's "standard metrics" but, unlike
# the request-level latency/throughput numbers, they are resource/serving metrics a
# harness MAY or may not emit, under either the BR v0.2 standardized ResourceMetrics
# object OR a harness-native per-metric observability entry. WHICH field name carries
# WHICH metric (the discovery judgment) lives as DATA in knowledge/standard_metrics.yaml;
# the code here is pure mechanism — it reads that catalog and extracts the first present
# candidate, NEVER fabricating a value the report doesn't carry (thin code / thick agent).

_STANDARD_METRICS_CATALOG = (
    Path(__file__).resolve().parents[2] / "knowledge" / "analysis" / "standard_metrics.yaml"
)


@lru_cache(maxsize=8)
def _load_catalog_section(path: str, section: str) -> dict[str, Any]:
    """Load (and cache) one top-level mapping ``section`` from a knowledge YAML catalog.
    Missing file / malformed YAML / a missing-or-non-mapping section → empty (no crash)."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = yaml.safe_load(p.read_text())
    except yaml.YAMLError:
        return {}
    section_val = data.get(section) if isinstance(data, dict) else None
    return dict_or_empty(section_val)


def _native_stat(entry: Any) -> dict[str, Any] | None:
    """Pull a {units, mean, percentiles} stats dict from a harness-native observability entry.

    Harness-native entries (e.g. ``vllm_prefix_cache_hit_rate``) carry their stats under an
    ``aggregated`` block (cluster-wide) and/or a ``components[].statistics`` block (per pod).
    We prefer the cluster-wide ``aggregated`` and fall back to the first component's
    ``statistics``. Returns ``_stat``-shaped output (units + ladder), or None.
    """
    if not isinstance(entry, dict):
        return None
    agg = entry.get("aggregated")
    out = _stat(agg)
    if out:
        return out
    comps = entry.get("components")
    if isinstance(comps, list):
        for c in comps:
            if isinstance(c, dict):
                out = _stat(c.get("statistics"))
                if out:
                    return out
    return None


def _extract_standard_metric(
    observability: dict[str, Any], spec: dict[str, Any]
) -> dict[str, Any] | None:
    """Extract ONE catalogued metric from a report's ``results.observability`` block.

    Tries, in catalog-preference order: each standardized ResourceMetrics field name across
    every ``components[].aggregate`` block, then each harness-native top-level key. Returns a
    structured value (the picked stat ladder + provenance) or None when the metric is absent
    everywhere — it is NEVER fabricated.
    """
    components = observability.get("components")
    comp_list = components if isinstance(components, list) else []

    # 1) Standardized ResourceMetrics fields under components[].aggregate.
    for field_name in spec.get("standardized") or []:
        for comp in comp_list:
            if not isinstance(comp, dict):
                continue
            agg = comp.get("aggregate")
            if not isinstance(agg, dict):
                continue
            stat = _stat(agg.get(field_name))
            if stat:
                return {
                    "label": spec.get("label"),
                    "value": stat,
                    "source": "standardized",
                    "field": field_name,
                    "component_label": comp.get("component_label"),
                    "direction": spec.get("direction"),
                    "proxy": bool(spec.get("proxy", False)),
                }

    # 2) Harness-native per-metric observability entries (vendor metric keys).
    for key in spec.get("native") or []:
        stat = _native_stat(observability.get(key))
        if stat:
            return {
                "label": spec.get("label"),
                "value": stat,
                "source": "native",
                "field": key,
                "direction": spec.get("direction"),
                "proxy": bool(spec.get("proxy", False)),
            }

    return None


def extract_standard_metrics(
    report: dict[str, Any], *, catalog_path: str | Path | None = None
) -> dict[str, Any]:
    """Surface the §3.4 standard metrics (KV-cache hit rate / schedule delay / GPU util).

    Reads the field-name catalog (knowledge/standard_metrics.yaml) and mechanically pulls
    each metric from the report's ``results.observability`` block, in BR v0.2 standardized
    form first then harness-native. Metrics absent from the report are OMITTED (never
    fabricated). Returns ``{metric_name: {label, value, source, field, direction, ...}}``
    containing only the metrics actually present — an empty dict when none are.
    """
    if not isinstance(report, dict):
        return {}
    results = report.get("results", {})
    observability = results.get("observability", {}) if isinstance(results, dict) else {}
    if not isinstance(observability, dict):
        return {}
    catalog = _load_catalog_section(
        str(catalog_path) if catalog_path is not None else str(_STANDARD_METRICS_CATALOG),
        "metrics",
    )
    out: dict[str, Any] = {}
    for name, spec in catalog.items():
        if not isinstance(spec, dict):
            continue
        found = _extract_standard_metric(observability, spec)
        if found is not None:
            out[name] = found
    return out


# ---- session-level metrics (multi-turn inference-perf) ---------------------
#
# Multi-turn workloads carry a SECOND results block, results.session_performance.sessions,
# alongside the per-request request_performance: session counts plus per-session Statistics
# distributions (rate, duration, events/tokens per session). It is present ONLY for
# multi-turn runs; single-turn reports omit it entirely, so a single-turn report must yield
# None here (never a fabricated zero). WHICH field names carry the session data — and how to
# read them — is DATA in knowledge/standard_metrics.yaml under the top-level
# ``session_performance`` key (thin code / thick agent). The code below is pure mechanism: it
# reads that catalog and copies through each present scalar / extracts each present
# distribution via the same _stat() ladder used for latency/throughput. The committed JSON
# Schema lags here (Results has additionalProperties:false), so a multi-turn report surfaces
# session_performance as a NON-FATAL additionalProperties deviation under validate_report —
# validation still passes; we do not touch validate_report.


def extract_session_performance(
    report: dict[str, Any], *, catalog_path: str | Path | None = None
) -> dict[str, Any] | None:
    """Surface the multi-turn ``results.session_performance.sessions`` stats block.

    Reads the session field-name catalog (knowledge/standard_metrics.yaml →
    ``session_performance``) and mechanically pulls, from the report's
    ``results.session_performance.sessions`` block: each catalogued integer scalar (copied
    through) and each catalogued distribution (a Statistics object → the ``_stat`` units +
    percentile ladder, with the field's informational ``label``/``unit_hint``/``direction``
    attached as provenance). Returns ``None`` — not ``{}`` — when the block is absent or
    yields nothing, so single-turn reports stay ``None``. A value the report does not carry
    is NEVER fabricated.
    """
    if not isinstance(report, dict):
        return None
    results = report.get("results")
    sp = results.get("session_performance") if isinstance(results, dict) else None
    sessions = sp.get("sessions") if isinstance(sp, dict) else None
    if not isinstance(sessions, dict):
        return None

    catalog = _load_catalog_section(
        str(catalog_path) if catalog_path is not None else str(_STANDARD_METRICS_CATALOG),
        "session_performance",
    )
    scalars_spec = catalog.get("scalars")
    dists_spec = catalog.get("distributions")

    out: dict[str, Any] = {}

    scalars: dict[str, Any] = {}
    for name in scalars_spec if isinstance(scalars_spec, list) else []:
        if not isinstance(name, str):
            continue
        v = sessions.get(name)
        if isinstance(v, int) and not isinstance(v, bool):
            scalars[name] = v
    if scalars:
        out["scalars"] = scalars

    dists: dict[str, Any] = {}
    if isinstance(dists_spec, dict):
        for name, meta in dists_spec.items():
            if not isinstance(name, str):
                continue
            stat = _stat(sessions.get(name))
            if stat is None:
                continue
            meta = dict_or_empty(meta)
            dists[name] = {
                "label": meta.get("label"),
                "value": stat,
                "unit_hint": meta.get("unit_hint"),
                "direction": meta.get("direction"),
            }
    if dists:
        out["distributions"] = dists

    return out or None
