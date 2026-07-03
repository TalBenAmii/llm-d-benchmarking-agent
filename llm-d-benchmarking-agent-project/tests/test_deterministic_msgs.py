"""B2 deterministic/structured messages (TODO #3): the code-emitted welcome card and the
structured post-run results card. Hermetic — pure functions over hand-built data + the real
knowledge/welcome.md; no cluster, no LLM."""
from __future__ import annotations

from app.agent import cards as welcome
from app.agent import events
from app.agent.cards import build_results_card, build_welcome, parse_welcome


class FakeProvider:
    """A scripted provider: returns each prepared AssistantTurn in order (no API key)."""

    def __init__(self, turns):
        self._turns = turns
        self.i = 0

    async def chat(self, *, system, messages, tools, cache_key=None):
        turn = self._turns[self.i]
        self.i += 1
        return turn


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
        "pareto": {"frontier": ["a"], "slo_feasible": ["a"],
                   "objectives": [{"name": "ttft"}, {"name": "output_token_rate"}]},
        "skipped": [],
    }
    card = build_results_card("analyze_results", analysis)
    assert card is not None and card["kind"] == "sweep"
    assert card["n"] == 2
    assert card["frontier"] == ["a"]
    assert card["objectives"] == ["ttft", "output_token_rate"]
    assert {r["label"]: r["slo_met"] for r in card["runs"]} == {"a": True, "b": False}


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


# ---- loop wiring: the results card rides the turn --------------------------

async def test_loop_does_not_emit_results_card_for_report_tool(tmp_path):
    """The single-run report's structured view is the frontend report-summary card (rendered
    from the `tool_result`), so the loop must NOT also emit a `results_card` after
    locate_and_parse_report — doing so produced a duplicate, chart-less table card. The
    tool_result itself still rides the turn. Hermetic: simulate mode so the report tool
    synthesizes a labelled summary with no cluster/report on disk."""
    from app.agent.loop import AgentLoop
    from app.agent.session import Session
    from app.config import Settings
    from app.llm.provider import AssistantTurn, ToolCall
    from app.security.allowlist import Allowlist
    from app.security.runner import SimRunner
    from app.tools.context import ToolContext
    from tests.flows.catalog_snapshot import frozen_catalog

    settings = Settings(_env_file=None, simulate=True,
                        repos_dir=tmp_path / "repos", workspace_dir=tmp_path / "ws")
    al = Allowlist.from_file(settings.allowlist_path)
    ctx = ToolContext(settings=settings, allowlist=al, runner=SimRunner({}),
                      workspace=tmp_path / "ws" / "sessions" / "sim")
    frozen = frozen_catalog()
    ctx._catalog = frozen
    ctx.catalog = lambda *, refresh=False: frozen  # type: ignore[method-assign]
    session = Session(id="sim", ctx=ctx)

    turns = [
        AssistantTurn(text="Reading the report.",
                      tool_calls=[ToolCall("c1", "locate_and_parse_report", {})]),
        AssistantTurn(text="Here is what it means…", tool_calls=[]),
    ]

    emitted: list[tuple[str, dict]] = []

    async def emit(t, p):
        emitted.append((t, p))

    async def request_approval(kind, payload):
        return True

    loop = AgentLoop(FakeProvider(turns))
    await loop.run_turn(session, "how did the run do?", emit=emit, request_approval=request_approval)

    types = [t for t, _ in emitted]
    # The report tool's result rides the turn (the frontend renders its rich card from it)...
    assert "tool_result" in types, types
    # ...but NO deterministic results_card is emitted for it (that would duplicate the report card).
    assert "results_card" not in types, types
