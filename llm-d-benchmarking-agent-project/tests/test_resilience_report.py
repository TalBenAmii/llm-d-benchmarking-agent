"""build_resilience_report — the pure join of injected ⋈ classified ⋈ recovery.

No cluster, no orchestrator run: these tests build RunOutcome / FaultLedger / RestartProof
fixtures by hand and assert the join's deterministic predicates (classified_correctly,
recovered_as_designed, SLO met, verdict counts).
"""
from __future__ import annotations

from app.orchestrator.chaos import FaultLedger, LedgerEntry
from app.orchestrator.controller import AttemptResult, RunOutcome
from app.orchestrator.faults import Failure
from app.orchestrator.job import JobStatus
from app.orchestrator.resilience import build_resilience_report
from app.orchestrator.restart import RestartProof


def _attempt(run_id: str, phase: str, fault_kind: str | None = None) -> AttemptResult:
    failure = Failure(fault_kind) if fault_kind else None
    return AttemptResult(run_id=run_id, status=JobStatus(name=run_id, phase=phase), failure=failure)


def _ledger(*entries: tuple[str, str, int]) -> FaultLedger:
    led = FaultLedger()
    for run_id, kind, attempt in entries:
        led.record(LedgerEntry(run_id=run_id, kind=kind, attempt=attempt,
                               point="before-watch", realized=True))
    return led


def test_transient_fault_retried_then_succeeded_is_as_designed():
    outcome = RunOutcome("rd", succeeded=True, attempts=[
        _attempt("rd-a1", "failed", "evicted"),
        _attempt("rd-a2", "succeeded"),
    ])
    report = build_resilience_report(outcome, _ledger(("rd-a1", "evicted", 1)), None,
                                     slo_budget_s=600, elapsed_s=10)
    d = report.to_dict()
    row = d["injected"][0]
    assert row["injected_kind"] == "evicted" and row["classified_kind"] == "evicted"
    assert row["classified_correctly"] is True
    assert row["recovery_action"] == "retry"
    assert row["recovered_as_designed"] is True
    assert d["succeeded"] is True and d["dead_lettered"] is False


def test_deterministic_fault_dead_lettered_is_as_designed():
    outcome = RunOutcome("rd", succeeded=False, dead_lettered=True,
                         attempts=[_attempt("rd-a1", "failed", "oom")],
                         final_failure=Failure("oom"))
    report = build_resilience_report(outcome, _ledger(("rd-a1", "oom", 1)), None,
                                     slo_budget_s=600, elapsed_s=5)
    row = report.to_dict()["injected"][0]
    assert row["recovery_action"] == "dead-letter"
    assert row["classified_correctly"] is True
    assert row["recovered_as_designed"] is True


def test_misclassification_is_flagged():
    """If the classified kind differs from the injected kind, classified_correctly is False."""
    outcome = RunOutcome("rd", succeeded=False, dead_lettered=True,
                         attempts=[_attempt("rd-a1", "failed", "run_error")],
                         final_failure=Failure("run_error"))
    report = build_resilience_report(outcome, _ledger(("rd-a1", "oom", 1)), None,
                                     slo_budget_s=600, elapsed_s=5)
    row = report.to_dict()["injected"][0]
    assert row["injected_kind"] == "oom" and row["classified_kind"] == "run_error"
    assert row["classified_correctly"] is False


def test_transient_fault_dead_lettered_only_when_budget_spent():
    """A transient fault that dead-letters because the retry budget was exhausted is still
    'as designed' (only the LAST attempt may legitimately dead-letter a retryable fault)."""
    outcome = RunOutcome("rd", succeeded=False, dead_lettered=True, attempts=[
        _attempt("rd-a1", "failed", "evicted"),
        _attempt("rd-a2", "failed", "evicted"),
    ], final_failure=Failure("evicted"))
    led = _ledger(("rd-a1", "evicted", 1), ("rd-a2", "evicted", 2))
    rows = build_resilience_report(outcome, led, None, slo_budget_s=600, elapsed_s=5).to_dict()["injected"]
    # a1 was followed by a2 → retry (as designed); a2 was the last → dead-letter, still ok.
    assert rows[0]["recovery_action"] == "retry" and rows[0]["recovered_as_designed"] is True
    assert rows[1]["recovery_action"] == "dead-letter" and rows[1]["recovered_as_designed"] is True


def test_slo_met_and_missed():
    outcome = RunOutcome("rd", succeeded=True, attempts=[_attempt("rd-a1", "succeeded")])
    met = build_resilience_report(outcome, FaultLedger(), None, slo_budget_s=600, elapsed_s=412).to_dict()
    assert met["slo"] == {"budget_s": 600, "elapsed_s": 412, "met": True}
    missed = build_resilience_report(outcome, FaultLedger(), None, slo_budget_s=100, elapsed_s=412).to_dict()
    assert missed["slo"]["met"] is False


def test_verdict_counts_and_restart_survived():
    outcome = RunOutcome("rd", succeeded=True, attempts=[
        _attempt("rd-a1", "failed", "evicted"),
        _attempt("rd-a2", "succeeded"),
    ])
    restart = RestartProof(mode="sweep", recovered=True, no_duplicates=True,
                           completed_before=2, run_after=3, total_treatments=5)
    d = build_resilience_report(outcome, _ledger(("rd-a1", "evicted", 1)), restart,
                                slo_budget_s=600, elapsed_s=1).to_dict()
    vc = d["verdict_counts"]
    assert vc["faults_injected"] == 1
    assert vc["classified_correctly"] == 1
    assert vc["recovered_as_designed"] == 1
    assert vc["restart_survived"] == 1
    assert d["restart"]["no_duplicates"] is True


def test_restart_with_duplicates_is_not_survived():
    outcome = RunOutcome("rd", succeeded=True, attempts=[_attempt("rd-a1", "succeeded")])
    restart = RestartProof(mode="sweep", recovered=True, no_duplicates=False, duplicate_applies=1)
    d = build_resilience_report(outcome, FaultLedger(), restart, slo_budget_s=600, elapsed_s=1).to_dict()
    assert d["verdict_counts"]["restart_survived"] == 0


def test_unrealized_injections_are_not_joined():
    """A probability roll that did NOT fire is not a row in the report (only realized faults)."""
    led = FaultLedger()
    led.record(LedgerEntry(run_id="rd-a1", kind="oom", attempt=1, point="before-watch", realized=False))
    outcome = RunOutcome("rd", succeeded=True, attempts=[_attempt("rd-a1", "succeeded")])
    d = build_resilience_report(outcome, led, None, slo_budget_s=600, elapsed_s=1).to_dict()
    assert d["injected"] == [] and d["verdict_counts"]["faults_injected"] == 0


def test_report_to_dict_has_kind_for_card_dispatch():
    outcome = RunOutcome("rd", succeeded=True, attempts=[_attempt("rd-a1", "succeeded")])
    d = build_resilience_report(outcome, FaultLedger(), None, slo_budget_s=600, elapsed_s=1).to_dict()
    assert d["kind"] == "resilience"
    assert d["restart"] is None        # explicit None when no restart proof
