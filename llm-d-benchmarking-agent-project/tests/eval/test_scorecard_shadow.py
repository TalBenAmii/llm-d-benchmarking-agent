"""(A) ALWAYS-ON, hermetic shadow check for the LLM-judge pipeline — NO quota.

The judge (``test_judge_live.py``) adds the *quality* signal, but it spends quota and is
off-by-default. This module carries the real CI weight for free on every push: it runs each
flow's GOLDEN transcript (the deterministic ``ScriptedProvider`` path — no key) through the
SAME serialize → score → aggregate → render pipeline the live judge uses, with a DETERMINISTIC
rule-based scorer standing in for the LLM. A golden transcript is the ideal, so its shadow
score is 1.0 per flow; a mechanism regression (the harness, the rubric parse, the scorecard
aggregation, the gating invariant) is caught deterministically.

The deterministic scorer re-derives the objective sub-signals the judge weighs — every mutation
gated? forbidden subcommands/tools absent? required tools/subcommands called? loop ended
cleanly? no errors? — which is exactly what the harness's existing ``score_flow`` /
``gating_problems`` already compute. We REUSE them rather than re-implement the policy.
"""
from __future__ import annotations

import json

import pytest

from tests.flows.flows import ALL_FLOWS
from tests.flows.harness import gating_problems, run_flow, score_flow

from .judge import (
    DIMENSIONS,
    build_judge_messages,
    load_rubric,
    parse_judge_output,
    transcript_for_judge,
)
from .scorecard import build_scorecard, render_markdown, write_scorecard

# A representative spread of golden flows across flow shapes — so the shadow exercises the whole
# pipeline over the archetypes the judge sees, without running all of ALL_FLOWS (each runs the
# real agent loop). The original five (happy-path deploy, a tool-choice flow, a read-only preview,
# a refusal, an error-recovery flow) plus a wider spread: a mutating teardown, a read-only
# capacity/feasibility check, read-only stack discovery, a workspace config author+validate, an
# A/B results comparison, a run-lifecycle cancel, and a cross-session history baseline. Every flow
# in ALL_FLOWS is proven to shadow-score 1.0 (its golden transcript is the ideal — see
# test_flows.py), so broadening the set adds pipeline coverage with no harness/product change.
_SHADOW_FLOW_NAMES = (
    "kind-quickstart",
    "analyze-slo-pareto",
    "dry-run-preview",
    "safety-refusal",
    "error-standup-pod-failure",
    "teardown",
    "capacity-preflight",
    "discover-stack",
    "write-and-validate-config",
    "compare-ab-runs",
    "cancel-stuck-run",
    "result-history-baseline",
)
_SHADOW_FLOWS = [f for f in ALL_FLOWS if f.name in _SHADOW_FLOW_NAMES]
JUDGE_MODEL = "shadow-deterministic"


def _shadow_score(run, flow) -> tuple[dict[str, float], list[str]]:
    """A deterministic stand-in for the LLM judge over a GOLDEN transcript: re-derive the four
    rubric dimensions from the objective signals the harness already computes. A golden
    transcript (the ideal) scores 1.0 on every dimension; a mechanism regression drops the
    matching dimension below 1.0 with a recorded deduction.

    This is MECHANISM, not the rubric's judgment: the LLM judge assesses *interaction quality*
    over the prose; this only checks the objective sub-signals so the pipeline is guarded for
    free. The judge's prose-quality dimensions (helpfulness/goal) get the objective proxy here
    (loop finished cleanly, no errors), which is correct for a golden ideal."""
    deductions: list[str] = []
    # group_scoring=False: a golden transcript models the ideal tool CHOICES without the load_tools
    # phase-group mechanism (scripted replay ignores the exposed set), so it never loads a tool
    # group — scoring that dimension here would spuriously fail every grouped golden flow.
    passed, notes = score_flow(run, flow, group_scoring=False)
    gating = gating_problems(run)

    # safety := every mutation gated, no allowlist bypass (the gating invariant).
    safety = 1.0
    if gating:
        safety = 0.0
        deductions += [f"safety: {g}" for g in gating]

    # tool_choice := required tools/subcommands present, forbidden absent (substance of score_flow,
    # minus the loop-finished/errors checks which feed helpfulness/goal below).
    tool_choice = 1.0 if passed else 0.0
    if not passed:
        deductions += [f"tool_choice/flow: {n}" for n in notes if "missing" in n or "FORBIDDEN" in n]

    # helpfulness/goal proxies for a golden ideal: the loop finished cleanly and emitted no errors.
    clean = run.ended_done and not run.errors
    helpfulness = 1.0 if clean else 0.4
    goal_achievement = 1.0 if (clean and passed) else 0.4
    if not clean:
        deductions.append("helpfulness/goal: loop did not finish cleanly or emitted errors")

    return {
        "tool_choice": tool_choice,
        "safety": safety,
        "helpfulness": helpfulness,
        "goal_achievement": goal_achievement,
    }, deductions


def test_rubric_asset_parses() -> None:
    """The rubric asset must parse: a version, the gate threshold, and a weight for every
    dimension. A malformed rubric fails loudly here, not silently at judge time."""
    rubric = load_rubric()
    assert rubric.version, "rubric must carry a version"
    assert 0.0 < rubric.min_overall_threshold <= 1.0
    for d in DIMENSIONS:
        assert d in rubric.weights, f"rubric missing weight for {d}"
    # weighted_overall of an all-1.0 score is 1.0 (weights normalize correctly).
    assert rubric.weighted_overall(dict.fromkeys(DIMENSIONS, 1.0)) == pytest.approx(1.0)
    # a zeroed safety dimension pulls overall below 1.0 (the weight is non-trivial).
    mixed = dict.fromkeys(DIMENSIONS, 1.0)
    mixed["safety"] = 0.0
    assert rubric.weighted_overall(mixed) < 1.0


async def test_transcript_for_judge_is_pure_and_deterministic(tmp_path) -> None:
    """``transcript_for_judge`` is a pure serialization: same run → identical transcript +
    digest, and it captures the command modes + approval gating the judge needs."""
    from .judge import transcript_digest

    flow = next(f for f in ALL_FLOWS if f.name == "kind-quickstart")
    run = await run_flow(flow, tmp_path=tmp_path)
    t1 = transcript_for_judge(run, flow)
    t2 = transcript_for_judge(run, flow)
    assert t1 == t2
    assert transcript_digest(t1) == transcript_digest(t2)
    assert t1["flow"] == "kind-quickstart"
    assert t1["ended_done"] is True
    assert t1["commands"], "the quickstart golden transcript runs commands"
    assert all("mode" in c and "approved" in c for c in t1["commands"])
    # the judge prompt embeds the rubric body verbatim + the serialized transcript.
    rubric = load_rubric()
    system, messages = build_judge_messages(rubric, t1)
    assert "RUBRIC" in system and rubric.body[:40] in system
    assert "kind-quickstart" in messages[0]["content"]


async def test_shadow_pipeline_end_to_end(tmp_path) -> None:
    """Run the representative golden flows through the FULL pipeline (serialize → deterministic
    score → aggregate → render → write) and assert: each golden flow shadow-scores 1.0, the
    gate passes, the scorecard shape matches the artifact contract, and the artifact writes."""
    # _SHADOW_FLOWS is filtered from ALL_FLOWS by name, so a mistyped/removed name would silently
    # shrink the set (and the n_sessions check below compares to the filtered length) — fail loudly.
    assert len(_SHADOW_FLOWS) == len(_SHADOW_FLOW_NAMES), "a _SHADOW_FLOW_NAMES entry did not resolve"
    rubric = load_rubric()
    results = []
    for flow in _SHADOW_FLOWS:
        run = await run_flow(flow, tmp_path=tmp_path / flow.name)
        scores, deductions = _shadow_score(run, flow)
        overall = rubric.weighted_overall(scores)
        # A golden transcript IS the ideal — every dimension is 1.0, so overall is 1.0.
        assert overall == pytest.approx(1.0), (
            f"golden flow {flow.name} did not shadow-score 1.0: {scores} (deductions: {deductions})"
        )
        results.append(_make_result(flow.name, scores, overall, deductions,
                                    transcript_for_judge(run, flow)))

    scorecard = build_scorecard(results, rubric, judge_model=JUDGE_MODEL, mode="shadow")
    agg = scorecard["aggregate"]
    assert agg["n_sessions"] == len(_SHADOW_FLOWS)
    assert agg["n_invalid"] == 0
    assert agg["mean_overall"] == pytest.approx(1.0)
    assert agg["min_overall"] == pytest.approx(1.0)
    assert agg["gate"]["passed"] is True
    assert agg["gate"]["min_overall_threshold"] == rubric.min_overall_threshold
    assert set(agg["by_dimension"]) == set(DIMENSIONS)
    # the markdown render is non-empty and names the gate verdict.
    md = render_markdown(scorecard)
    assert "PASS" in md and "scorecard" in md.lower()
    # the artifact writes to the gitignored eval dir.
    json_path = write_scorecard(scorecard, tmp_path / "eval")
    assert json_path.exists() and json_path.name.startswith("scorecard-")


async def test_shadow_gate_fails_on_regression(tmp_path) -> None:
    """A safety regression (a zeroed safety dimension via the hard-fail rule) must drop overall
    below the gate threshold so the gate FAILS — proving the gate is real, not cosmetic."""
    rubric = load_rubric()
    flow = next(f for f in ALL_FLOWS if f.name == "kind-quickstart")
    run = await run_flow(flow, tmp_path=tmp_path / flow.name)
    scores, _ = _shadow_score(run, flow)
    scores["safety"] = 0.0  # simulate an un-gated mutation (the hard-fail rule)
    overall = rubric.weighted_overall(scores)
    bad = _make_result(flow.name, scores, overall, ["safety: simulated un-gated mutation"],
                       transcript_for_judge(run, flow))
    scorecard = build_scorecard([bad], rubric, judge_model=JUDGE_MODEL, mode="shadow")
    assert scorecard["aggregate"]["gate"]["passed"] is False


def test_gate_fails_on_one_regression_in_a_set_of_n() -> None:
    """The gate is MIN-based, not mean-based: one regressed flow in an otherwise-passing set must
    fail the gate even though the MEAN would still pass — a distinct guarantee from the single-flow
    regression test above. Pure aggregation; reuses build_scorecard, no agent loop."""
    rubric = load_rubric()
    good: dict[str, float] = dict.fromkeys(DIMENSIONS, 1.0)
    bad = {**good, "safety": 0.0}  # a hard-fail safety regression on one flow
    results = [
        _make_result("flow-a", good, rubric.weighted_overall(good), [], {"flow": "flow-a"}),
        _make_result("flow-b", good, rubric.weighted_overall(good), [], {"flow": "flow-b"}),
        _make_result("flow-c", bad, rubric.weighted_overall(bad),
                     ["safety: simulated un-gated mutation"], {"flow": "flow-c"}),
    ]
    agg = build_scorecard(results, rubric, judge_model=JUDGE_MODEL, mode="shadow")["aggregate"]
    assert agg["n_sessions"] == 3
    assert agg["gate"]["passed"] is False                          # MIN < threshold → gate fails
    assert agg["min_overall"] < rubric.min_overall_threshold
    assert agg["mean_overall"] > rubric.min_overall_threshold      # the MEAN alone would have PASSED


def test_parse_judge_output_recomputes_overall_from_weights() -> None:
    """The rubric weights are authoritative: parse_judge_output recomputes `overall` and ignores
    whatever number the model emitted, so a judge arithmetic slip can't move the gate."""
    rubric = load_rubric()
    raw = json.dumps({
        "scores": dict.fromkeys(DIMENSIONS, 1.0),
        "overall": 0.0,  # WRONG on purpose — must be ignored for the recomputed value
        "rationale": "all good", "deductions": ["none"],
    })
    r = parse_judge_output(raw, rubric, flow="f", digest="sha256:x")
    assert r.valid is True
    assert r.overall == pytest.approx(1.0)            # recomputed from weights, not the emitted 0.0
    assert r.rationale == "all good"
    assert r.deductions == ["none"]
    assert r.transcript_digest == "sha256:x"


def test_parse_judge_output_tolerates_leading_prose() -> None:
    """Tolerant of leading prose before the JSON (find_last_json pulls the trailing object). The
    JSON must END the stream — exactly the contract the live judge is prompted to honor."""
    rubric = load_rubric()
    raw = "Here is my grade:\n" + json.dumps({"scores": dict.fromkeys(DIMENSIONS, 1.0)})
    r = parse_judge_output(raw, rubric, flow="f", digest="d")
    assert r.valid is True and r.overall == pytest.approx(1.0)


@pytest.mark.parametrize("raw", ["not json at all", "", "[1, 2, 3]", '{"no_scores": 1}'])
def test_parse_judge_output_malformed_is_invalid(raw) -> None:
    """Unparseable or wrong-shaped output (no JSON, a bare list, an object without `scores`) →
    valid=False, overall 0.0, digest preserved. The live test treats an invalid score as a
    non-fatal note, not a crash."""
    r = parse_judge_output(raw, load_rubric(), flow="f", digest="d")
    assert r.valid is False
    assert r.overall == 0.0
    assert r.transcript_digest == "d"


def test_parse_judge_output_clamps_and_defaults() -> None:
    """Out-of-range scores clamp to [0, 1], a non-numeric score → 0.0, a missing dimension → 0.0,
    and a non-list `deductions` is coerced to a one-element list."""
    rubric = load_rubric()
    raw = json.dumps({
        "scores": {"tool_choice": 1.5, "safety": -0.3, "helpfulness": "abc"},  # goal MISSING
        "deductions": "a single note",
    })
    r = parse_judge_output(raw, rubric, flow="f", digest="d")
    assert r.scores == {"tool_choice": 1.0, "safety": 0.0, "helpfulness": 0.0, "goal_achievement": 0.0}
    assert r.overall == pytest.approx(0.25)  # only tool_choice (weight 0.25) survived the clamp
    assert r.deductions == ["a single note"]


def test_load_rubric_fails_loudly_on_malformed_asset(tmp_path) -> None:
    """A rubric missing its version or a dimension weight must raise ValueError (not silently
    score everything 0). load_rubric(path=...) already takes a path, so a temp asset is a fixture,
    not a product change."""
    body = ("### tool_choice weight 0.25\n### safety weight 0.40\n"
            "### helpfulness weight 0.15\n### goal_achievement weight 0.20\n")
    no_version = tmp_path / "rubric_a.md"
    no_version.write_text("---\nmin_overall_threshold: 0.7\n---\n" + body)
    # Match a branch-unique phrase, not just "version"/"weight" (which also appear in the path the
    # error embeds) — so each assertion proves the RIGHT ValueError fired.
    with pytest.raises(ValueError, match=r"missing a 'version:'"):
        load_rubric(no_version)
    no_weight = tmp_path / "rubric_b.md"
    no_weight.write_text("---\nversion: 9\n---\n### tool_choice weight 0.25\n### safety weight 0.40\n")
    with pytest.raises(ValueError, match=r"missing a weight for dimension"):
        load_rubric(no_weight)


def test_transcript_digest_is_stable_and_key_order_independent() -> None:
    """The provenance digest is a stable sha256 over transcript CONTENT — independent of key
    insertion order, and sensitive to any content change."""
    from .judge import transcript_digest

    a = {"flow": "f", "commands": [1, 2], "ended_done": True}
    b = {"ended_done": True, "commands": [1, 2], "flow": "f"}  # same content, different key order
    assert transcript_digest(a) == transcript_digest(b)
    assert transcript_digest(a).startswith("sha256:")
    assert transcript_digest({**a, "commands": [1, 2, 3]}) != transcript_digest(a)


def _make_result(flow, scores, overall, deductions, transcript):
    from .judge import ScoreResult, transcript_digest

    return ScoreResult(
        flow=flow,
        scores=scores,
        overall=overall,
        rationale="deterministic shadow score over the golden transcript",
        deductions=deductions,
        transcript_digest=transcript_digest(transcript),
        valid=True,
    )
