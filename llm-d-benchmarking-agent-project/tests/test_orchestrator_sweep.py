"""Phase 3e — parallel sweep orchestration + cleanup. Treatments run as parallel Jobs under
a concurrency cap, each with its own retry/dead-letter budget; a persistently-failing
treatment dead-letters without sinking the sweep. Cleanup reaps only terminal Jobs. Hermetic."""
from __future__ import annotations

import asyncio

from app.orchestrator.controller import BenchmarkOrchestrator
from app.orchestrator.job import LABEL_SESSION, JobSpec, job_name
from tests.orchestrator_fakes import FakeKubeClient, make_pod


def _spec(run_id, **kw):
    base = dict(run_id=run_id, namespace="bench", image="img", command=["llmdbenchmark", "run"],
                session_id="sessA", sweep_id="sw1")
    base.update(kw)
    return JobSpec(**base)


async def test_sweep_aggregates_success_and_dead_letter(tmp_path):
    kube = FakeKubeClient()
    kube.program("t1-a1", phases=["succeeded"])                                   # succeeds
    kube.program("t2-a1", phases=["failed"],                                      # OOM -> dead-letter
                 pods=[make_pod("t2-a1", phase="Failed", terminated="OOMKilled", exit_code=137)])
    kube.program("t3-a1", phases=["failed"],                                      # evicted -> retry
                 pods=[make_pod("t3-a1", phase="Failed", reason="Evicted")])
    kube.program("t3-a2", phases=["succeeded"])                                   # ...then succeeds
    orch = BenchmarkOrchestrator(kube, workspace=tmp_path)

    out = await orch.run_sweep([_spec("t1"), _spec("t2"), _spec("t3")],
                               max_parallel=2, max_attempts=2, poll_interval=0)
    assert sorted(out.succeeded) == ["t1", "t3"]
    assert out.dead_lettered == ["t2"]
    assert not out.all_succeeded


async def test_sweep_respects_parallel_cap(tmp_path):
    kube = FakeKubeClient()
    kube.apply_gate = asyncio.Event()         # block applies so we can observe concurrency
    for i in (1, 2, 3, 4):
        kube.program(f"t{i}-a1", phases=["succeeded"])
    orch = BenchmarkOrchestrator(kube, workspace=tmp_path)
    specs = [_spec(f"t{i}") for i in (1, 2, 3, 4)]

    task = asyncio.create_task(orch.run_sweep(specs, max_parallel=2, max_attempts=1, poll_interval=0))
    await asyncio.sleep(0.05)
    assert kube.apply_peak == 2, "at most max_parallel treatments should apply at once"
    kube.apply_gate.set()
    out = await task
    assert out.all_succeeded and len(out.outcomes) == 4


async def test_cleanup_removes_terminal_jobs_only(tmp_path):
    kube = FakeKubeClient()
    kube.program("r1", phases=["succeeded"], labels={LABEL_SESSION: "sessA"})
    kube.program("r2", phases=["active"], labels={LABEL_SESSION: "sessA"})
    orch = BenchmarkOrchestrator(kube, workspace=tmp_path)

    deleted = await orch.cleanup(namespace="bench", session_id="sessA")
    assert deleted == [job_name("r1")]                          # only the terminal (succeeded) Job
    assert ("bench", job_name("r2")) not in kube.deleted        # the active run is preserved


async def test_cleanup_can_remove_all_when_not_only_terminal(tmp_path):
    kube = FakeKubeClient()
    kube.program("r1", phases=["succeeded"], labels={LABEL_SESSION: "sessA"})
    kube.program("r2", phases=["failed"], labels={LABEL_SESSION: "sessA"})
    orch = BenchmarkOrchestrator(kube, workspace=tmp_path)

    deleted = await orch.cleanup(namespace="bench", session_id="sessA", only_terminal=True)
    assert sorted(deleted) == sorted([job_name("r1"), job_name("r2")])  # both terminal
