"""Reproducibility — the self-contained HTML report card (app/packaging/report_card.py).

Hermetic, pure string assertions: the HTML must carry the SHAs / model / regenerate command, must
carry ZERO external asset links (no http(s) URL, no <link href=…> to a non-data URL, no
<img src=…>), must surface a dirty/unavailable honesty banner, and must HTML-escape interpolated
values so a bundle value cannot inject markup.
"""
from __future__ import annotations

import re

from app.packaging.report_card import render_report_card


def _bundle(**overrides):
    b = {
        "bundle_id": "abc123def456",
        "model": "meta-llama/Llama-3.1-8B",
        "agent_version": "0.1.0",
        "created_at": 1_700_000_000.0,
        "harness": "inference-perf",
        "spec": "cicd/kind",
        "workload": "sanity_random.yaml",
        "namespace": "ns-prod",
        "repos": {
            "llm-d": {"sha": "abcd123", "dirty": False, "ref": "main"},
            "llm-d-benchmark": {"sha": "ef99887", "dirty": False, "ref": "main"},
        },
        "resolved_config": {"found": True, "path": "/ws/run-config.yaml", "body": "spec: cicd/kind\n"},
        "report_summary": {
            "model": "meta-llama/Llama-3.1-8B", "harness": "inference-perf",
            "requests_total": 500, "success_rate_pct": 99.8,
            "latency": {
                "ttft": {"units": "s", "mean": 0.12, "p50": 0.10, "p99": 0.40},
                "request_latency": {"units": "s", "mean": 2.1, "p99": 5.0},
            },
            "throughput": {"output_token_rate": {"units": "tokens/s", "mean": 420.0}},
        },
        "report_digest": "deadbeefcafef00d",
        "knowledge_version": "cafef00dbeef",
        "regenerate_command": "llmdbenchmark run -c /ws/run-config.yaml -p ns-prod",
        "dirty": False,
        "env_snapshot": {"kube_context": {"context": "kind-llmd"}},
    }
    b.update(overrides)
    return b


def test_report_card_contains_shas_model_and_command():
    html = render_report_card(_bundle())
    assert "abcd123" in html and "ef99887" in html
    assert "meta-llama/Llama-3.1-8B" in html
    assert "llmdbenchmark run -c /ws/run-config.yaml -p ns-prod" in html
    # Provenance hashes + key facts are present.
    assert "deadbeefcafef00d" in html and "cafef00dbeef" in html
    assert "cicd/kind" in html and "inference-perf" in html
    # The resolved-config body is embedded (so the run is fully reproducible from the card).
    assert "spec: cicd/kind" in html


def test_report_card_has_no_external_assets():
    html = render_report_card(_bundle())
    # No network URLs at all (not even the SVG namespace) — the doc is fully self-contained.
    assert "http://" not in html and "https://" not in html
    # No <link href=…> to a non-data stylesheet/asset, no <img src=…> (logo is inline <svg>).
    assert not re.search(r"<link[^>]+href=", html)
    assert not re.search(r"<img\b", html)
    # No <script src=…> either.
    assert not re.search(r"<script[^>]+src=", html)
    # Fonts come from a system stack, not a font <link>.
    assert "fonts.googleapis" not in html
    # CSS is inlined.
    assert "<style>" in html


def test_report_card_dirty_repo_shows_loud_banner():
    b = _bundle(dirty=True)
    b["repos"]["llm-d"]["dirty"] = True
    html = render_report_card(b)
    assert "banner" in html
    assert "uncommitted" in html.lower()


def test_report_card_unavailable_sha_is_honest():
    b = _bundle()
    b["repos"]["llm-d-benchmark"] = {"sha": None, "dirty": None, "unavailable": True}
    html = render_report_card(b)
    assert "unavailable" in html.lower()
    # The honesty banner fires for an unavailable SHA too.
    assert "banner" in html
    assert "NOT captured" in html or "not captured" in html.lower()


def test_report_card_no_banner_when_clean_and_available():
    html = render_report_card(_bundle())
    assert "prov-dirty-banner" not in html
    assert '<div class="banner">' not in html


def test_report_card_percentile_ladder_present():
    html = render_report_card(_bundle())
    # The full percentile ladder is rendered as a table with the column headers.
    assert "All percentiles" in html
    assert "p99p9" in html and "p50" in html
    assert "<table" in html


def test_report_card_escapes_injected_markup():
    # A value carrying HTML must be escaped — never rendered as live markup.
    b = _bundle(model="<script>alert(1)</script>")
    html = render_report_card(b)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_report_card_handles_missing_fields_gracefully():
    # A minimal bundle (no env, no config, no repos) still renders without crashing.
    html = render_report_card({
        "bundle_id": "x1", "report_summary": {}, "repos": {},
        "resolved_config": {"found": False, "note": "no config"},
        "regenerate_command": "llmdbenchmark run -c <cfg> -p <ns>",
    })
    assert "<html" in html and "Reproduce" in html
    assert "no config" in html


def test_report_card_is_a_full_html_document():
    html = render_report_card(_bundle())
    assert html.startswith("<!DOCTYPE html>")
    assert "</html>" in html
    assert "Benchmark report" in html
