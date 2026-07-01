"""autotune_search — the closed-loop autotuner's search-state tracker (MECHANISM ONLY).

Proposal: docs/history/proposals/01-autotuner.md. A single, action-dispatched tool (mirrors
``result_history``'s shape) the agent leans on so it doesn't hand-roll the search
bookkeeping in its context window. It:

  * ``record_trial``        — validate the trial's Benchmark Report (REUSE
                              ``app.validation.report``), summarize it, evaluate it against
                              the plan's SLO (REUSE ``evaluate_slo``), and append a Trial to
                              the per-search log. REFUSES an unvalidated report (gate d).
  * ``propose_next_config`` — PURE VALIDATION of the candidate the AGENT computed: in-bounds?
                              duplicate of a prior trial? budget left? It NEVER computes the
                              candidate value.
  * ``status``              — surface FACTS (incumbent, SLO-feasible frontier via REUSED
                              ``pareto_analysis``, budget remaining, recent-improvement delta,
                              whether the SLO boundary is bracketed). It returns NO
                              ``converged``/``stop`` verdict.

THE CARDINAL CONSTRAINT: the search STRATEGY (how to pick the next config) and the
CONVERGENCE decision ("stop?") are JUDGMENT — they live entirely in
``knowledge/autotune_strategy.md`` and are executed by the LLM. There is NO next-config
arithmetic and NO stop decision in this file. All actions auto-run (workspace read/write
only; no cluster, no repos), so there is no allowlist entry and no approval gate here.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.storage.autotune import (
    AutotuneStore,
    Trial,
    best_feasible,
    frontier_facts,
    recent_improvement_pct,
    slo_boundary_bracketed,
    valid_knob_key,
)
from app.tools.context import ToolContext

# name -> dotted summary path for the analyzer objective space (reused, not re-derived).
from app.validation.analysis import _OBJECTIVES as _ANALYZER_OBJECTIVES
from app.validation.analysis import (
    SLOTargets,
    _objective_value,  # reused: representative objective stat from a summary
    evaluate_slo,
)
from app.validation.report import (
    find_reports,
    load_report,
    summarize_report,
    validate_report,
)

# Objective name -> dotted summary path, built from the analyzer's own objective table so the
# autotuner and the Pareto analyzer always agree on what (e.g.) "output_token_rate" means.
_OBJECTIVE_PATHS: dict[str, str] = {name: path for path, name, _dir in _ANALYZER_OBJECTIVES}


def _store(ctx: ToolContext) -> AutotuneStore:
    """The autotune trial-log store, rooted at the SHARED workspace root (same resolution as
    the history store), so a search log is co-located with the session's other state."""
    ws = ctx.workspace
    root = ws.parent.parent if ws.parent.name == "sessions" else ws.parent
    return AutotuneStore(root)


def _slo_targets(slo: dict[str, Any] | None) -> tuple[SLOTargets | None, str | None]:
    """Build SLOTargets from the agent-supplied dict, or (None, error)."""
    if not slo:
        return None, None
    try:
        return SLOTargets(**slo), None
    except Exception as exc:  # pydantic validation (e.g. no targets) — surface, don't raise
        return None, f"invalid SLO targets: {exc}"


def _objective_value_for(summary: dict[str, Any], objective: str | None) -> float | None:
    """Representative value of the named objective from a summary (REUSES the analyzer's
    extraction). ``None`` when no objective is given or the metric is absent — never fabricated."""
    if not objective:
        return None
    path = _OBJECTIVE_PATHS.get(objective)
    if path is None:
        return None
    return _objective_value(summary, path)


async def _record_trial(
    ctx: ToolContext,
    *,
    search_id: str,
    config: dict[str, Any] | None,
    report_source: str | None,
    slo: dict[str, Any] | None,
    objective: str | None,
) -> dict[str, Any]:
    if not config:
        return {"recorded": False, "reason": "record_trial requires `config` (the knob values used)"}
    if not report_source:
        return {"recorded": False, "reason": "record_trial requires `report_source` (the run dir/report file)"}

    slo_targets, slo_err = _slo_targets(slo)
    if slo_err:
        return {"recorded": False, "reason": slo_err}

    p = Path(report_source)
    if p.is_file():
        report_path: Path | None = p
    else:
        found = find_reports([p], newest_only=True)
        report_path = found[0] if found else None
    if report_path is None:
        return {"recorded": False, "reason": f"no Benchmark Report found under {report_source!r}"}

    report = load_report(report_path)
    validation = validate_report(report, ctx.settings.benchmark_report_schema_path)
    if not validation.valid:
        # Never record a log-scraped / unvalidated number (determinism gate d).
        return {
            "recorded": False,
            "reason": "report failed schema validation — not recorded",
            "report_path": str(report_path),
            "errors": validation.errors[:5],
        }

    summary = summarize_report(report)
    slo_eval = evaluate_slo(summary, slo_targets) if slo_targets is not None else None
    objective_value = _objective_value_for(summary, objective)
    # Feasibility is the analyzer's verdict — NOT a threshold decided here.
    feasible = bool(slo_eval["overall_met"]) if slo_eval is not None else False

    store = _store(ctx)
    existing = store.load(search_id)
    trial = Trial(
        index=len(existing),
        config=dict(config),
        summary=summary,
        slo_eval=slo_eval,
        objective_value=objective_value,
        feasible=feasible,
        report_source=str(report_path),
    )
    store.append(search_id, trial)

    return {
        "recorded": True,
        "search_id": search_id,
        "trial_index": trial.index,
        "trials_used": trial.index + 1,
        "config": trial.config,
        "feasible": feasible,
        "objective": objective,
        "objective_value": objective_value,
        "slo": slo_eval,
        # The mechanism hands control straight back to the agent's judgment.
        "note": "trial recorded. Now reason about the NEXT config using "
                "read_knowledge('autotune_strategy'); validate it with action='propose_next_config'.",
    }


def _propose_next_config(
    ctx: ToolContext,
    *,
    search_id: str,
    candidate: dict[str, Any] | None,
    knobs: list[dict[str, Any]] | None,
    budget: int | None,
) -> dict[str, Any]:
    """PURE VALIDATION of the agent's candidate — bounds, duplicate, budget. No arithmetic,
    no next-value computation. Returns ``ok`` plus the reasons it is or isn't acceptable."""
    if not candidate:
        return {"ok": False, "reason": "propose_next_config requires `candidate` (the config YOU computed)"}

    store = _store(ctx)
    trials = store.load(search_id)
    trials_used = len(trials)

    budget_remaining: int | None = None
    if budget is not None:
        budget_remaining = max(budget - trials_used, 0)

    # 1) Budget exhausted — there is no trial left to spend.
    if budget_remaining is not None and budget_remaining <= 0:
        return {
            "ok": False,
            "reason": "budget exhausted — no trials remaining in this approved search",
            "budget_remaining": 0,
            "trials_used": trials_used,
        }

    # 2) Bounds + key well-formedness (reuses the DoE key vocabulary).
    bounds = _index_knobs(knobs)
    out_of_bounds: list[dict[str, Any]] = []
    unknown_keys: list[str] = []
    for key, value in candidate.items():
        if not valid_knob_key(key):
            return {
                "ok": False,
                "reason": f"candidate key {key!r} is not a valid dotted override key",
                "budget_remaining": budget_remaining,
                "trials_used": trials_used,
            }
        b = bounds.get(key)
        if b is None:
            unknown_keys.append(key)
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return {
                "ok": False,
                "reason": f"candidate value for {key!r} must be a number (got {value!r})",
                "budget_remaining": budget_remaining,
                "trials_used": trials_used,
            }
        if value < b["min"] or value > b["max"]:
            out_of_bounds.append({"key": key, "value": value, "min": b["min"], "max": b["max"]})
    if unknown_keys and bounds:
        return {
            "ok": False,
            "reason": f"candidate sets knob(s) not in the approved bounds: {unknown_keys}",
            "unknown_keys": unknown_keys,
            "budget_remaining": budget_remaining,
            "trials_used": trials_used,
        }
    if out_of_bounds:
        return {
            "ok": False,
            "reason": "candidate is outside the declared knob bounds — revise it",
            "out_of_bounds": out_of_bounds,
            "budget_remaining": budget_remaining,
            "trials_used": trials_used,
        }

    # 3) Duplicate of an already-run trial (same config content).
    dup_index = _duplicate_index(candidate, trials)
    if dup_index is not None:
        return {
            "ok": False,
            "reason": "candidate duplicates a config already tried — pick a new point",
            "duplicate_of": dup_index,
            "budget_remaining": budget_remaining,
            "trials_used": trials_used,
        }

    return {
        "ok": True,
        "candidate": candidate,
        "budget_remaining": budget_remaining,
        "trials_used": trials_used,
        "note": "candidate is well-formed, in-bounds, non-duplicate, and within budget — run it, "
                "then record_trial. Deciding whether to run another vs stop is yours "
                "(read_knowledge('autotune_strategy')).",
    }


def _index_knobs(knobs: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    """Build a {dotted key -> {min,max,...}} map from the plan's knob declarations. Tolerant
    of either dotted-key shape (the model_dump of an AutotuneKnob)."""
    out: dict[str, dict[str, Any]] = {}
    for k in knobs or []:
        if not isinstance(k, dict):
            continue
        key = k.get("key")
        if isinstance(key, str) and "min" in k and "max" in k:
            out[key] = {"min": k["min"], "max": k["max"], "resolution": k.get("resolution")}
    return out


def _duplicate_index(candidate: dict[str, Any], trials: list[Trial]) -> int | None:
    """The index of the first prior trial whose config equals the candidate, or None.
    Numbers compare by value (1 == 1.0) so an int/float restate isn't a false 'new' point."""
    cand = {k: _norm(v) for k, v in candidate.items()}
    for t in trials:
        if {k: _norm(v) for k, v in t.config.items()} == cand:
            return t.index
    return None


def _norm(value: Any) -> Any:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else value


def _status(
    ctx: ToolContext,
    *,
    search_id: str,
    slo: dict[str, Any] | None,
    objective: str | None,
    direction: str | None,
    budget: int | None,
) -> dict[str, Any]:
    """The convergence-FACT surface. Facts only — no verdict. (The result deliberately has
    NO ``converged``/``stop`` key; the stop decision is the agent's, per the knowledge doc.)"""
    slo_targets, slo_err = _slo_targets(slo)
    if slo_err:
        return {"error": slo_err}

    store = _store(ctx)
    trials = store.load(search_id)
    trials_used = len(trials)
    budget_remaining = max(budget - trials_used, 0) if budget is not None else None

    dir_ = direction or "max"
    incumbent = best_feasible(trials, direction=dir_)
    pareto = frontier_facts(trials, slo=slo_targets)

    best_feasible_out: dict[str, Any] | None = None
    if incumbent is not None:
        best_feasible_out = {
            "trial_index": incumbent.index,
            "config": incumbent.config,
            "objective_value": incumbent.objective_value,
            "slo_eval": incumbent.slo_eval,
        }

    return {
        "search_id": search_id,
        "trials_used": trials_used,
        "budget_remaining": budget_remaining,
        "objective": objective,
        "direction": dir_,
        "best_feasible": best_feasible_out,
        "slo_feasible_frontier": pareto.get("slo_frontier", []),
        "frontier": pareto.get("frontier", []),
        "recent_improvement_pct": recent_improvement_pct(trials, direction=dir_),
        "slo_boundary_bracketed": slo_boundary_bracketed(trials),
        "trials": [
            {
                "index": t.index,
                "config": t.config,
                "objective_value": t.objective_value,
                "feasible": t.feasible,
            }
            for t in trials
        ],
        "note": "FACTS only — decide converge vs continue using "
                "read_knowledge('autotune_strategy'). This tool returns no stop verdict.",
    }


async def autotune_search(
    ctx: ToolContext,
    *,
    action: str,
    search_id: str,
    slo: dict[str, Any] | None = None,
    objective: str | None = None,
    direction: str | None = None,
    config: dict[str, Any] | None = None,
    report_source: str | None = None,
    candidate: dict[str, Any] | None = None,
    knobs: list[dict[str, Any]] | None = None,
    budget: int | None = None,
) -> dict[str, Any]:
    if action == "record_trial":
        return await _record_trial(
            ctx, search_id=search_id, config=config, report_source=report_source,
            slo=slo, objective=objective,
        )
    if action == "propose_next_config":
        return _propose_next_config(
            ctx, search_id=search_id, candidate=candidate, knobs=knobs, budget=budget,
        )
    if action == "status":
        return _status(
            ctx, search_id=search_id, slo=slo, objective=objective,
            direction=direction, budget=budget,
        )
    return {
        "error": f"unknown action {action!r}",
        "valid_actions": ["record_trial", "propose_next_config", "status"],
    }
