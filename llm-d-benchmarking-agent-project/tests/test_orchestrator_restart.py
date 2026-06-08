"""Orchestrator-restart durability proof (prove_restart_recovery).

Restart = discard the orchestrator object and build a FRESH one against the same (fake) cluster
state, then resume via the EXISTING reconstruct()/run_sweep(sweep_id=...) methods. These tests
reuse the test_orchestrator_checkpoint.py interrupt→fresh-store→resume pattern and assert the
key durability invariant: 0 duplicate Job applies on resume.

All hermetic — FakeKubeClient persists the checkpoint ConfigMap in-memory exactly as
kubectl apply/get would. No real cluster, no GPU, no network.
"""
from __future__ import annotations

from app.orchestrator.controller import BenchmarkOrchestrator
from app.orchestrator.job import JobSpec
from app.orchestrator.restart import RestartProof, prove_restart_recovery
from tests.orchestrator_fakes import FakeKubeClient

NS = "bench"
SWEEP = "sw-restart"


def _spec(run_id: str, **kw) -> JobSpec:
    base = dict(run_id=run_id, namespace=NS, image="img", command=["llmdbenchmark", "run"],
                session_id="sessA", sweep_id=SWEEP)
    base.update(kw)
    return JobSpec(**base)


def _applied_run_ids(kube: FakeKubeClient) -> list[str]:
    ids = []
    for _ns, manifest in kube.applied:
        if manifest.get("kind") != "Job":
            continue
        rid = manifest["metadata"]["labels"]["llmd-bench/run-id"]
        ids.append(rid.rsplit("-a", 1)[0])
    return ids


# --------------------------------------------------------------------------- single-run reconstruct


async def test_fresh_orchestrator_reconstructs_in_flight_run_from_labels(tmp_path):
    """A run is active; the orchestrator is dropped; a fresh one on the same cluster recovers
    the in-flight Job PURELY from cluster labels (it stored nothing locally)."""
    kube = FakeKubeClient()
    # An active run already in the cluster (as if submitted before the restart).
    kube.program("r1-a1", phases=["active"], labels={"llmd-bench/session": "sessA"})

    proof = await prove_restart_recovery(kube, tmp_path, namespace=NS, session_id="sessA")
    assert proof.mode == "reconstruct"
    assert proof.recovered is True
    assert proof.in_flight_recovered == 1
    assert any("r1-a1" in name for name in proof.recovered_run_ids)


async def test_reconstruct_then_watch_to_completion(tmp_path):
    """After reconstruct recovers an in-flight run, a fresh orchestrator can watch it to a
    terminal phase on the same cluster — full restart-resume of a single run."""
    kube = FakeKubeClient()
    kube.program("r1-a1", phases=["active", "succeeded"], labels={"llmd-bench/session": "sessA"})

    fresh = BenchmarkOrchestrator(kube, tmp_path)
    statuses = await fresh.reconstruct(namespace=NS, session_id="sessA")
    assert len(statuses) == 1
    final = await fresh.watch("r1-a1", namespace=NS, poll_interval=0, max_wait=10)
    assert final.phase == "succeeded"


# --------------------------------------------------------------------------- sweep checkpoint resume


async def test_sweep_resume_runs_only_remaining_with_zero_duplicates(tmp_path):
    """ACCEPTANCE: a sweep records k=2 of N=5 completed, then a FRESH orchestrator resumes via
    run_sweep(sweep_id=...) — only the remaining 3 run, each treatment applied exactly once
    (0 duplicate Jobs), and the checkpoint ends with all 5 completed."""
    kube = FakeKubeClient()
    treatments = ["t1", "t2", "t3", "t4", "t5"]
    for t in treatments:
        kube.program(f"{t}-a1", phases=["succeeded"])

    # --- pre-restart: the first k=2 complete + checkpoint to the cluster ConfigMap.
    staging = BenchmarkOrchestrator(kube, tmp_path)
    await staging.run_sweep([_spec("t1"), _spec("t2")], max_parallel=2, max_attempts=1,
                            poll_interval=0, sweep_id=SWEEP, namespace=NS)
    applied_before = list(_applied_run_ids(kube))
    assert sorted(applied_before) == ["t1", "t2"]

    # --- restart: a FRESH orchestrator resumes the full sweep through the proof wrapper.
    specs = [_spec(t) for t in treatments]
    proof = await prove_restart_recovery(kube, tmp_path, namespace=NS, sweep_id=SWEEP, specs=specs,
                                         max_parallel=2, max_attempts=1, poll_interval=0)

    assert isinstance(proof, RestartProof) and proof.mode == "sweep"
    assert proof.recovered is True
    assert proof.completed_before == 2
    assert proof.run_after == 3                       # only t3,t4,t5 ran on resume
    assert proof.total_treatments == 5
    assert proof.duplicate_applies == 0 and proof.no_duplicates is True
    assert sorted(proof.resumed_ids) == ["t1", "t2"]

    # No duplicate Jobs across BOTH passes.
    all_applied = _applied_run_ids(kube)
    for t in treatments:
        assert all_applied.count(t) == 1

    # The cluster checkpoint now records all five completed.
    final = await BenchmarkOrchestrator(kube, tmp_path).reconstruct_sweep(SWEEP, namespace=NS)
    assert final.completed_ids() == set(treatments)


async def test_sweep_proof_to_dict_has_durability_keys(tmp_path):
    kube = FakeKubeClient()
    for t in ["t1", "t2"]:
        kube.program(f"{t}-a1", phases=["succeeded"])
    specs = [_spec("t1"), _spec("t2")]
    proof = await prove_restart_recovery(kube, tmp_path, namespace=NS, sweep_id=SWEEP, specs=specs,
                                         max_parallel=2, max_attempts=1, poll_interval=0)
    d = proof.to_dict()
    for key in ("mode", "recovered", "no_duplicates", "completed_before", "run_after",
                "total_treatments", "duplicate_applies", "resumed_ids"):
        assert key in d, f"missing {key}"
    assert d["no_duplicates"] is True


# --------------------------------------------------------------------------- restart during a faulted run


async def test_restart_during_injected_fault_run_combined(tmp_path):
    """Combined: a chaos-faulted run + a restart-durability proof in one drill. A transient
    fault retries to success, and a fresh orchestrator resumes a partial sweep with 0 dups."""
    from app.orchestrator.chaos import ChaosKubeClient, ChaosPlan

    # The faulted run (evicted @ a1 → retry → a2 succeeds), through the chaos decorator.
    fault_kube = FakeKubeClient()
    fault_kube.program("rd-a1", phases=["active", "succeeded"])
    fault_kube.program("rd-a2", phases=["active", "succeeded"])
    chaos = ChaosKubeClient(fault_kube, ChaosPlan.from_dict(
        {"seed": 1, "injections": [{"kind": "evicted", "at_attempt": 1}]}))
    run_outcome = await BenchmarkOrchestrator(chaos, tmp_path).run_with_retries(
        _spec("rd"), max_attempts=3, poll_interval=0, max_wait=10)
    assert run_outcome.succeeded and len(run_outcome.attempts) == 2

    # The restart-durability proof on a partial sweep (separate clean cluster).
    sweep_kube = FakeKubeClient()
    for t in ["t1", "t2", "t3"]:
        sweep_kube.program(f"{t}-a1", phases=["succeeded"])
    await BenchmarkOrchestrator(sweep_kube, tmp_path).run_sweep(
        [_spec("t1")], max_parallel=1, max_attempts=1, poll_interval=0, sweep_id=SWEEP, namespace=NS)
    proof = await prove_restart_recovery(
        sweep_kube, tmp_path, namespace=NS, sweep_id=SWEEP,
        specs=[_spec("t1"), _spec("t2"), _spec("t3")], max_parallel=2, max_attempts=1, poll_interval=0)
    assert proof.no_duplicates and proof.completed_before == 1 and proof.run_after == 2
