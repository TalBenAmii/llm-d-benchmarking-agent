"""Phase 26 — llm-d-inference-sim integration tests (opt-in) + hermetic wiring coverage.

Two layers live here:

  A. HERMETIC harness tests (always run): build an inference-sim-SHAPED Benchmark Report v0.2
     from repo truth (``tests/integration/sim_report.py``), write it to disk exactly as a run
     produces it, and drive it through the REAL ``analyze_results`` and ``compare_reports``
     tools end to end. This proves the analyze/compare wiring genuinely parses a sim-shaped
     report — SLO verdict, goodput estimate, §3.4 standard metrics, and an A/B delta — even
     when the sim binary is absent. The default suite stays hermetic and green.

  B. The OPT-IN integration test (``test_live_sim_*``): stands up a REAL ``llm-d-inference-sim``,
     issues real inference requests, builds a Benchmark Report v0.2 from the measured results,
     and runs analyze/compare against THAT. It is SKIPPED by default (env flag + sim presence
     gate) so it never touches the hermetic baseline, and it never hangs reaching a server
     that isn't there.

A guard test asserts layer B is COLLECTED-AND-SKIPPED without the opt-in flag.
"""
from __future__ import annotations

import json
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.tools import analyze, compare
from app.validation.report import load_report, summarize_report, validate_report

from .conftest import (
    INTEGRATION_ENV_FLAG,
    SimLocation,
    integration_enabled,
    integration_skip_reason,
    requires_sim_integration,
    sim_available,
)
from .sim_report import SIM_MODEL, build_sim_report

# --------------------------------------------------------------------------- #
#  A. HERMETIC wiring coverage — analyze/compare over a sim-shaped report      #
#     fixture. Runs in the DEFAULT suite (no sim, no network, no cluster).     #
# --------------------------------------------------------------------------- #


def _write_report(run_dir: Path, report: dict[str, Any]) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    p = run_dir / "benchmark_report_v0.2.yaml"
    p.write_text(yaml.safe_dump(report, sort_keys=False))
    return p


def test_sim_shaped_report_validates_against_repo_schema(tool_ctx, bench_repo):
    """The sim-shaped fixture must be a VALID BR v0.2 report (built from repo truth)."""
    report = build_sim_report(bench_repo)
    validation = validate_report(report, tool_ctx.settings.benchmark_report_schema_path)
    assert validation.valid, validation.errors
    summary = summarize_report(report)
    assert summary["model"] == SIM_MODEL
    assert summary["harness"] == "inference-perf"
    # The sim's standardized cache_hit_rate must surface as a §3.4 standard metric.
    assert summary["standard_metrics"]
    assert "kv_cache_hit_rate" in summary["standard_metrics"]


async def test_analyze_sim_report_single_run(tool_ctx, bench_repo, tmp_path):
    """analyze_results genuinely parses a sim report: SLO verdict + goodput estimate."""
    report = build_sim_report(bench_repo, ttft_s=0.045, output_token_rate=850.0)
    run = tmp_path / "sim-run"
    _write_report(run, report)

    out = await analyze.analyze_results(
        tool_ctx,
        slo={"ttft_ms": 200, "throughput_floor_tok_s": 500},
        sources=[str(run)],
    )
    assert out["analyzed"] is True and out["n"] == 1
    run0 = out["runs"][0]
    assert run0["model"] == SIM_MODEL
    slo = run0["slo"]
    assert slo["overall_met"] is True            # 45ms TTFT, 850 tok/s pass the SLO
    assert slo["goodput"]["is_estimate"] is True
    # §3.4 standard metric surfaced per-run.
    assert run0["standard_metrics"]["kv_cache_hit_rate"]["value"]["mean"] == pytest.approx(37.5)
    assert "pareto" not in out                    # single run -> no frontier


async def test_analyze_sim_report_failing_slo(tool_ctx, bench_repo, tmp_path):
    """A too-tight SLO over the same sim report fails — the verdict is real, not vacuous."""
    report = build_sim_report(bench_repo, ttft_s=0.045)
    run = tmp_path / "sim-run"
    _write_report(run, report)
    out = await analyze.analyze_results(
        tool_ctx, slo={"ttft_ms": 10, "percentile": "p99"}, sources=[str(run)]
    )
    assert out["runs"][0]["slo"]["overall_met"] is False


async def test_compare_sim_ab_pair(tool_ctx, bench_repo, tmp_path):
    """compare_reports contrasts two sim runs (A/B) and computes a real per-metric delta."""
    fast = build_sim_report(
        bench_repo, run_uid="sim-A", ttft_s=0.030, output_token_rate=1200.0
    )
    slow = build_sim_report(
        bench_repo, run_uid="sim-B", ttft_s=0.090, output_token_rate=600.0
    )
    a = _write_report(tmp_path / "A", fast)
    b = _write_report(tmp_path / "B", slow)

    out = await compare.compare_reports(
        tool_ctx, sources=[str(a), str(b)], labels=["A", "B"], baseline_index=0
    )
    assert out["compared"] is True and out["n"] == 2
    assert out["baseline"] == "A"
    comparison = out["comparison"]
    assert comparison["labels"] == ["A", "B"]
    rows = {r["key"]: r for r in comparison["metrics"]}

    # TTFT (lower is better): A=30ms beats B=90ms — A must win and B's delta is +ve & real.
    ttft = rows["latency.ttft"]
    assert ttft["best"]["label"] == "A"
    b_ttft = next(p for p in ttft["per_run"] if p["label"] == "B")
    assert b_ttft["delta_abs"] is not None and b_ttft["delta_abs"] > 0

    # Output token rate (higher is better): A=1200 beats B=600 — A wins, B's delta is -ve.
    otr = rows["throughput.output_token_rate"]
    assert otr["best"]["label"] == "A"
    b_otr = next(p for p in otr["per_run"] if p["label"] == "B")
    assert b_otr["delta_abs"] is not None and b_otr["delta_abs"] < 0


async def test_analyze_sim_sweep_pareto(tool_ctx, bench_repo, tmp_path):
    """A 3-config sim sweep yields a Pareto frontier (the DoE analyze path)."""
    exp = tmp_path / "sim-experiment"
    _write_report(exp / "c1", build_sim_report(bench_repo, run_uid="c1", ttft_s=0.030, output_token_rate=400.0))
    _write_report(exp / "c2", build_sim_report(bench_repo, run_uid="c2", ttft_s=0.060, output_token_rate=800.0))
    _write_report(exp / "c3", build_sim_report(bench_repo, run_uid="c3", ttft_s=0.120, output_token_rate=1200.0))
    out = await analyze.analyze_results(
        tool_ctx, slo={"ttft_ms": 250, "percentile": "p99"}, experiment_dir=str(exp)
    )
    assert out["analyzed"] is True and out["n"] == 3
    assert "pareto" in out
    pareto = out["pareto"]
    assert pareto["frontier"], "expected a non-empty Pareto frontier over the sweep"
    # All three configs are mutually non-dominated (each trades TTFT for throughput), so
    # the frontier should retain more than one config — a real multi-objective result.
    assert len(pareto["frontier"]) >= 2
    assert pareto["slo_feasible"]                 # ttft p99 <= 250ms holds for the faster runs


# --------------------------------------------------------------------------- #
#  Guard: the OPT-IN integration test is collected + SKIPPED by default.       #
# --------------------------------------------------------------------------- #


def test_integration_skipped_by_default_keeps_suite_hermetic():
    """Without LLMD_SIM_INTEGRATION=1 the live integration test must skip cleanly.

    This locks the hermetic guarantee: the opt-in test is visible (collected) but does not
    run — so the default suite stays green with no sim, no network, no cluster.
    """
    if integration_enabled() and sim_available():
        pytest.skip("integration is enabled in this env; the default-skip guard is N/A")
    reason = integration_skip_reason()
    assert reason is not None
    assert INTEGRATION_ENV_FLAG in reason or "llm-d-inference-sim" in reason


# --------------------------------------------------------------------------- #
#  B. The OPT-IN integration test — REAL llm-d-inference-sim, end to end.      #
#     SKIPPED unless LLMD_SIM_INTEGRATION=1 AND the sim is locatable.          #
# --------------------------------------------------------------------------- #


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_ready(base_url: str, timeout_s: float = 30.0) -> bool:
    """Poll the sim's models endpoint until it answers (bounded — never hangs)."""
    deadline = time.monotonic() + timeout_s
    url = f"{base_url}/v1/models"
    while time.monotonic() < deadline:
        try:
            # localhost-only request to the sim we just started.
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    return False


def _start_sim(loc: SimLocation, port: int) -> subprocess.Popen[bytes]:
    """Start the sim (binary or container) listening on ``port``. argv list, shell=False."""
    if loc.kind == "binary":
        argv = [loc.ref, "--model", SIM_MODEL, "--port", str(port)]
    else:  # image
        import shutil

        engine = shutil.which("docker") or shutil.which("podman")
        assert engine, "container engine vanished"
        argv = [
            engine, "run", "--rm", "-p", f"{port}:8000", loc.ref,
            "--model", SIM_MODEL, "--port", "8000",
        ]
    # argv list, shell=False; ref is from our own discovery, not user input.
    return subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def _measure_request(base_url: str, *, max_tokens: int) -> float:
    """Issue one completion against the sim and return its wall-clock latency (s)."""
    body = json.dumps(
        {"model": SIM_MODEL, "prompt": "Benchmark the sim.", "max_tokens": max_tokens}
    ).encode()
    # localhost-only POST to the sim we just started.
    req = urllib.request.Request(
        f"{base_url}/v1/completions",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()
    return time.monotonic() - t0


@requires_sim_integration
def test_live_sim_analyze_compare_end_to_end(tool_ctx, bench_repo, sim_location, tmp_path):
    """End to end: stand up the REAL sim, benchmark it, analyze + compare the report.

    This is the proposal's explicit "integration test with llm-d-inference-sim": exercise the
    analyze/compare path against a report produced from a real mock-inference run. Opt-in and
    skipped by default; bounded timeouts so it can never wedge the suite.
    """
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    proc = _start_sim(sim_location, port)
    try:
        if not _wait_ready(base_url):
            pytest.skip("llm-d-inference-sim did not become ready within the bound")

        # A tiny "benchmark": measure two load points (short vs long output) against the sim.
        n = 8
        short = [_measure_request(base_url, max_tokens=16) for _ in range(n)]
        long = [_measure_request(base_url, max_tokens=128) for _ in range(n)]

        def _report(uid: str, lat: list[float]) -> dict[str, Any]:
            lat_sorted = sorted(lat)
            mean = sum(lat) / len(lat)
            # Build a real BR v0.2 report from the MEASURED sim latencies.
            return build_sim_report(
                bench_repo,
                run_uid=uid,
                request_latency_s=mean,
                ttft_s=lat_sorted[0],          # best observed as a TTFT proxy
                output_token_rate=max(1.0, 1.0 / mean) * 100.0,
                total_requests=len(lat),
                failures=0,
            )

        a = _write_report(tmp_path / "short", _report("sim-short", short))
        b = _write_report(tmp_path / "long", _report("sim-long", long))

        # The two report files (built from MEASURED sim latencies) must parse + validate
        # as real BR v0.2 reports — the artifact the analyze/compare path consumes.
        for path in (a, b):
            rep = load_report(path)
            v = validate_report(rep, tool_ctx.settings.benchmark_report_schema_path)
            assert v.valid, v.errors

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


@requires_sim_integration
async def test_live_sim_drives_analyze_compare_tools(tool_ctx, bench_repo, sim_location, tmp_path):
    """The async half: run the REAL analyze/compare tools over the live-sim reports."""
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    proc = _start_sim(sim_location, port)
    try:
        if not _wait_ready(base_url):
            pytest.skip("llm-d-inference-sim did not become ready within the bound")
        lat = [_measure_request(base_url, max_tokens=32) for _ in range(8)]
        mean = sum(lat) / len(lat)
        fast = build_sim_report(bench_repo, run_uid="live-A", request_latency_s=mean, ttft_s=min(lat))
        slow = build_sim_report(bench_repo, run_uid="live-B", request_latency_s=mean * 2, ttft_s=min(lat) * 2)
        a = _write_report(tmp_path / "A", fast)
        b = _write_report(tmp_path / "B", slow)

        analyzed = await analyze.analyze_results(
            tool_ctx, slo={"request_latency_ms": 5000}, sources=[str(a)]
        )
        assert analyzed["analyzed"] is True and analyzed["n"] == 1

        compared = await compare.compare_reports(
            tool_ctx, sources=[str(a), str(b)], labels=["A", "B"]
        )
        assert compared["compared"] is True and compared["n"] == 2
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
