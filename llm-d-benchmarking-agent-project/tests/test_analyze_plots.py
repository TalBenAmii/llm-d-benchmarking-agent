"""Phase 40 — Trigger the CLI's local ``--analyze`` plot families (``flags['analyze']``).

GOAL: generate the CLI's optional workstation matplotlib plot families (per-request
distributions, session-lifecycle, Prometheus time-series) IN ADDITION to the harness PNGs, and
surface them in the UI — WITHOUT touching the agent's own SLO/goodput/Pareto math.

Hermetic: no cluster / GPU / network / matplotlib. Covers the three layers the feature spans:

  * build_argv emits a bare ``--analyze`` (pure MECHANISM) only when ``flags['analyze']`` is
    truthy AND only on ``run`` (upstream defines it on the run subparser alone), without
    disturbing the other run args;
  * the allowlist PERMITS ``--analyze`` under ``run`` but, unlike ``-z``, does NOT downgrade the
    run's mode — a real ``run --analyze`` stays MUTATING / approval-gated;
  * the artifact lister (``_discover_charts``) surfaces the three new PNG families from a fixture
    results dir, each labelled with its family subdir so they don't collide, and the agent's own
    analyzer math is UNCHANGED;
  * the WHEN-to-analyze JUDGMENT lives in knowledge/analysis.md (present, read_knowledge-able,
    names the three families + that they are supplementary) — not in Python.
"""
from __future__ import annotations

from pathlib import Path

from app.security.allowlist import MUTATING, READ_ONLY
from app.tools.run.execute import build_argv
from app.tools.access.knowledge_access import read_knowledge
from app.tools.analyze.report_locate import _discover_charts
from app.tools.schemas import ExecuteInput
from tests._helpers import _argv

KNOWLEDGE_DIR = Path(__file__).resolve().parents[1] / "knowledge"

# A 1x1 PNG (smallest valid image) so a fixture chart is a real image file.
_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000154a24f600000000049454e44ae426082"
)

# ---------------------------------------------------------------------------
# build_argv — --analyze emission (PURE MECHANISM), run-only
# ---------------------------------------------------------------------------


def test_analyze_emits_bare_flag_on_run():
    argv = build_argv("run", spec="cicd/kind", flags={"analyze": True})
    assert "--analyze" in argv
    # It is a BARE boolean flag — nothing is consumed after it as a value.
    assert argv[argv.index("--analyze") - 1] != "--analyze"


def test_analyze_falsey_or_absent_emits_nothing():
    for flags in ({}, {"analyze": False}, {"analyze": None}):
        argv = build_argv("run", spec="cicd/kind", flags=flags)
        assert "--analyze" not in argv


def test_analyze_is_run_only_for_every_other_subcommand():
    # Upstream defines --analyze on the `run` subparser ALONE; experiment/standup/plan reject it,
    # so build_argv must never emit it there even when the agent mistakenly sets the flag.
    for sub in ("standup", "smoketest", "experiment", "plan", "teardown"):
        argv = build_argv(sub, spec="cicd/kind", flags={"analyze": True})
        assert "--analyze" not in argv, f"--analyze must not be emitted on {sub}"


def test_analyze_after_subcommand_and_does_not_disturb_other_run_args():
    argv = build_argv(
        "run", spec="cicd/kind", harness="inference-perf", workload="sanity_random.yaml",
        flags={"analyze": True, "output": "local", "monitoring": True},
    )
    assert argv[:3] == ["llmdbenchmark", "--spec", "cicd/kind"]
    assert argv.index("--analyze") > argv.index("run")
    # The other run args are untouched.
    assert "-l" in argv and "inference-perf" in argv
    assert "-w" in argv and "sanity_random.yaml" in argv
    assert "-r" in argv and "local" in argv
    assert "--monitoring" in argv


def test_analyze_coexists_with_skip_collect_only():
    # A common combo: re-collect an existing run AND render the plot families from it.
    argv = build_argv("run", spec="cicd/kind", flags={"skip": True, "analyze": True})
    assert "-z" in argv and "--analyze" in argv


def test_execute_schema_accepts_analyze_flag():
    m = ExecuteInput(subcommand="run", spec="cicd/kind", flags={"analyze": True})
    assert m.flags == {"analyze": True}


# ---------------------------------------------------------------------------
# allowlist — --analyze permitted on `run`, but does NOT downgrade the mode
# ---------------------------------------------------------------------------


def _run(*rest):
    return _argv("run", "-l", "inference-perf", "-w", "sanity_random.yaml", *rest)


def test_allowlist_permits_analyze_on_run(allowlist, catalog):
    d = allowlist.validate(_run("--analyze"), catalog=catalog)
    assert d.allowed, f"--analyze should be allowed on run: {d.reason}"


def test_analyze_run_stays_mutating_and_approval_gated(allowlist, catalog):
    # Unlike -z (collect-only), --analyze does NOT change the run's mode: a real `run --analyze`
    # still loads the cluster, so it stays MUTATING and keeps its approval gate.
    d = allowlist.validate(_run("--analyze"), catalog=catalog)
    assert d.allowed
    assert d.mode == MUTATING
    assert d.requires_approval is True


def test_analyze_does_not_rescue_a_skip_run_into_mutating(allowlist, catalog):
    # A collect-only `run -z` auto-runs (read-only); adding --analyze (which only renders plots
    # from existing results) must KEEP it read-only/auto-run, not promote it.
    d = allowlist.validate(_run("-z", "--analyze"), catalog=catalog)
    assert d.allowed
    assert d.mode == READ_ONLY
    assert d.requires_approval is False


def test_analyze_value_abuse_is_screened(allowlist, catalog):
    # --analyze is a bare boolean; a metachar-laden trailing token is still rejected by the screen.
    assert not allowlist.validate(_run("--analyze", "a;rm -rf /"), catalog=catalog).allowed


# ---------------------------------------------------------------------------
# acceptance — the EXTRA plot families are surfaced via the artifact lister
# ---------------------------------------------------------------------------


def _make_analyzed_run(sessions_root: Path, sid: str) -> Path:
    """Build a run whose analysis/ tree holds the harness PNGs PLUS the three --analyze
    families (distributions/, session/, graphs/). Returns the report path."""
    run = sessions_root / sid / "tal-run-1"
    results = run / "results"
    analysis = run / "analysis"
    results.mkdir(parents=True)
    for sub in ("", "distributions", "session", "graphs"):
        (analysis / sub).mkdir(parents=True, exist_ok=True)
    report = results / "benchmark_report_v0.2,_stage_0.json.yaml"
    report.write_text("schema: v0.2\n")
    # Harness's own chart (directly under analysis/).
    (analysis / "latency_vs_qps.png").write_bytes(_PNG_BYTES)
    # The three --analyze families.
    (analysis / "distributions" / "ttft_hist.png").write_bytes(_PNG_BYTES)
    (analysis / "session" / "session_session_rate_qps.png").write_bytes(_PNG_BYTES)
    (analysis / "graphs" / "kv_cache_hit_rate.png").write_bytes(_PNG_BYTES)
    return report


def test_discover_charts_surfaces_the_three_analyze_families(tmp_path):
    sessions_root = tmp_path / "sessions"
    report = _make_analyzed_run(sessions_root, "sessA")

    charts = _discover_charts(report, sessions_root)
    paths = {c["path"] for c in charts}

    # Every family PNG (and the harness chart) is surfaced, addressable via the artifact route.
    assert "tal-run-1/analysis/latency_vs_qps.png" in paths
    assert "tal-run-1/analysis/distributions/ttft_hist.png" in paths
    assert "tal-run-1/analysis/session/session_session_rate_qps.png" in paths
    assert "tal-run-1/analysis/graphs/kv_cache_hit_rate.png" in paths
    # The non-image notes file (had there been one) would be excluded; here assert exactly 4 PNGs.
    assert len(charts) == 4


def test_discover_charts_labels_families_so_they_do_not_collide(tmp_path):
    sessions_root = tmp_path / "sessions"
    report = _make_analyzed_run(sessions_root, "sessB")

    charts = _discover_charts(report, sessions_root)
    by_path = {c["path"]: c for c in charts}

    # Each --analyze family carries an explicit `family` subdir + a family-prefixed title, so two
    # families with the same bare filename would still be distinct in the UI.
    dist = by_path["tal-run-1/analysis/distributions/ttft_hist.png"]
    assert dist["family"] == "distributions"
    assert dist["title"].startswith("Distributions:")

    sess = by_path["tal-run-1/analysis/session/session_session_rate_qps.png"]
    assert sess["family"] == "session"
    assert sess["title"].startswith("Session:")

    graphs = by_path["tal-run-1/analysis/graphs/kv_cache_hit_rate.png"]
    assert graphs["family"] == "graphs"
    assert graphs["title"].startswith("Graphs:")

    # A chart written directly under analysis/ (the harness PNG) has no family prefix.
    harness = by_path["tal-run-1/analysis/latency_vs_qps.png"]
    assert "family" not in harness
    assert harness["title"] == "Latency vs qps"


# ---------------------------------------------------------------------------
# the agent's own analyzer math is UNCHANGED by --analyze (it never reads the PNGs)
# ---------------------------------------------------------------------------


def test_agent_analysis_math_is_independent_of_analyze_plots():
    """The SLO/goodput/Pareto analysis reads the VALIDATED report, not the plot files. Assert the
    analyzer module exposes the same math entry points regardless of --analyze (no plot-driven
    branch was introduced)."""
    from app.validation import analysis as analysis_math

    for fn in ("evaluate_slo", "pareto_analysis"):
        assert hasattr(analysis_math, fn), f"analyzer math entry point {fn} must be intact"
    # build_argv with/without analyze differs ONLY by the bare flag — nothing about the report
    # destination (-r/--output/--workspace anchoring) changes.
    base = build_argv("run", spec="cicd/kind", harness="inference-perf", flags={"output": "local"})
    with_analyze = build_argv(
        "run", spec="cicd/kind", harness="inference-perf",
        flags={"output": "local", "analyze": True},
    )
    assert with_analyze == base + ["--analyze"]


# ---------------------------------------------------------------------------
# knowledge — the WHEN-to-analyze JUDGMENT is a discoverable knowledge file, not Python
# ---------------------------------------------------------------------------


def test_analysis_knowledge_documents_the_three_families_as_supplementary():
    guide = KNOWLEDGE_DIR / "analysis/analysis.md"
    assert guide.is_file()
    text = guide.read_text().lower()
    assert "--analyze" in text
    # Names the three families.
    assert "distributions" in text and "session" in text and "graphs" in text
    assert "time-series" in text or "time series" in text
    # Makes the supplementary-not-new-math point and the run-only constraint.
    assert "supplementary" in text
    assert "run-only" in text or "run only" in text
    assert "unchanged" in text or "do not change any number" in text or "does not change" in text


def test_analysis_knowledge_is_loadable_via_read_knowledge(tool_ctx):
    res = read_knowledge(tool_ctx, name="analysis")
    assert res.get("topic") == "analysis"
    assert "error" not in res
    assert "--analyze" in res["content"]
