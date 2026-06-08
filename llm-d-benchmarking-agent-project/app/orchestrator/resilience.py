"""Resilience report — the PURE JOIN of injected ⋈ classified ⋈ recovered.

The report is the proof artifact of the drill: it cross-references what the chaos decorator
INJECTED (the authoritative :class:`~app.orchestrator.chaos.FaultLedger`) against what the
UNMODIFIED controller did about it (the ``RunOutcome.attempts``, each carrying the *classified*
fault and the recovery the unchanged retry/dead-letter rule took), plus the restart-durability
:class:`~app.orchestrator.restart.RestartProof` and the SLO budget.

Mechanism only — a pure join + a couple of deterministic predicates (classified-correctly,
recovered-as-designed, SLO-met). It states FACTS; the VERDICT prose ("is this resilient
enough? what to fix?") is the agent's judgment, grounded in ``knowledge/resilience.md``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.orchestrator.faults import EVICTED, UNKNOWN

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.orchestrator.chaos import FaultLedger
    from app.orchestrator.controller import RunOutcome
    from app.orchestrator.restart import RestartProof

# The unmodified controller's retry rule (controller.py:DEFAULT_RETRYABLE). Mirrored here ONLY
# to compute the "recovered as designed?" predicate for the report — it does NOT drive any
# control flow (the real decision was already made by the unchanged run_with_retries).
_RETRYABLE = frozenset({EVICTED, UNKNOWN})

# Recovery actions surfaced on a per-fault row.
ACTION_RETRY = "retry"
ACTION_DEAD_LETTER = "dead-letter"
ACTION_COMPLETED = "completed"


@dataclass
class FaultRecovery:
    """One injected fault joined to how the unmodified controller handled it."""

    injected_kind: str
    attempt: int
    point: str
    classified_kind: str | None      # what classify_failure returned for that attempt
    recovery_action: str             # retry | dead-letter | completed
    classified_correctly: bool       # injected_kind == classified_kind
    recovered_as_designed: bool      # the recovery matches the retry/dead-letter rule

    def to_dict(self) -> dict[str, Any]:
        return {
            "injected_kind": self.injected_kind,
            "attempt": self.attempt,
            "point": self.point,
            "classified_kind": self.classified_kind,
            "recovery_action": self.recovery_action,
            "classified_correctly": self.classified_correctly,
            "recovered_as_designed": self.recovered_as_designed,
        }


@dataclass
class ResilienceReport:
    """The full drill report (facts only)."""

    run_id: str
    succeeded: bool
    dead_lettered: bool
    recoveries: list[FaultRecovery] = field(default_factory=list)
    restart: RestartProof | None = None
    slo: dict[str, Any] = field(default_factory=dict)
    verdict_counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            # A stable kind so the deterministic results-card builder can dispatch on it.
            "kind": "resilience",
            "run_id": self.run_id,
            "succeeded": self.succeeded,
            "dead_lettered": self.dead_lettered,
            "injected": [r.to_dict() for r in self.recoveries],
            "slo": dict(self.slo),
            "verdict_counts": dict(self.verdict_counts),
        }
        out["restart"] = self.restart.to_dict() if self.restart is not None else None
        return out


def _recovery_action_for(attempt_index: int, n_attempts: int, outcome: RunOutcome) -> str:
    """Classify what the controller DID for the attempt at ``attempt_index`` (0-based):
    succeeded → completed; a non-final failed attempt that was followed by another attempt →
    retry; the final failed attempt of a dead-lettered run → dead-letter."""
    attempt = outcome.attempts[attempt_index]
    if attempt.status.phase == "succeeded":
        return ACTION_COMPLETED
    is_last = attempt_index == n_attempts - 1
    if not is_last:
        return ACTION_RETRY
    # last attempt: dead-letter if the run dead-lettered, else (it succeeded earlier) completed
    return ACTION_DEAD_LETTER if outcome.dead_lettered else ACTION_COMPLETED


def build_resilience_report(
    outcome: RunOutcome,
    ledger: FaultLedger,
    restart: RestartProof | None,
    *,
    slo_budget_s: float,
    elapsed_s: float,
) -> ResilienceReport:
    """Join the authoritative inject ledger against the unmodified controller's ``RunOutcome``.

    For each REALIZED injection, find the matching attempt (by per-attempt run id), read what
    ``classify_failure`` returned (carried on ``AttemptResult.failure``) and what recovery the
    controller took, and compute the two deterministic predicates. SLO ``met`` = elapsed ≤
    budget. Pure function — no I/O, no policy."""
    n = len(outcome.attempts)
    by_run_id = {a.run_id: idx for idx, a in enumerate(outcome.attempts)}

    recoveries: list[FaultRecovery] = []
    for entry in ledger.realized():
        idx = by_run_id.get(entry.run_id)
        classified_kind: str | None = None
        action = ACTION_COMPLETED
        if idx is not None:
            attempt = outcome.attempts[idx]
            classified_kind = attempt.failure.kind if attempt.failure is not None else None
            action = _recovery_action_for(idx, n, outcome)

        classified_correctly = classified_kind == entry.kind
        # "As designed": a retryable kind should retry (unless it's the budget's last attempt),
        # a deterministic kind should dead-letter. Evaluated against the kind that was injected.
        should_retry = entry.kind in _RETRYABLE
        if action == ACTION_RETRY:
            recovered_as_designed = should_retry
        elif action == ACTION_DEAD_LETTER:
            # A retryable fault may legitimately dead-letter ONLY when the retry budget is spent.
            recovered_as_designed = (not should_retry) or (idx == n - 1)
        else:  # completed — a transient fault that retried and then succeeded
            recovered_as_designed = True

        recoveries.append(FaultRecovery(
            injected_kind=entry.kind,
            attempt=entry.attempt,
            point=entry.point,
            classified_kind=classified_kind,
            recovery_action=action,
            classified_correctly=classified_correctly,
            recovered_as_designed=recovered_as_designed,
        ))

    met = elapsed_s <= slo_budget_s
    slo = {"budget_s": slo_budget_s, "elapsed_s": elapsed_s, "met": met}

    verdict_counts = {
        "faults_injected": len(recoveries),
        "classified_correctly": sum(1 for r in recoveries if r.classified_correctly),
        "recovered_as_designed": sum(1 for r in recoveries if r.recovered_as_designed),
        "restart_survived": int(bool(restart and restart.recovered and restart.no_duplicates)),
    }

    return ResilienceReport(
        run_id=outcome.run_id,
        succeeded=outcome.succeeded,
        dead_lettered=outcome.dead_lettered,
        recoveries=recoveries,
        restart=restart,
        slo=slo,
        verdict_counts=verdict_counts,
    )
