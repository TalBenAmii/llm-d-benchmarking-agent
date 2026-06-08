"""(A) Scorecard — pure aggregation of per-session :class:`~tests.eval.judge.ScoreResult`s into
an aggregate AGENT-QUALITY SCORE, plus the artifact writer.

Pure mechanism: no LLM, no provider. The GATE THRESHOLD is read from the rubric asset (carried
on the :class:`~tests.eval.judge.Rubric`), NEVER hard-coded here. Artifacts land under the
gitignored ``workspace/eval/`` and are never committed by a test run.
"""
from __future__ import annotations

import json
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .judge import DIMENSIONS, Rubric, ScoreResult


def build_scorecard(
    results: list[ScoreResult],
    rubric: Rubric,
    *,
    judge_model: str,
    mode: str = "live",
) -> dict[str, Any]:
    """Aggregate judged sessions into the scorecard dict (see ``docs/VALIDATION.md`` for the
    shape). Pure: identical inputs → identical output (modulo ``generated_at``).

    ``gate.passed`` = (min per-session ``overall`` ≥ the rubric's ``min_overall_threshold``).
    Using the MIN (not the mean) means one badly-graded session fails the gate — a behavioral
    regression in any single flow is caught, not averaged away. An empty result set does NOT
    pass (nothing was scored)."""
    overalls = [r.overall for r in results]
    by_dim: dict[str, dict[str, float]] = {}
    for d in DIMENSIONS:
        vals = [r.scores.get(d, 0.0) for r in results if r.scores]
        by_dim[d] = {
            "mean": round(statistics.fmean(vals), 4) if vals else 0.0,
            "min": round(min(vals), 4) if vals else 0.0,
        }
    threshold = rubric.min_overall_threshold
    passed = bool(overalls) and min(overalls) >= threshold
    return {
        "rubric_version": rubric.version,
        "judge_model": judge_model,
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": mode,
        "aggregate": {
            "mean_overall": round(statistics.fmean(overalls), 4) if overalls else 0.0,
            "min_overall": round(min(overalls), 4) if overalls else 0.0,
            "n_sessions": len(results),
            "n_invalid": sum(1 for r in results if not r.valid),
            "by_dimension": by_dim,
            "gate": {"min_overall_threshold": threshold, "passed": passed},
        },
        "sessions": [
            {
                "flow": r.flow,
                "scores": r.scores,
                "overall": round(r.overall, 4),
                "rationale": r.rationale,
                "deductions": r.deductions,
                "valid": r.valid,
                "transcript_digest": r.transcript_digest,
            }
            for r in results
        ],
    }


def render_markdown(scorecard: dict[str, Any]) -> str:
    """A compact human-readable render of a scorecard dict (the ``.md`` artifact)."""
    agg = scorecard["aggregate"]
    gate = agg["gate"]
    lines = [
        f"# Agent-quality scorecard (rubric v{scorecard['rubric_version']})",
        "",
        f"- judge model: `{scorecard['judge_model']}`",
        f"- generated: {scorecard['generated_at']}",
        f"- mode: {scorecard['mode']}",
        f"- sessions: {agg['n_sessions']} ({agg['n_invalid']} unparseable)",
        f"- mean overall: **{agg['mean_overall']}** · min overall: **{agg['min_overall']}**",
        f"- GATE: threshold {gate['min_overall_threshold']} → "
        f"**{'PASS' if gate['passed'] else 'FAIL'}**",
        "",
        "## Per-dimension (mean / min)",
        "",
        "| dimension | mean | min |",
        "|---|---|---|",
    ]
    for dim, v in agg["by_dimension"].items():
        lines.append(f"| {dim} | {v['mean']} | {v['min']} |")
    lines += ["", "## Per-session", "", "| flow | overall | rationale |", "|---|---|---|"]
    for s in scorecard["sessions"]:
        rationale = (s["rationale"] or "").replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {s['flow']} | {s['overall']} | {rationale} |")
    return "\n".join(lines) + "\n"


def write_scorecard(scorecard: dict[str, Any], eval_dir: Path) -> Path:
    """Write ``scorecard-<ts>.json`` + ``.md`` under ``eval_dir`` (created if absent). Returns
    the JSON path. ``eval_dir`` is the gitignored ``workspace/eval/`` in live use."""
    eval_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    json_path = eval_dir / f"scorecard-{ts}.json"
    json_path.write_text(json.dumps(scorecard, indent=2, ensure_ascii=False))
    (eval_dir / f"scorecard-{ts}.md").write_text(render_markdown(scorecard))
    return json_path
