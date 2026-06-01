"""Phase 3d — retry + dead-letter. Transient faults (eviction, unknown) get a fresh Job
attempt; deterministic faults (OOM) dead-letter immediately; exhausting the budget
dead-letters. Each attempt is a distinct Job (<run_id>-a<N>). Hermetic."""
from __future__ import annotations

from app.orchestrator.controller import BenchmarkOrchestrator
from app.orchestrator.faults import OOM
from app.orchestrator.job import JobSpec
from tests.orchestrator_fakes import FakeKubeClient, make_pod


def _spec(run_id="r1"):
    return JobSpec(run_id=run_id, namespace="bench", image="img", command=["llmdbenchmark", "run"],
                   session_id="sess1", active_deadline_seconds=300)


async def test_success_on_first_attempt(tmp_path):
    kube = FakeKubeClient()
    kube.program("r1-a1", phases=["active", "succeeded"])
    orch = BenchmarkOrchestrator(kube, workspace=tmp_path)
    out = await orch.run_with_retries(_spec(), poll_interval=0)
    assert out.succeeded and not out.dead_lettered
    assert [a.run_id for a in out.attempts] == ["r1-a1"]


async def test_transient_failure_then_success(tmp_path):
    kube = FakeKubeClient()
    # attempt 1: failed + evicted pod (transient → retry); attempt 2: succeeds
    kube.program("r1-a1", phases=["active", "failed"], pods=[make_pod("r1-a1", phase="Failed", reason="Evicted")])
    kube.program("r1-a2", phases=["succeeded"])
    orch = BenchmarkOrchestrator(kube, workspace=tmp_path)
    out = await orch.run_with_retries(_spec(), max_attempts=3, poll_interval=0)
    assert out.succeeded and not out.dead_lettered
    assert [a.run_id for a in out.attempts] == ["r1-a1", "r1-a2"]
    assert out.attempts[0].failure.kind == "evicted"


async def test_nonretryable_oom_dead_letters_immediately(tmp_path):
    kube = FakeKubeClient()
    kube.program("r1-a1", phases=["failed"], pods=[make_pod("r1-a1", phase="Failed", terminated="OOMKilled", exit_code=137)])
    orch = BenchmarkOrchestrator(kube, workspace=tmp_path)
    out = await orch.run_with_retries(_spec(), max_attempts=3, poll_interval=0)
    assert not out.succeeded and out.dead_lettered
    assert len(out.attempts) == 1                 # no retry — OOM is deterministic
    assert out.final_failure.kind == OOM


async def test_exhausts_retries_then_dead_letters(tmp_path):
    kube = FakeKubeClient()
    for i in (1, 2, 3):
        kube.program(f"r1-a{i}", phases=["failed"], pods=[make_pod(f"r1-a{i}", phase="Failed", reason="Evicted")])
    orch = BenchmarkOrchestrator(kube, workspace=tmp_path)
    out = await orch.run_with_retries(_spec(), max_attempts=3, poll_interval=0)
    assert not out.succeeded and out.dead_lettered
    assert [a.run_id for a in out.attempts] == ["r1-a1", "r1-a2", "r1-a3"]
    assert out.final_failure.kind == "evicted"


async def test_each_attempt_is_a_distinct_job(tmp_path):
    kube = FakeKubeClient()
    kube.program("r1-a1", phases=["failed"], pods=[make_pod("r1-a1", phase="Failed", reason="Evicted")])
    kube.program("r1-a2", phases=["succeeded"])
    orch = BenchmarkOrchestrator(kube, workspace=tmp_path)
    await orch.run_with_retries(_spec(), max_attempts=3, poll_interval=0)
    # both attempts were applied as separate Jobs (distinct manifests persisted)
    assert (tmp_path / "jobs" / "r1-a1.yaml").is_file()
    assert (tmp_path / "jobs" / "r1-a2.yaml").is_file()
    applied_run_ids = [m["metadata"]["labels"]["llmd-bench/run-id"] for _, m in kube.applied]
    assert applied_run_ids == ["r1-a1", "r1-a2"]
