"""Deterministic, knowledge/data-sourced start-of-chat + post-run cards (mechanism only).

Three sibling deterministic UI-content builders, each reshaping already-computed, data-/
knowledge-sourced facts into the flat render model its event carries — NO judgment, no LLM turn:
the start-of-chat welcome card (``knowledge/welcome.md``), the post-run results card (the Results
Analyzer's SLO/Pareto output), and the start-of-chat suggestion chips (``suggestions.yaml``). Each
section preserves its original module docstring verbatim as the comment block under its separator.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from app.config import Settings
from app.tools.context import ToolContext

# ── welcome ──────────────────────────────────────────────────────────────────
# Deterministic start-of-chat welcome (B2 / TODO #3).
#
# A FRESH chat opens with a code-emitted welcome card that concisely offers the assistant's
# capabilities — consistent every time, with NO LLM turn spent. The judgment text (heading,
# capability bullets, closing nudge) lives in ``knowledge/welcome.md`` so it stays editable and
# in the agent's own voice; THIS module is mechanism only: it parses that markdown into the flat
# ``{heading, bullets, nudge}`` shape the ``welcome`` event carries.
#
# Best-effort: a missing/garbled file, or a file lacking the expected sections, yields ``None``
# (the UI then falls back to its suggestion chips / plain note). Pure parsing — no judgment, no
# per-field knowledge baked in here.

_WELCOME_FILE = "welcome.md"


def build_welcome(ctx: ToolContext) -> dict[str, Any] | None:
    """Return the deterministic welcome payload ``{heading, bullets, nudge}`` parsed from
    ``knowledge/welcome.md``, or ``None`` when the file is missing/unreadable or carries no
    capability bullets (the one part the card cannot be useful without)."""
    path = ctx.settings.knowledge_dir / _WELCOME_FILE
    try:
        text = path.read_text()
    except OSError:
        return None
    return parse_welcome(text)


def parse_welcome(text: str) -> dict[str, Any] | None:
    """Parse the welcome markdown into ``{heading, bullets, nudge}``.

    Deterministic, section-driven (mechanism only): the LAST ``## `` heading is the card
    heading (the first ``## `` is the doc's own title), the ``- `` bullets under the
    ``### Capabilities`` section are the capabilities, and the first non-empty line under the
    ``### Nudge`` section is the closing nudge. Returns ``None`` when no capability bullets are
    found — the card is pointless without them. Heading/nudge degrade to ``""`` independently.
    """
    heading = ""
    bullets: list[str] = []
    nudge = ""
    section: str | None = None  # "capabilities" | "nudge" | None

    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith("### "):
            label = stripped[4:].strip().lower()
            section = "capabilities" if label == "capabilities" else "nudge" if label == "nudge" else None
            continue
        if stripped.startswith("## "):
            # A card heading (the FRESH-chat greeting), not the doc's own H1 title. The last
            # such heading wins so the leading explanatory ``## Welcome message …`` title that
            # documents the file is never shown to the user.
            heading = stripped[3:].strip()
            section = None
            continue
        if section == "capabilities" and stripped.startswith("- "):
            bullets.append(stripped[2:].strip())
        elif section == "nudge" and stripped and not nudge:
            nudge = stripped

    if not bullets:
        return None
    return {"heading": heading, "bullets": bullets, "nudge": nudge}


# ── results card ─────────────────────────────────────────────────────────────
# Deterministic post-run results card (B2 / TODO #3).
#
# After a run, the agent's prose summary is emergent (and varies turn to turn). To give the
# non-expert a CONSISTENT, structured view we also emit a ``results_card`` event built HERE —
# purely from the Results Analyzer's already-computed SLO/Pareto output
# (``app/validation/analysis.py``). No free-form prose, no fabricated numbers: the card carries
# only fields the analyzer produced.
#
# Scope note — why ONLY ``analyze_results`` and not ``locate_and_parse_report``: the single-run
# benchmark's structured view (the latency/throughput tiles, the percentile ladder, and the
# per-run chart thumbnails) is already rendered by the frontend's report-summary card
# (``renderReportSummary`` in ``ui/app.js``), driven directly from the same validated
# ``locate_and_parse_report`` result. Building a second card from that report here only duplicated
# those metrics in a separate, chart-less table — so we don't. This card adds the one thing the
# report-summary card does NOT carry: the analyzer's exact, deterministic SLO pass/fail verdicts
# (single run) and the Pareto frontier (sweep).
#
# This is mechanism — it reshapes already-computed, schema-validated facts into a flat render
# model. It makes NO judgment about whether a result is "good" (that stays the agent's prose,
# grounded in knowledge/results_interpretation.md + knowledge/analysis.md); the only verdicts it
# surfaces are the analyzer's own exact pass/fail SLO verdicts, which are deterministic given the
# report.
#
# ``build_results_card`` returns ``None`` when the tool result carries nothing renderable (a
# non-analysis tool, or an analysis with no valid run), so the loop simply doesn't emit a card —
# the agent's prose still stands on its own.


def build_results_card(tool_name: str, result: Any) -> dict[str, Any] | None:
    """Build the deterministic results card for an analyze_results tool result, or ``None``
    when there is nothing renderable.

    Single dispatch point (mechanism): the loop calls this after every tool result; only
    ``analyze_results`` yields a card (the single-run report's structured view is the frontend's
    report-summary card, so ``locate_and_parse_report`` deliberately yields ``None`` here to
    avoid duplicating it)."""
    if not isinstance(result, dict):
        return None
    if tool_name == "analyze_results":
        return _card_from_analysis(result)
    return None


def _card_from_analysis(result: dict[str, Any]) -> dict[str, Any] | None:
    """A card from an analyze_results result. One run -> a single-run card with SLO verdicts;
    a sweep -> a multi-run card with the per-run rows + the Pareto frontier."""
    if not result.get("analyzed"):
        return None
    runs = result.get("runs")
    if not isinstance(runs, list) or not runs:
        return None
    slo_targets = result.get("slo_targets")

    if len(runs) == 1:
        run = runs[0]
        # analyze_results doesn't re-embed the full summary on the run row, but DID compute the
        # exact SLO verdicts — surface those alongside whatever scalar metrics the verdicts and
        # standard metrics expose. The single-run latency/throughput table comes from the SLO
        # verdicts' observed values (which are derived from the validated report).
        card = _single_run_analysis_card(run)
        if slo_targets:
            card["slo_targets"] = slo_targets
        return card

    # Sweep: a comparison card.
    sweep_card: dict[str, Any] = {
        "kind": "sweep",
        "n": result.get("n") or len(runs),
        "runs": [{"label": r.get("label"), "model": r.get("model"),
                  "slo_met": (r.get("slo") or {}).get("overall_met")} for r in runs],
    }
    if slo_targets:
        sweep_card["slo_targets"] = slo_targets
    pareto = result.get("pareto")
    if isinstance(pareto, dict):
        sweep_card["frontier"] = pareto.get("frontier") or []
        sweep_card["slo_feasible"] = pareto.get("slo_feasible")
        sweep_card["objectives"] = [o.get("name") for o in (pareto.get("objectives") or [])]
    return sweep_card


def _single_run_analysis_card(run: dict[str, Any]) -> dict[str, Any]:
    """A single-run card from an analyze_results run row (which carries the exact SLO verdicts
    but not the full summary)."""
    card: dict[str, Any] = {
        "kind": "run",
        "model": run.get("model"),
        "run_uid": run.get("run_uid"),
    }
    slo = run.get("slo")
    if isinstance(slo, dict):
        card["slo"] = {
            "overall_met": slo.get("overall_met"),
            "checked_count": slo.get("checked_count"),
            "success_rate_pct": slo.get("success_rate_pct"),
            "goodput": slo.get("goodput"),
            # Each verdict is a deterministic, exact pass/fail at a stated statistic.
            "verdicts": [
                {"metric": v.get("metric"), "statistic": v.get("statistic"),
                 "direction": v.get("direction"), "target": v.get("target"),
                 "observed": v.get("observed"), "units": v.get("units"), "met": v.get("met")}
                for v in (slo.get("verdicts") or [])
            ],
        }
    if run.get("standard_metrics"):
        card["standard_metrics"] = run["standard_metrics"]
    if run.get("session_performance"):
        card["session_performance"] = run["session_performance"]
    return {k: v for k, v in card.items() if v is not None}


# ── suggestion chips ─────────────────────────────────────────────────────────
# Start-of-chat suggestion chips (DATA, no logic).
#
# The chips themselves live in ``suggestions.yaml`` beside this module — deliberately under
# ``app/agent/`` rather than ``knowledge/`` so they never leak into the system prompt or
# ``read_knowledge``. This loader is mechanism only: it reads the YAML and returns the flat
# ``chips`` list, filtered to well-formed ``{label, prompt}`` entries. Best-effort — a missing
# file, a parse error, or a wrong shape yields ``[]`` (the UI then falls back to its plain note).

_SUGGESTIONS_PATH = Path(__file__).with_name("suggestions.yaml")


def load_suggestions(settings: Settings) -> list[dict[str, str]]:
    """Return the start-of-chat chips as a list of ``{"label": ..., "prompt": ...}`` dicts.

    Best-effort: returns ``[]`` if the file is missing, unparseable, or the wrong shape, and
    drops any entry lacking both ``label`` and ``prompt`` (each coerced to ``str``)."""
    try:
        data = yaml.safe_load(_SUGGESTIONS_PATH.read_text())
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(data, dict):
        return []
    chips = data.get("chips")
    if not isinstance(chips, list):
        return []
    out: list[dict[str, str]] = []
    for chip in chips:
        if isinstance(chip, dict) and chip.get("label") and chip.get("prompt"):
            out.append({"label": str(chip["label"]), "prompt": str(chip["prompt"])})
    return out
