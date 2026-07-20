"""B2 deterministic/structured messages (TODO #3): the code-emitted welcome card and the
structured post-run results card. Hermetic — pure functions over hand-built data + the real
knowledge/welcome.md; no cluster, no LLM."""
from __future__ import annotations

from app.agent import cards as welcome
from app.agent import events
from app.agent.cards import build_results_card, build_welcome, parse_welcome

# ---- deterministic welcome -------------------------------------------------

def test_welcome_loads_from_knowledge_file(tool_ctx):
    """build_welcome reads the real knowledge/welcome.md and returns the structured payload."""
    w = build_welcome(tool_ctx)
    assert w is not None
    assert w["heading"] and "Benchmarking Assistant" in w["heading"]
    assert isinstance(w["bullets"], list) and len(w["bullets"]) >= 4
    # The capabilities the item names must be offered.
    joined = " ".join(w["bullets"]).lower()
    assert "deploy" in joined and "benchmark" in joined and "trend" in joined
    assert w["nudge"]


def test_welcome_is_deterministic(tool_ctx):
    """Same input -> identical payload every call (no per-turn variation)."""
    assert build_welcome(tool_ctx) == build_welcome(tool_ctx)


def test_parse_welcome_sections():
    md = (
        "# Doc title (the file's own H1, ignored)\n\n"
        "## Hi there — capabilities follow.\n\n"
        "### Capabilities\n"
        "- Do A.\n"
        "- Do B.\n\n"
        "### Nudge\n"
        "Pick something.\n"
    )
    w = parse_welcome(md)
    assert w == {"heading": "Hi there — capabilities follow.",
                 "bullets": ["Do A.", "Do B."], "nudge": "Pick something."}


def test_parse_welcome_no_bullets_returns_none():
    """Without capability bullets the card is pointless -> None (UI falls back to chips)."""
    assert parse_welcome("## Heading only\n\nno bullets here") is None


def test_build_welcome_missing_file_returns_none(tool_ctx, monkeypatch):
    monkeypatch.setattr(welcome, "_WELCOME_FILE", "does_not_exist.md")
    assert build_welcome(tool_ctx) is None


def test_welcome_is_a_non_turn_lifecycle_event():
    """The welcome frame is a connection-lifecycle frame, NOT buffered into the per-turn ring."""
    assert events.WELCOME in events.NON_TURN_EVENTS
    # The results card, by contrast, IS a turn event (so a mid-turn reconnect still catches it).
    assert events.RESULTS_CARD not in events.NON_TURN_EVENTS


# ---- structured results card -----------------------------------------------

def test_results_card_not_built_for_report_tool():
    """A locate_and_parse_report result yields NO results_card: the single-run report's
    metrics + charts are rendered by the frontend report-summary card (renderReportSummary),
    driven from this same validated result. Building a second, chart-less table card here only
    duplicated those numbers, so build_results_card deliberately returns None for this tool —
    regardless of whether the report was found/valid/simulated or carried charts."""
    summary = {
        "model": "facebook/opt-125m", "harness": "inference-perf", "run_uid": "uid-1",
        "duration": 30, "requests_total": 500, "requests_failures": 0,
        "success_rate_pct": 100.0,
        "latency": {"ttft": {"units": "ms", "mean": 120.0, "p99": 210.0}},
        "throughput": {"output_token_rate": {"units": "tokens/s", "mean": 4200.0}},
    }
    valid = {"found": True, "valid": True, "report_path": "/x/benchmark_report_v0.2.yaml",
             "summary": summary}
    assert build_results_card("locate_and_parse_report", valid) is None
    assert build_results_card("locate_and_parse_report",
                              {**valid, "simulated": True}) is None
    assert build_results_card("locate_and_parse_report",
                              {**valid, "charts": [{"path": "analysis/x.png", "title": "x"}]}) is None
    assert build_results_card("locate_and_parse_report", {"found": False}) is None
    assert build_results_card("locate_and_parse_report", {"found": True, "valid": False}) is None


def test_results_card_from_analysis_single_run_slo():
    """analyze_results single-run -> a card surfacing the exact SLO verdicts."""
    analysis = {
        "analyzed": True, "n": 1,
        "slo_targets": {"ttft_ms": 200.0, "percentile": "p99"},
        "runs": [{
            "label": "run1", "model": "m", "run_uid": "u",
            "slo": {
                "overall_met": True, "checked_count": 1, "success_rate_pct": 100.0,
                "goodput": {"estimate_pct": 98.5, "is_estimate": True},
                "verdicts": [{"metric": "ttft", "statistic": "p99", "direction": "max",
                              "target": 200.0, "observed": 150.0, "units": "ms", "met": True}],
            },
        }],
        "skipped": [],
    }
    card = build_results_card("analyze_results", analysis)
    assert card is not None and card["kind"] == "run"
    assert card["slo"]["overall_met"] is True
    assert card["slo"]["verdicts"][0]["met"] is True
    assert card["slo"]["goodput"]["estimate_pct"] == 98.5
    assert card["slo_targets"]["ttft_ms"] == 200.0


def test_results_card_from_analysis_sweep():
    analysis = {
        "analyzed": True, "n": 2,
        "runs": [
            {"label": "a", "model": "m", "slo": {"overall_met": True}},
            {"label": "b", "model": "m", "slo": {"overall_met": False}},
        ],
        # Both runs are mutually non-dominated (the usual concurrency-sweep shape), but only
        # "a" meets the SLOs -> the card must star the SLO-feasible frontier, not the raw one.
        "pareto": {"frontier": ["a", "b"], "slo_feasible": ["a"], "slo_frontier": ["a"],
                   "objectives": [{"name": "ttft"}, {"name": "output_token_rate"}]},
        "skipped": [],
    }
    card = build_results_card("analyze_results", analysis)
    assert card is not None and card["kind"] == "sweep"
    assert card["n"] == 2
    assert card["frontier"] == ["a"]
    assert card["frontier_basis"] == "slo_feasible"
    assert card["objectives"] == ["ttft", "output_token_rate"]
    assert {r["label"]: r["slo_met"] for r in card["runs"]} == {"a": True, "b": False}


def test_results_card_sweep_with_no_slo_feasible_run_falls_back_to_the_raw_frontier():
    """SLOs given but NO run meets them -> the analyzer's ``slo_frontier`` is EMPTY. Starring it
    would blank every ★ and leave the footnote explaining nothing, so the card falls back to the
    raw frontier and carries the analyzer's note to say why."""
    analysis = {
        "analyzed": True, "n": 2,
        "runs": [
            {"label": "a", "model": "m", "slo": {"overall_met": False}},
            {"label": "b", "model": "m", "slo": {"overall_met": False}},
        ],
        "pareto": {"frontier": ["a", "b"], "slo_feasible": [], "slo_frontier": [],
                   "note": "no run satisfies all SLO targets",
                   "objectives": [{"name": "ttft"}, {"name": "output_token_rate"}]},
        "skipped": [],
    }
    card = build_results_card("analyze_results", analysis)
    assert card is not None and card["frontier"] == ["a", "b"]
    assert card["frontier_basis"] == "no_slo_feasible"
    assert card["note"] == "no run satisfies all SLO targets"


def test_results_card_sweep_feasible_but_unrankable_is_not_no_slo_feasible():
    """SLOs given, runs DID meet them (``slo_feasible`` non-empty), but none is placeable on the
    frontier -> ``slo_frontier`` is EMPTY. This must NOT be labelled ``no_slo_feasible`` (which
    tells the user "no run met the SLO targets" — false here); it gets its own basis, and still
    falls back to the raw frontier so the trade-offs are shown."""
    analysis = {
        "analyzed": True, "n": 2,
        "runs": [
            {"label": "a", "model": "m", "slo": {"overall_met": True}},
            {"label": "b", "model": "m", "slo": {"overall_met": False}},
        ],
        # "a" passed the SLOs but carries no deciding latency objective, so it can't be ranked ->
        # slo_frontier empty while slo_feasible is not.
        "pareto": {"frontier": ["b"], "slo_feasible": ["a"], "slo_frontier": [],
                   "objectives": [{"name": "ttft"}, {"name": "output_token_rate"}]},
        "skipped": [],
    }
    card = build_results_card("analyze_results", analysis)
    assert card is not None
    assert card["frontier_basis"] == "slo_unrankable"
    assert card["frontier"] == ["b"]           # raw frontier fallback, so trade-offs still show
    assert "note" not in card                  # the analyzer sets no "nothing passed" note here


def test_results_card_sweep_without_slos_stars_the_raw_frontier():
    """No SLO targets -> no ``slo_frontier`` from the analyzer -> the card stars the raw one."""
    analysis = {
        "analyzed": True, "n": 2,
        "runs": [{"label": "a", "model": "m"}, {"label": "b", "model": "m"}],
        "pareto": {"frontier": ["a", "b"], "objectives": [{"name": "ttft"}]},
        "skipped": [],
    }
    card = build_results_card("analyze_results", analysis)
    assert card is not None and card["frontier"] == ["a", "b"]
    assert card["frontier_basis"] == "overall"
    assert "note" not in card


def test_results_card_forwards_degeneracy_of_the_frontier_it_stars():
    """A monotone sweep puts EVERY run on the frontier — correct, but the ★s then narrow nothing
    down. The card must carry that fact for the footnote, and must read the flag belonging to the
    frontier it actually stars (the SLO-restricted one here, which is NOT degenerate)."""
    analysis = {
        "analyzed": True, "n": 3,
        "runs": [
            {"label": "a", "model": "m", "slo": {"overall_met": True}},
            {"label": "b", "model": "m", "slo": {"overall_met": True}},
            {"label": "c", "model": "m", "slo": {"overall_met": False}},
        ],
        "pareto": {"frontier": ["a", "b", "c"], "frontier_degenerate": True,
                   "slo_feasible": ["a", "b"], "slo_frontier": ["a"],
                   "slo_frontier_degenerate": False,
                   "objectives": [{"name": "ttft"}, {"name": "output_token_rate"}]},
        "skipped": [],
    }
    card = build_results_card("analyze_results", analysis)
    assert card is not None and card["frontier_basis"] == "slo_feasible"
    # the OVERALL frontier is degenerate, but the starred one isn't -> must not claim it is
    assert card["frontier_degenerate"] is False


def test_results_card_degeneracy_without_slos_comes_from_the_raw_frontier():
    analysis = {
        "analyzed": True, "n": 3,
        "runs": [{"label": "a", "model": "m"}, {"label": "b", "model": "m"},
                 {"label": "c", "model": "m"}],
        "pareto": {"frontier": ["a", "b", "c"], "frontier_degenerate": True,
                   "objectives": [{"name": "ttft"}, {"name": "output_token_rate"}]},
        "skipped": [],
    }
    card = build_results_card("analyze_results", analysis)
    assert card is not None and card["frontier_basis"] == "overall"
    assert card["frontier_degenerate"] is True


def test_results_card_ignores_other_tools():
    """Only analyze_results produces a card; every other tool (including the report tool, whose
    structured view is the frontend report-summary card) returns None."""
    assert build_results_card("probe_environment", {"found": True, "summary": {}}) is None
    assert build_results_card("list_catalog", {"specs": []}) is None
    assert build_results_card("analyze_results", {"analyzed": False, "reason": "x"}) is None


def _analysis_result():
    return {
        "analyzed": True, "n": 1,
        "slo_targets": {"ttft_ms": 200.0, "percentile": "p99"},
        "runs": [{
            "label": "run1", "model": "m", "run_uid": "u",
            "slo": {"overall_met": True, "checked_count": 1, "success_rate_pct": 100.0,
                    "verdicts": [{"metric": "ttft", "statistic": "p99", "direction": "max",
                                  "target": 200.0, "observed": 150.0, "units": "ms", "met": True}]},
        }],
        "skipped": [],
    }


def test_results_card_is_deterministic():
    r = _analysis_result()
    assert build_results_card("analyze_results", r) == \
        build_results_card("analyze_results", r)


# ---- engine wiring: the results card rides the turn --------------------------

async def test_engine_does_not_emit_results_card_for_report_tool(tmp_path):
    """The single-run report's structured view is the frontend report-summary card (rendered
    from the `tool_result`), so the engine must NOT also emit a `results_card` after
    locate_and_parse_report — doing so produced a duplicate, chart-less table card. The
    tool_result itself still rides the turn. Hermetic: simulate mode so the report tool
    synthesizes a labelled summary with no cluster/report on disk."""
    from app.agent.engine import SdkNativeEngine
    from app.agent.session import Session
    from app.config import Settings
    from app.security.policy import CommandPolicy
    from app.security.runner import SimRunner
    from app.tools.context import ToolContext
    from app.tools.mcp_server import TOOL_PREFIX
    from tests._sdk_fake import FakeTransport, assistant, result, text, tool_use
    from tests.flows.catalog_snapshot import frozen_catalog

    settings = Settings(_env_file=None, simulate=True,
                        repos_dir=tmp_path / "repos", workspace_dir=tmp_path / "ws")
    al = CommandPolicy.from_file(settings.command_policy_path)
    ctx = ToolContext(settings=settings, policy=al, runner=SimRunner({}),
                      workspace=tmp_path / "ws" / "sessions" / "sim")
    frozen = frozen_catalog()
    ctx._catalog = frozen
    ctx.catalog = lambda *, refresh=False: frozen  # type: ignore[method-assign]
    session = Session(id="sim", ctx=ctx, catalog_injected=True)

    script = [[
        assistant(text("Reading the report."),
                  tool_use("c1", TOOL_PREFIX + "locate_and_parse_report", {})),
        assistant(text("Here is what it means…")),
        result(),
    ]]

    emitted: list[tuple[str, dict]] = []

    async def emit(t, p):
        emitted.append((t, p))

    async def request_approval(kind, payload):
        return True

    engine = SdkNativeEngine(transport_factory=lambda: FakeTransport(script))
    await engine.run_turn(session, "how did the run do?", emit=emit,
                          request_approval=request_approval)

    types = [t for t, _ in emitted]
    # The report tool's result rides the turn (the frontend renders its rich card from it)...
    assert "tool_result" in types, types
    # ...but NO deterministic results_card is emitted for it (that would duplicate the report card).
    assert "results_card" not in types, types
