"""Phase 3b — the Job lifecycle controller: manifest generation, submit, watch-to-terminal,
log streaming, and cluster-only reconstruction. Hermetic (FakeKubeClient, no cluster)."""
from __future__ import annotations

from app.orchestrator.controller import BenchmarkOrchestrator
from app.orchestrator.job import (
    ACTIVE,
    FAILED,
    LABEL_RUN,
    LABEL_SESSION,
    PENDING,
    SUCCEEDED,
    JobSpec,
    build_job_manifest,
    classify_job_status,
    job_name,
)
from tests.orchestrator_fakes import FakeKubeClient, make_job


def _spec(run_id="r1", **kw):
    base = dict(run_id=run_id, namespace="bench", image="ghcr.io/llm-d/bench:0",
                command=["llmdbenchmark", "run"], session_id="sess1", spec="cicd/kind",
                harness="inference-perf", workload="sanity_random.yaml")
    base.update(kw)
    return JobSpec(**base)


# ---- manifest (pure) ------------------------------------------------------

def test_build_job_manifest_shape():
    m = build_job_manifest(_spec(active_deadline_seconds=600, cpu="2", memory="4Gi"))
    assert m["apiVersion"] == "batch/v1" and m["kind"] == "Job"
    assert m["metadata"]["name"] == "llmd-bench-r1"
    assert m["metadata"]["labels"][LABEL_RUN] == "r1"
    assert m["metadata"]["labels"][LABEL_SESSION] == "sess1"
    assert m["metadata"]["annotations"]["llmd-bench/spec"] == "cicd/kind"
    js = m["spec"]
    assert js["backoffLimit"] == 0                       # orchestrator owns retries
    assert js["activeDeadlineSeconds"] == 600            # K8s marks a hung run DeadlineExceeded
    pod = js["template"]["spec"]
    assert pod["restartPolicy"] == "Never"
    assert pod["containers"][0]["resources"]["limits"] == {"cpu": "2", "memory": "4Gi"}
    # pod template carries the run label so logs/pod-inspection can select it
    assert js["template"]["metadata"]["labels"][LABEL_RUN] == "r1"


# ---- classify (pure) ------------------------------------------------------

def test_classify_phases():
    assert classify_job_status(make_job("r", "active")).phase == ACTIVE
    assert classify_job_status(make_job("r", "pending")).phase == PENDING
    assert classify_job_status(make_job("r", "succeeded")).phase == SUCCEEDED
    f = classify_job_status(make_job("r", "failed", reason="DeadlineExceeded", message="too slow"))
    assert f.phase == FAILED and f.reason == "DeadlineExceeded" and f.message == "too slow"
    assert classify_job_status(make_job("r", "succeeded")).terminal is True
    assert classify_job_status(make_job("r", "active")).terminal is False


# ---- submit ---------------------------------------------------------------

async def test_submit_writes_manifest_and_applies(tmp_path):
    kube = FakeKubeClient()
    orch = BenchmarkOrchestrator(kube, workspace=tmp_path)
    name = await orch.submit(_spec(active_deadline_seconds=300))
    assert name == "llmd-bench-r1"
    # manifest persisted as the run record
    assert (tmp_path / "jobs" / "r1.yaml").is_file()
    # applied to the cluster with the right namespace + labels
    ns, manifest = kube.applied[-1]
    assert ns == "bench" and manifest["metadata"]["labels"][LABEL_RUN] == "r1"


# ---- watch ----------------------------------------------------------------

async def test_watch_polls_to_succeeded(tmp_path):
    kube = FakeKubeClient()
    kube.program("r1", phases=["active", "active", "succeeded"])
    orch = BenchmarkOrchestrator(kube, workspace=tmp_path)
    seen = []

    async def on_status(st):
        seen.append(st.phase)

    final = await orch.watch("r1", namespace="bench", poll_interval=0, on_status=on_status)
    assert final.phase == SUCCEEDED
    assert seen[0] == ACTIVE and seen[-1] == SUCCEEDED   # progression observed, deduped by phase


async def test_watch_reports_failure_reason(tmp_path):
    kube = FakeKubeClient()
    kube.program("r1", phases=["active", "failed"], reason="DeadlineExceeded")
    orch = BenchmarkOrchestrator(kube, workspace=tmp_path)
    final = await orch.watch("r1", namespace="bench", poll_interval=0)
    assert final.phase == FAILED and final.reason == "DeadlineExceeded"


async def test_watch_absent_run_times_out(tmp_path):
    kube = FakeKubeClient()  # nothing programmed
    orch = BenchmarkOrchestrator(kube, workspace=tmp_path)
    final = await orch.watch("ghost", namespace="bench", poll_interval=0, max_wait=0)
    assert final.phase == "absent"


# ---- logs + reconstruction ------------------------------------------------

async def test_stream_logs_returns_pod_output(tmp_path):
    kube = FakeKubeClient()
    kube.program("r1", phases=["succeeded"], logs="benchmark complete: 30/30 ok")
    orch = BenchmarkOrchestrator(kube, workspace=tmp_path)
    out = await orch.stream_logs("r1", namespace="bench")
    assert "30/30 ok" in out


async def test_reconstruct_from_cluster_by_session(tmp_path):
    kube = FakeKubeClient()
    # two runs in this session (one active, one succeeded), one in a different session
    kube.program("r1", phases=["active"], labels={LABEL_SESSION: "sessA"})
    kube.program("r2", phases=["succeeded"], labels={LABEL_SESSION: "sessA"})
    kube.program("r3", phases=["active"], labels={LABEL_SESSION: "sessB"})
    orch = BenchmarkOrchestrator(kube, workspace=tmp_path)

    statuses = await orch.reconstruct(namespace="bench", session_id="sessA")
    names = {s.name for s in statuses}
    assert names == {job_name("r1"), job_name("r2")}     # only sessA, rebuilt from cluster
    phases = {s.name: s.phase for s in statuses}
    assert phases[job_name("r1")] == ACTIVE and phases[job_name("r2")] == SUCCEEDED
