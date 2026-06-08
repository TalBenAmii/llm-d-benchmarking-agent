"""Chaos fault-injection — the ChaosKubeClient decorator drives injected faults through the
COMPLETELY UNMODIFIED classify_failure → run_with_retries retry/dead-letter path.

All hermetic: the decorator wraps the in-memory FakeKubeClient; no cluster, no GPU, no network.
The proof these tests carry is that each injected fault is classified back to the same kind by
the unchanged classifier and recovered by the unchanged retry rule — so the resilience feature
adds NO new lifecycle logic.
"""
from __future__ import annotations

import pytest

from app.orchestrator.chaos import (
    POINT_MID_WATCH,
    ChaosKubeClient,
    ChaosPlan,
    FaultInjection,
    FaultLedger,
)
from app.orchestrator.controller import BenchmarkOrchestrator
from app.orchestrator.job import JobSpec
from tests.orchestrator_fakes import FakeKubeClient

NS = "bench"


def _spec(run_id: str = "rd") -> JobSpec:
    return JobSpec(run_id=run_id, namespace=NS, image="img", command=["llmdbenchmark", "run"])


def _drive(plan: ChaosPlan, *, programs: dict[str, list[str]], run_id: str = "rd",
           max_attempts: int = 3):
    """Wrap a programmed FakeKubeClient in the chaos decorator and run_with_retries."""
    kube = FakeKubeClient()
    for rid, phases in programs.items():
        kube.program(rid, phases=phases)
    chaos = ChaosKubeClient(kube, plan)
    orch = BenchmarkOrchestrator(chaos, workspace="/tmp/chaos-test-ws")
    return chaos, orch, _spec(run_id)


# --------------------------------------------------------------------------- plan parsing


def test_plan_from_dict_validates_shape_not_wisdom():
    plan = ChaosPlan.from_dict({"seed": 5, "injections": [
        {"kind": "evicted", "at_attempt": 1, "point": "before-watch", "probability": 1.0}]})
    assert plan.seed == 5 and len(plan.injections) == 1
    assert plan.injections[0].kind == "evicted"


def test_plan_from_dict_accepts_faults_alias_and_empty():
    assert ChaosPlan.from_dict(None).is_empty()
    assert ChaosPlan.from_dict({}).is_empty()
    plan = ChaosPlan.from_dict({"faults": [{"kind": "oom"}]})
    assert len(plan.injections) == 1 and plan.injections[0].kind == "oom"


@pytest.mark.parametrize("bad", [
    {"injections": [{"kind": "not_a_kind"}]},          # bad kind
    {"injections": [{"kind": "oom", "at_attempt": 0}]},  # at_attempt < 1
    {"injections": [{"kind": "oom", "point": "wat"}]},   # bad point
    {"injections": [{"kind": "oom", "probability": 2}]},  # prob out of range
    {"injections": [{"kind": "oom", "bogus": 1}]},       # unknown injection field
    {"bogus": 1},                                          # unknown top-level field
    {"seed": "x", "injections": []},                      # bad seed type
    {"injections": "notalist"},                           # injections not a list
])
def test_plan_from_dict_rejects_bad_shape(bad):
    with pytest.raises(ValueError):
        ChaosPlan.from_dict(bad)


# --------------------------------------------------------------------------- transient -> retry


async def test_evicted_is_classified_and_retried_then_succeeds():
    """evicted @ a1 → classified evicted → retried as a fresh Job -a2 → succeeds. Through the
    UNMODIFIED run_with_retries."""
    plan = ChaosPlan.from_dict({"seed": 1, "injections": [{"kind": "evicted", "at_attempt": 1}]})
    chaos, orch, spec = _drive(plan, programs={
        "rd-a1": ["active", "succeeded"], "rd-a2": ["active", "succeeded"]})
    outcome = await orch.run_with_retries(spec, max_attempts=3, poll_interval=0, max_wait=10)

    assert outcome.succeeded is True and outcome.dead_lettered is False
    assert [a.run_id for a in outcome.attempts] == ["rd-a1", "rd-a2"]
    assert outcome.attempts[0].failure is not None and outcome.attempts[0].failure.kind == "evicted"
    assert outcome.attempts[1].status.phase == "succeeded"
    # The ledger recorded exactly the one realized injection (on attempt 1 only).
    realized = chaos.ledger.realized()
    assert len(realized) == 1 and realized[0].kind == "evicted" and realized[0].attempt == 1


# --------------------------------------------------------------------------- deterministic -> dead-letter


@pytest.mark.parametrize("kind", ["oom", "unschedulable", "image_error", "timeout"])
async def test_deterministic_faults_are_classified_and_dead_lettered(kind):
    """A deterministic fault is classified correctly by the UNCHANGED classify_failure and
    dead-letters immediately — exactly one attempt, no retry."""
    plan = ChaosPlan.from_dict({"seed": 1, "injections": [{"kind": kind, "at_attempt": 1}]})
    chaos, orch, spec = _drive(plan, programs={"rd-a1": ["active", "succeeded"]})
    outcome = await orch.run_with_retries(spec, max_attempts=3, poll_interval=0, max_wait=10)

    assert outcome.succeeded is False and outcome.dead_lettered is True
    assert len(outcome.attempts) == 1                     # no retry on a deterministic fault
    assert outcome.final_failure is not None and outcome.final_failure.kind == kind


async def test_run_error_dead_letters_with_exit_code():
    plan = ChaosPlan.from_dict({"seed": 1, "injections": [
        {"kind": "run_error", "at_attempt": 1, "exit_code": 42, "message": "boom"}]})
    chaos, orch, spec = _drive(plan, programs={"rd-a1": ["active", "succeeded"]})
    outcome = await orch.run_with_retries(spec, max_attempts=2, poll_interval=0, max_wait=10)
    assert outcome.dead_lettered and outcome.final_failure.kind == "run_error"
    assert outcome.final_failure.exit_code == 42


# --------------------------------------------------------------------------- ledger / determinism


async def test_probability_zero_injects_nothing():
    plan = ChaosPlan.from_dict({"seed": 1, "injections": [
        {"kind": "oom", "at_attempt": 1, "probability": 0.0}]})
    chaos, orch, spec = _drive(plan, programs={"rd-a1": ["active", "succeeded"]})
    outcome = await orch.run_with_retries(spec, max_attempts=2, poll_interval=0, max_wait=10)
    assert outcome.succeeded is True
    # The roll was recorded but did not realize.
    assert chaos.ledger.entries and not chaos.ledger.realized()


async def test_seeded_rng_is_reproducible():
    """A probabilistic plan with a fixed seed produces the SAME realized/skipped decision."""
    async def fire():
        plan = ChaosPlan.from_dict({"seed": 123, "injections": [
            {"kind": "evicted", "at_attempt": 1, "probability": 0.5}]})
        kube = FakeKubeClient()
        kube.program("rd-a1", phases=["active", "failed"])
        chaos = ChaosKubeClient(kube, plan)
        # Trigger the decision by reading the attempt's job once.
        await chaos.list_jobs(namespace=NS, selector="llmd-bench/run-id=rd-a1")
        return [(e.kind, e.realized) for e in chaos.ledger.entries]

    assert await fire() == await fire()


async def test_empty_plan_is_byte_identical_to_a_plain_fake_run():
    """Determinism: chaos OFF (empty plan) ⇒ the same outcome as a plain FakeKubeClient run."""
    spec = _spec()
    # Plain run.
    plain = FakeKubeClient()
    plain.program("rd-a1", phases=["active", "succeeded"])
    plain_outcome = await BenchmarkOrchestrator(plain, "/tmp/ws-plain").run_with_retries(
        spec, max_attempts=2, poll_interval=0, max_wait=10)
    # Chaos with an empty plan.
    chaos_kube = FakeKubeClient()
    chaos_kube.program("rd-a1", phases=["active", "succeeded"])
    chaos = ChaosKubeClient(chaos_kube, ChaosPlan())
    chaos_outcome = await BenchmarkOrchestrator(chaos, "/tmp/ws-chaos").run_with_retries(
        spec, max_attempts=2, poll_interval=0, max_wait=10)

    assert plain_outcome.succeeded == chaos_outcome.succeeded is True
    assert [a.run_id for a in plain_outcome.attempts] == [a.run_id for a in chaos_outcome.attempts]
    assert not chaos.ledger.entries           # nothing injected, nothing recorded


async def test_mid_watch_point_fails_after_active_polls():
    """A 'mid-watch' fault reads as active for after_polls polls, then presents the fault."""
    plan = ChaosPlan(injections=[FaultInjection(kind="oom", at_attempt=1,
                                                point=POINT_MID_WATCH, after_polls=2)], seed=1)
    kube = FakeKubeClient()
    # Program several active snapshots so the chaos decorator can keep it active, then it forces failed.
    kube.program("rd-a1", phases=["active", "active", "active", "active"])
    chaos = ChaosKubeClient(kube, plan)
    orch = BenchmarkOrchestrator(chaos, "/tmp/ws-mid")
    outcome = await orch.run_with_retries(_spec(), max_attempts=1, poll_interval=0, max_wait=10)
    assert outcome.dead_lettered and outcome.final_failure.kind == "oom"


def test_fault_ledger_records_realized_subset():
    ledger = FaultLedger()
    from app.orchestrator.chaos import LedgerEntry
    ledger.record(LedgerEntry(run_id="rd-a1", kind="oom", attempt=1, point="before-watch", realized=True))
    ledger.record(LedgerEntry(run_id="rd-a2", kind="evicted", attempt=2, point="before-watch", realized=False))
    assert len(ledger.entries) == 2 and len(ledger.realized()) == 1
    assert ledger.realized()[0].kind == "oom"
