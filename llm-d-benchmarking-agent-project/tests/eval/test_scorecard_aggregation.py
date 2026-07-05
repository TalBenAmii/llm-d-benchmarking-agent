"""scorecard.build_scorecard aggregation + render — the gate paths the shadow scorer skips.

test_scorecard_shadow.py exercises the all-golden PASS path and one MIN-based regression;
these guard the other aggregation contracts: an EMPTY result set must NOT pass (nothing
scored != pass), invalid results are counted, and render_markdown emits PASS/FAIL and
escapes pipes/newlines in a rationale. Hermetic, sibling-independent (synthetic Rubric,
no file I/O, no agent loop).
"""
from __future__ import annotations

from tests.eval.judge import DIMENSIONS, Rubric, ScoreResult
from tests.eval.scorecard import build_scorecard, render_markdown

_RUBRIC = Rubric(
    version="test", min_overall_threshold=0.7,
    weights=dict.fromkeys(DIMENSIONS, 0.25), body="",
)


def _result(flow, overall, *, valid=True, rationale=""):
    return ScoreResult(
        flow=flow, scores=dict.fromkeys(DIMENSIONS, overall), overall=overall,
        rationale=rationale, valid=valid,
    )


def _gate(results):
    return build_scorecard(results, _RUBRIC, judge_model="unit")["aggregate"]


def test_empty_result_set_does_not_pass():
    """Nothing scored is NOT a pass (the bool(overalls) guard)."""
    agg = _gate([])
    assert agg["gate"]["passed"] is False
    assert agg["n_sessions"] == 0


def test_all_above_threshold_passes():
    """Every session at/above the threshold passes the MIN-based gate."""
    assert _gate([_result("a", 1.0), _result("b", 0.8)])["gate"]["passed"] is True


def test_one_below_threshold_fails_the_gate():
    """A single below-threshold session fails the whole gate (MIN, not mean)."""
    assert _gate([_result("a", 1.0), _result("b", 0.5)])["gate"]["passed"] is False


def test_invalid_results_are_counted():
    """valid=False results are surfaced via n_invalid."""
    assert _gate([_result("a", 1.0), _result("b", 0.0, valid=False)])["n_invalid"] == 1


def test_render_markdown_emits_pass_and_fail():
    """render_markdown renders the gate verdict as **PASS** / **FAIL**."""
    passing = build_scorecard([_result("a", 1.0)], _RUBRIC, judge_model="unit")
    failing = build_scorecard([_result("a", 0.0)], _RUBRIC, judge_model="unit")
    assert "**PASS**" in render_markdown(passing)
    assert "**FAIL**" in render_markdown(failing)


def test_render_markdown_escapes_pipe_and_newline_in_rationale():
    """A rationale's pipes/newlines are escaped so they can't break the markdown table."""
    sc = build_scorecard(
        [_result("a", 1.0, rationale="has | pipe\nand newline")], _RUBRIC, judge_model="unit",
    )
    md = render_markdown(sc)
    assert "\\|" in md
    assert "pipe and newline" in md
