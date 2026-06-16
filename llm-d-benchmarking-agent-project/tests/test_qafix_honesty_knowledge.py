"""QA-fix regression guards — the *honesty* rules added to the agent's brain.

These are hermetic content assertions over the editable ``knowledge/`` files. They lock in
the anti-fabrication guidance added to fix a batch of QA findings (absent-metric / P99
fabrication, SIMULATE probe narration as real host facts, trusting user-supplied data as
validated, "live catalog" claimed without a tool call, and throughput-vs-concurrency).

The JUDGMENT lives in the markdown/yaml; these tests only assert the guidance is PRESENT and
loads cleanly, so a future edit can't silently strip it. No network / repo / cluster needed.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.config import get_settings


def _kdir() -> Path:
    return get_settings().knowledge_dir


def _read(name: str) -> str:
    return (_kdir() / name).read_text(encoding="utf-8")


# ---- absent-metric / P99 fabrication (findings sim-1 ×3, sim-2 ×2) -------------------

def test_results_interpretation_has_absent_metric_floor() -> None:
    txt = _read("results_interpretation.md").lower()
    assert "honesty floor" in txt
    assert "ttft_ms_p99" in txt  # names the field that is absent in SIMULATE
    assert "not available" in txt
    assert "cannot be verified" in txt
    # must explicitly forbid deriving p99 from p90/p50
    assert "p90/p50" in txt or "from p90" in txt


def test_analysis_absent_metric_is_inconclusive_not_verdict() -> None:
    txt = _read("analysis.md").lower()
    assert "inconclusive" in txt
    assert "extrapolate" in txt
    # never a definitive verdict on an unmeasured metric
    assert "unmeasured metric" in txt


# ---- self-estimate misattributed to the sim engine (finding sim-1 21:30) -------------

def test_results_interpretation_forbids_misattributing_estimate() -> None:
    txt = _read("results_interpretation.md").lower()
    assert "placeholder output" in txt  # the exact false attribution to forbid
    assert "you** estimated it" in txt  # attribute the estimate to yourself


# ---- SIMULATE probe narration as real host facts (sim-6, AGENT_FINDINGS ×3) ----------

def test_sim_integration_probe_outcomes_are_simulated() -> None:
    txt = " ".join(_read("sim_integration.md").lower().split())  # collapse line-wraps
    assert "no command actually ran)" in txt
    assert "unsolicited" in txt
    assert "zero tool calls" in txt
    # the specific fabricated readiness phrasings should be called out as forbidden
    assert "docker is up" in txt
    assert "cluster reachable" in txt


# ---- trusting user-supplied data as validated (sim-1 03:30 / 17:30, sim-2 09:10) -----

def test_results_interpretation_only_validated_reports_authoritative() -> None:
    txt = _read("results_interpretation.md").lower()
    assert "pasted, typed, or recalled" in txt
    assert "unverified input, not data" in txt
    # verbal disclaimer is not a substitute for refusing the numbers
    assert "verbal disclaimer is not a substitute" in txt
    # ghost-job + "today's" recency framing
    assert "your job completed before the crash" in txt
    assert "today's" in txt


def test_history_refuses_unverified_baselines() -> None:
    txt = _read("history.md").lower()
    assert "never seed history/trends with user-asserted numbers" in txt
    assert "re-run" in txt


# ---- "live catalog" without a tool call (sim-2 00:45 + 19:10) ------------------------

def test_multi_harness_no_fake_live_catalog_claim() -> None:
    txt = _read("multi_harness.md").lower()
    assert "live catalog snapshot" in txt
    assert "list_catalog" in txt
    assert "prior knowledge" in txt


# ---- throughput vs concurrency / Little's Law (AGENT_FINDINGS 21:44) ------------------

def test_capacity_uses_littles_law_for_concurrency() -> None:
    txt = _read("capacity.md").lower()
    assert "little's law" in txt
    assert "throughput × per-request latency" in txt or "throughput x per-request latency" in txt
    assert "max concurrent requests" in txt


# ---- the edited files still parse / load cleanly -------------------------------------

def test_standard_metrics_yaml_still_parses() -> None:
    data = yaml.safe_load(_read("standard_metrics.yaml"))
    assert isinstance(data, dict) and "metrics" in data


@pytest.mark.parametrize(
    "name",
    [
        "results_interpretation.md",
        "sim_integration.md",
        "analysis.md",
        "capacity.md",
        "history.md",
        "multi_harness.md",
    ],
)
def test_edited_markdown_nonempty(name: str) -> None:
    assert len((_kdir() / name).read_text(encoding="utf-8").strip()) > 0
