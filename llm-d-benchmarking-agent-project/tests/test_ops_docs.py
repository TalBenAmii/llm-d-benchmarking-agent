"""Phase 17 — operability docs + Prometheus alert rules.

Hermetic by default (like ``tests/test_packaging.py``): the doc tests read the shipped
markdown as text and assert the required files exist and carry the expected sections; the
alert-rules tests parse ``deploy/observability/alerts.rules.yaml`` as YAML, assert it is a
structurally valid Prometheus rule file, and — crucially — assert every metric name it
references is one the app ACTUALLY exports (derived live from ``app.observability.metrics``
so the test can never drift from the code). An optional ``promtool check rules`` runs only if
the binary happens to be installed, and skips cleanly otherwise.

No cluster, no network, no Prometheus, no extra binaries required.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from app.config import PROJECT_ROOT
from app.observability import metrics as instrument
from app.observability.metrics import Histogram, MetricsRegistry

DOCS = Path(PROJECT_ROOT) / "docs"
ALERTS = Path(PROJECT_ROOT) / "deploy" / "observability" / "alerts.rules.yaml"


# ---- helpers ---------------------------------------------------------------

def _read(path: Path) -> str:
    assert path.exists(), f"required file missing: {path}"
    return path.read_text()


def _exported_metric_base_names() -> set[str]:
    """The metric names the app really exports, taken from a fresh registry the instrument
    module defines into — so the allow-set tracks the code, not a hand-copied list."""
    reg = MetricsRegistry()
    instrument.bind_registry(reg)
    try:
        return {m.name for m in reg.metrics()}
    finally:
        instrument.bind_registry(instrument.REGISTRY)  # restore the process default


def _histogram_metric_names() -> set[str]:
    reg = MetricsRegistry()
    instrument.bind_registry(reg)
    try:
        return {m.name for m in reg.metrics() if isinstance(m, Histogram)}
    finally:
        instrument.bind_registry(instrument.REGISTRY)


# A metric reference in a PromQL expression: an identifier that looks like one of ours. We
# strip the histogram-derived suffixes (_bucket/_sum/_count) before checking membership.
_METRIC_TOKEN = re.compile(r"llmdbench_[a-z0-9_]+")
_HIST_SUFFIXES = ("_bucket", "_sum", "_count")


def _normalize_metric_token(tok: str) -> str:
    for suf in _HIST_SUFFIXES:
        if tok.endswith(suf):
            return tok[: -len(suf)]
    return tok


# ===========================================================================
# The operability docs exist and are accurate to THIS codebase
# ===========================================================================

@pytest.mark.parametrize("doc_path,needles", [
    (DOCS / "reference/SECURITY.md", (
        "Trust boundaries",
        "allowlist",            # the allowlist/approval model
        "approval",
        "security/allowlist.yaml",
        "shell=False",
        "Secret handling",
        "scrub",                # secret scrubbing
        "Network exposure",     # pairs with Phase 12
        "AUTH_ENABLED",
        "requires isolation",
    )),
    (DOCS / "guides/TROUBLESHOOTING.md", (
        "/healthz",
        "/readyz",
        "Debug",                # debug mode (Phase 1 command trail)
        "corr_id",              # Phase 11 structured logs
        "session_id",
        "LOG_LEVEL",
        # references real metric families for the run-failure path
        "llmdbench_orchestrator_run_faults_total",
    )),
])
def test_operability_doc_has_expected_sections(doc_path, needles):
    text = _read(doc_path)
    for needle in needles:
        assert needle in text, f"{doc_path.name} missing expected content: {needle!r}"


def test_docs_index_links_the_new_docs():
    index = _read(DOCS / "README.md")
    for name in ("SECURITY.md", "TROUBLESHOOTING.md"):
        assert name in index, f"docs/README.md should link {name}"


# ===========================================================================
# The Prometheus alert-rules file is valid and references only real metrics
# ===========================================================================

def test_alert_rules_parse_as_prometheus_rule_yaml():
    doc = yaml.safe_load(_read(ALERTS))
    assert isinstance(doc, dict) and "groups" in doc, "rule file needs a top-level 'groups'"
    groups = doc["groups"]
    assert isinstance(groups, list) and groups, "expected a non-empty groups list"

    seen_alerts: list[str] = []
    for grp in groups:
        assert grp.get("name"), "every group needs a name"
        rules = grp.get("rules")
        assert isinstance(rules, list) and rules, f"group {grp.get('name')!r} has no rules"
        for rule in rules:
            # Each is an alerting rule with the mandatory alert + expr (Prometheus schema).
            assert "alert" in rule and rule["alert"], f"rule missing 'alert': {rule}"
            assert "expr" in rule and str(rule["expr"]).strip(), f"rule missing 'expr': {rule}"
            seen_alerts.append(rule["alert"])
            # 'for' (when present) must be a valid Prometheus duration.
            if "for" in rule:
                assert re.fullmatch(r"\d+[smhdwy]", str(rule["for"])), rule["for"]
            # Annotations should carry an operator-readable summary.
            ann = rule.get("annotations", {})
            assert ann.get("summary"), f"rule {rule['alert']!r} needs a summary annotation"
    # Alert names are unique and we shipped a meaningful set.
    assert len(seen_alerts) == len(set(seen_alerts)), f"duplicate alert names: {seen_alerts}"
    assert len(seen_alerts) >= 4, "expected several meaningful alert rules"


def test_alert_rules_reference_only_real_metric_names():
    text = _read(ALERTS)
    exported = _exported_metric_base_names()
    assert exported, "sanity: the app must export some llmdbench_* metrics"

    referenced: set[str] = set()
    # Only scan the actual rule expressions, not the explanatory header comments — a metric
    # named in a comment is documentation, while one in an expr is a real query dependency.
    doc = yaml.safe_load(text)
    for grp in doc["groups"]:
        for rule in grp["rules"]:
            for raw in _METRIC_TOKEN.findall(str(rule["expr"])):
                referenced.add(_normalize_metric_token(raw))

    assert referenced, "the rules should reference at least one llmdbench_* metric"
    unknown = referenced - exported
    assert not unknown, (
        f"alert rules reference metric(s) the app does not export: {sorted(unknown)}; "
        f"exported: {sorted(exported)}"
    )


def test_histogram_quantile_uses_a_real_histogram_metric():
    # The latency alert uses histogram_quantile over *_bucket; that base metric must really be a
    # Histogram in the app (otherwise the _bucket series wouldn't exist to query).
    text = _read(ALERTS)
    hist_names = _histogram_metric_names()
    # Find every *_bucket reference and confirm its base is a real histogram.
    for raw in _METRIC_TOKEN.findall(text):
        if raw.endswith("_bucket"):
            base = _normalize_metric_token(raw)
            assert base in hist_names, (
                f"{raw} is queried but {base} is not a Histogram in the app "
                f"(histograms: {sorted(hist_names)})"
            )


def test_alert_rules_scope_to_the_real_scrape_job():
    # The 'up' availability alert must scope to the job name the shipped scrape config uses,
    # or it would alert on (or miss) the wrong target.
    scrape = yaml.safe_load(
        (Path(PROJECT_ROOT) / "deploy" / "observability" / "prometheus-scrape.yaml").read_text()
    )
    job = scrape["scrape_configs"][0]["job_name"]
    text = _read(ALERTS)
    assert f'job="{job}"' in text, f"availability alert should scope to job={job!r}"


@pytest.mark.skipif(shutil.which("promtool") is None, reason="promtool not installed")
def test_promtool_validates_rules():
    out = subprocess.run(
        ["promtool", "check", "rules", str(ALERTS)],
        capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stdout + out.stderr
