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


_PCTL_KEYS = ("mean", "p50", "p90", "p95", "p99")


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
    """Extract {units, mean, p50, p90, p95, p99} from a metric object, if present."""
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
