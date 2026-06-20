"""Autotune trial-log storage — the pure, append-only search-state tracker for the
closed-loop goal-seeking autotuner (proposal docs/proposals/01-autotuner.md).

This is **MECHANISM ONLY**, the autotuner's analogue of ``app/storage/history.py``:
it reads/appends a per-search trial log under the session workspace and computes the
SLO-feasible frontier **by reusing** ``app.validation.analysis.pareto_analysis`` verbatim.

It contains ZERO benchmarking judgment:
  * it does NOT compute the next config (no bisection midpoint, no gradient step),
  * it does NOT decide whether the search has converged (no ``converged``/``stop`` verdict),
  * it does NOT pick a knob, a strategy, or a stop threshold.

The search STRATEGY (how to pick the next candidate) and the CONVERGENCE decision
("stop?") are JUDGMENT — they live entirely in ``knowledge/autotune_strategy.md`` and are
executed by the LLM. The functions here only:
  * record a trial keyed to its already-validated report summary + SLO evaluation,
  * validate that an agent-proposed candidate is well-formed, in-bounds, non-duplicate,
    and within budget (a pure check — it never modifies the candidate),
  * surface FACTS for the agent to reason over (incumbent, feasible frontier, budget
    remaining, recent-improvement delta, whether the SLO boundary is bracketed).

The bracketed/improvement facts are exactly that — FACTS, not a decision. Python must
never return ``converged: true``; the threshold + rubric live in the knowledge doc.

Storage model: ``<workspace>/autotune/<search_id>.json`` — an append-only trial log
(same defensive, best-effort I/O pattern as :class:`~app.storage.history.HistoryStore`).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.validation.analysis import SLOTargets, pareto_analysis

# Reuse the DoE dotted-key regex VERBATIM so a knob `key` follows the same vocabulary the
# DoE generator / scenario authoring already validate (e.g. ``decode.parallelism.tensor``).
from app.validation.doe import _KEY_RE

# A search id is used to build a filesystem path, so constrain it to a safe token.
_SEARCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")


@dataclass
class AutotuneKnobBound:
    """The declared search bound for ONE knob, mirrored from the approved AutotunePlan.
    Mechanism only: it is the box the agent's candidate must fall inside. Picking the box
    is the agent's judgment (knowledge/autotune_strategy.md + knowledge/sweep_playbook.md)."""

    name: str
    key: str
    min: float
    max: float
    resolution: float | None = None


@dataclass
class Trial:
    """One recorded trial: the config used, the validated report summary, the SLO verdict,
    the objective value, and whether the run was SLO-feasible. Every number here derives
    from a schema-validated Benchmark Report (determinism gate d) — never scraped."""

    index: int
    config: dict[str, Any]
    summary: dict[str, Any]              # summarize_report() output (already validated)
    slo_eval: dict[str, Any] | None      # evaluate_slo() output, or None when no SLO set
    objective_value: float | None        # the objective metric's representative value
    feasible: bool                       # SLO-feasible (overall_met); False when no SLO/missing
    report_source: str | None = None
    recorded_at: float = field(default_factory=time.time)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def safe_search_id(search_id: str | None) -> bool:
    return isinstance(search_id, str) and bool(_SEARCH_ID_RE.fullmatch(search_id))


def valid_knob_key(key: str) -> bool:
    """A knob's dotted override key reuses the DoE key vocabulary."""
    return isinstance(key, str) and bool(_KEY_RE.fullmatch(key))


def _as_num(v: Any) -> float:
    """Crash-proof sort key: the value if a real number, else 0.0. ``Trial`` is built from on-disk
    JSON with no type-check (the dataclass annotation isn't enforced), so a corrupt log with a
    non-numeric ``index`` (null/string) would make ``load()``'s ``trials.sort(...)`` raise
    ``TypeError`` — violating the documented "a corrupt log degrades to empty, never crashes"
    contract. ``bool`` is excluded (an ``int`` subclass, never a valid index)."""
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


class AutotuneStore:
    """Append/read the per-search trial log under ``<root>/autotune``.

    All I/O is best-effort and defensive (a corrupt log degrades to empty, never crashes
    the agent), mirroring :class:`~app.storage.history.HistoryStore`. The store touches no
    cluster and no repo — only the session workspace."""

    def __init__(self, root: Path):
        self._dir = Path(root) / "autotune"

    @property
    def dir(self) -> Path:
        return self._dir

    def _path(self, search_id: str) -> Path:
        return self._dir / f"{search_id}.json"

    def load(self, search_id: str) -> list[Trial]:
        """All recorded trials for a search, in recorded order (oldest first). Empty when
        the search has no log yet or the log is unreadable."""
        if not safe_search_id(search_id):
            return []
        try:
            data = json.loads(self._path(search_id).read_text())
        except (OSError, json.JSONDecodeError):
            return []
        raw = data.get("trials") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            return []
        trials: list[Trial] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            known: dict[str, Any] = {k: item.get(k) for k in Trial.__dataclass_fields__}
            try:
                trials.append(Trial(**known))
            except TypeError:
                continue
        trials.sort(key=lambda t: _as_num(t.index))
        return trials

    def append(self, search_id: str, trial: Trial) -> None:
        """Append one trial to the search's log (atomically-ish: temp then replace)."""
        trials = self.load(search_id)
        trials.append(trial)
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path(search_id)
        payload = {"search_id": search_id, "trials": [t.to_json() for t in trials]}
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, default=str))
        tmp.replace(path)


# ---- pure facts over a trial log (no verdicts) -----------------------------


def frontier_facts(
    trials: list[Trial], *, slo: SLOTargets | None
) -> dict[str, Any]:
    """Compute the Pareto + SLO-feasible frontier over the recorded trials by REUSING
    ``pareto_analysis`` verbatim. Returns the analyzer output (or an empty-shaped dict
    when fewer than two trials exist — pareto needs two). Facts only; no recommendation."""
    if len(trials) < 2:
        return {"objectives": [], "runs": [], "frontier": []}
    entries = [
        {"label": _trial_label(t), "summary": t.summary} for t in trials
    ]
    return pareto_analysis(entries, slo=slo)


def _trial_label(trial: Trial) -> str:
    """A stable, human-ish label for a trial built from its config (reused as the
    pareto/analyzer ``label``). Pure formatting — no judgment."""
    if trial.config:
        parts = [f"{k}={v}" for k, v in sorted(trial.config.items())]
        return "; ".join(parts)
    return f"trial{trial.index}"


def best_feasible(trials: list[Trial], *, direction: str) -> Trial | None:
    """The incumbent: the SLO-feasible trial with the best objective value for the search
    DIRECTION ('max' or 'min'). Pure selection over recorded facts — it does NOT decide
    whether the search is done, only which feasible trial currently leads. ``None`` when no
    feasible trial has an objective value."""
    candidates = [t for t in trials if t.feasible and t.objective_value is not None]
    if not candidates:
        return None
    chooser = max if direction == "max" else min

    def _obj(t: Trial) -> float:
        # candidates is pre-filtered to objective_value is not None.
        return float(t.objective_value or 0.0)

    return chooser(candidates, key=_obj)


def recent_improvement_pct(trials: list[Trial], *, direction: str, window: int = 2) -> float | None:
    """The objective delta (%) across the last ``window`` SLO-feasible trials, signed so a
    POSITIVE value always means "improving in the search direction".

    This is a FACT the agent reads against its convergence rubric — it is NOT a stop
    decision. The threshold ('treat <~5% as diminishing returns') lives in
    knowledge/autotune_strategy.md, and the decision is the LLM's. ``None`` when there
    aren't ``window`` feasible trials with objective values to compare."""
    feasible = [t for t in trials if t.feasible and t.objective_value is not None]
    if len(feasible) < window or window < 2:
        return None
    prev = feasible[-window].objective_value
    last = feasible[-1].objective_value
    if prev is None or last is None or prev == 0:
        return None
    raw_pct = 100.0 * (last - prev) / abs(prev)
    # Sign so positive == improvement regardless of whether bigger or smaller is better.
    signed = raw_pct if direction == "max" else -raw_pct
    return round(signed, 4)


def slo_boundary_bracketed(trials: list[Trial]) -> bool:
    """FACT: do we have BOTH >=1 SLO-feasible and >=1 SLO-infeasible trial? (i.e. the SLO
    crossing is bracketed by the trials so far). This is a fact the agent uses when deciding
    to bisect/stop — it is NOT itself a stop decision."""
    has_feasible = any(t.feasible for t in trials)
    has_infeasible = any(not t.feasible for t in trials)
    return has_feasible and has_infeasible
