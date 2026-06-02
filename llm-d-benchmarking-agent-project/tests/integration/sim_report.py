"""An ``llm-d-inference-sim``-shaped Benchmark Report v0.2 fixture.

``llm-d-inference-sim`` (ghcr.io/llm-d/llm-d-inference-sim) is the project's CPU-only mock
inference server: an OpenAI-compatible endpoint that serves a tiny model (the catalog's
``examples/sim`` / ``cicd/kind`` scenarios run ``facebook/opt-125m`` on it without a GPU).
Running a harness (inference-perf / guidellm) against it produces a real Benchmark Report
v0.2 — the SAME artifact the analyze/compare path consumes.

This module builds that report SHAPE from the repo's OWN BR v0.2 example (loaded live from
``llm-d-benchmark`` — never vendored), rewritten to reflect a sim run:

  * model ``facebook/opt-125m`` (the sim's served model), no accelerator (CPU-only),
  * the inference-sim image as the inference-engine ``tool_version``,
  * sim-plausible request-level latency/throughput numbers (tunable per call), and
  * a standardized ``cache_hit_rate`` resource metric (so the §3.4 path is exercised).

The result validates against the repo's BR v0.2 JSON Schema by construction, so the SAME
function feeds (a) the hermetic harness test (sim absent) and (b) the opt-in live
integration test's assertions (sim present) — one fixture, two callers.
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from app.validation.report import load_report

SIM_IMAGE = "ghcr.io/llm-d/llm-d-inference-sim"
SIM_MODEL = "facebook/opt-125m"


def _ladder(value: float) -> dict[str, Any]:
    """A monotone percentile ladder around ``value`` (sim runs are low-variance)."""
    return {
        "units": "s",
        "mean": value,
        "min": value * 0.85,
        "p0p1": value * 0.86,
        "p1": value * 0.88,
        "p5": value * 0.90,
        "p10": value * 0.92,
        "p25": value * 0.96,
        "p50": value,
        "p75": value * 1.05,
        "p90": value * 1.12,
        "p95": value * 1.18,
        "p99": value * 1.30,
        "p99p9": value * 1.45,
        "max": value * 1.50,
    }


def _rate_ladder(value: float) -> dict[str, Any]:
    out = _ladder(value)
    out["units"] = "tokens/s"
    return out


def build_sim_report(
    bench_repo: str | Path,
    *,
    run_uid: str = "sim-run-0001",
    ttft_s: float = 0.045,
    tpot_s: float = 0.012,
    request_latency_s: float = 0.65,
    output_token_rate: float = 850.0,
    total_requests: int = 200,
    failures: int = 0,
    kv_cache_hit_rate_pct: float | None = 37.5,
    harness: str = "inference-perf",
) -> dict[str, Any]:
    """Return a sim-shaped BR v0.2 report dict, derived from the repo's BR example.

    ``bench_repo`` is the path to the read-only ``llm-d-benchmark`` checkout (so the example
    — and therefore the schema shape — is read from repo truth, never vendored). The numeric
    knobs let a caller fabricate an A/B pair (two configs) for the compare path.
    """
    example = (
        Path(bench_repo)
        / "llmdbenchmark"
        / "analysis"
        / "benchmark_report"
        / "br_v0_2_example.yaml"
    )
    report = copy.deepcopy(load_report(example))

    # ---- run provenance -----------------------------------------------------
    run = report.setdefault("run", {})
    run["uid"] = run_uid
    run["description"] = "llm-d-inference-sim mock run (CPU, no GPU)"
    run["keywords"] = ["inference-sim", "cpu", "mock"]

    # ---- scenario: rewrite the inference engine as the sim --------------------
    stack = report.get("scenario", {}).get("stack") or []
    if stack:
        std = stack[0].setdefault("standardized", {})
        std["tool"] = "llm-d-inference-sim"
        std["tool_version"] = f"{SIM_IMAGE}:latest"
        std["model"] = {"name": SIM_MODEL}
        std["replicas"] = 1
        # CPU-only: the sim scenario still carries an accelerator object (required by the
        # inference_engine schema variant), but with count 0 — see config/scenarios/examples/
        # sim.yaml (accelerator.count: 0). Keep the block, mark it GPU-less.
        accel = std.setdefault("accelerator", {})
        accel["model"] = "cpu"
        accel["count"] = 0
        accel["parallelism"] = {"dp": 1, "dp_local": 1, "workers": 1, "ep": 1, "pp": 1, "tp": 1}

    # ---- scenario.load: which harness drove the sim -------------------------
    load_std = report.setdefault("scenario", {}).setdefault("load", {}).setdefault(
        "standardized", {}
    )
    load_std["tool"] = harness
    load_std["rate_qps"] = 10

    # ---- results.request_performance.aggregate ------------------------------
    agg = (
        report.setdefault("results", {})
        .setdefault("request_performance", {})
        .setdefault("aggregate", {})
    )
    agg.setdefault("requests", {})["total"] = total_requests
    agg["requests"]["failures"] = failures

    latency = agg.setdefault("latency", {})
    latency["time_to_first_token"] = _ladder(ttft_s)
    tpot = _ladder(tpot_s)
    tpot["units"] = "s/token"
    latency["time_per_output_token"] = tpot
    itl = _ladder(tpot_s)
    itl["units"] = "s/token"
    latency["inter_token_latency"] = itl
    latency["request_latency"] = _ladder(request_latency_s)

    throughput = agg.setdefault("throughput", {})
    throughput["output_token_rate"] = _rate_ladder(output_token_rate)
    throughput["total_token_rate"] = _rate_ladder(output_token_rate * 1.4)
    rr = _rate_ladder(output_token_rate / 100.0)
    rr["units"] = "queries/s"
    throughput["request_rate"] = rr

    # ---- results.observability: a STANDARDIZED §3.4 metric (cache_hit_rate) -
    # The sim reports a prefix-cache hit rate; surface it via the standardized
    # ResourceMetrics field so the analyzer's standard-metric path is exercised.
    obs = report.setdefault("results", {}).setdefault("observability", {})
    comps = obs.get("components")
    if not isinstance(comps, list) or not comps:
        comps = [{"component_label": "vllm-svc-0", "aggregate": {}}]
        obs["components"] = comps
    comp0_agg = comps[0].setdefault("aggregate", {})
    if kv_cache_hit_rate_pct is None:
        comp0_agg.pop("cache_hit_rate", None)
    else:
        comp0_agg["cache_hit_rate"] = {
            "units": "percent",
            "mean": kv_cache_hit_rate_pct,
            "min": kv_cache_hit_rate_pct * 0.4,
            "p50": kv_cache_hit_rate_pct,
            "p90": kv_cache_hit_rate_pct * 1.3,
            "p99": kv_cache_hit_rate_pct * 1.5,
            "max": kv_cache_hit_rate_pct * 1.6,
        }

    return report
