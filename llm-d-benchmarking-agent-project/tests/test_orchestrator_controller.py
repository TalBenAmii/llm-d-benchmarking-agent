"""Phase 3b — the Job lifecycle controller: manifest generation, submit, watch-to-terminal,
log streaming, and cluster-only reconstruction. Hermetic (FakeKubeClient, no cluster)."""
from __future__ import annotations

import pytest

from app.orchestrator.controller import BenchmarkOrchestrator
from app.orchestrator.job import (
    ABSENT,
    ACTIVE,
    FAILED,
    LABEL_RUN,
    LABEL_SESSION,
    LABEL_SWEEP,
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


def test_classify_edge_cases():
    # failed count set but the Failed condition not yet written -> still terminal FAILED
    # (a real, brief K8s window; with backoffLimit:0 the single attempt has failed).
    assert classify_job_status({"metadata": {"name": "j"}, "status": {"failed": 1}}).phase == FAILED
    # both Complete and Failed conditions True -> Complete wins (locks the precedence).
    both = {"metadata": {"name": "j"}, "status": {"succeeded": 1, "conditions": [
        {"type": "Complete", "status": "True"}, {"type": "Failed", "status": "True"}]}}
    assert classify_job_status(both).phase == SUCCEEDED
    # succeeded count, no condition, not active -> SUCCEEDED.
    assert classify_job_status({"metadata": {}, "status": {"succeeded": 1}}).phase == SUCCEEDED
    # a Failed condition with status "False" must NOT count as failed.
    notfailed = {"metadata": {}, "status": {"active": 1, "conditions": [{"type": "Failed", "status": "False"}]}}
    assert classify_job_status(notfailed).phase == ACTIVE


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


async def test_watch_returns_when_seen_then_deleted(tmp_path):
    # A run that was ACTIVE then gets deleted out from under us must terminate via the
    # seen-then-gone branch — NOT the (large) max_wait timeout.
    kube = FakeKubeClient()
    kube.program("r1", phases=["active", "active"])
    orch = BenchmarkOrchestrator(kube, workspace=tmp_path)
    state = {"deleted": False}

    async def on_status(st):
        if st.phase == ACTIVE and not state["deleted"]:
            state["deleted"] = True
            await kube.delete_job(job_name("r1"), namespace="bench")  # vanish before next poll

    final = await orch.watch("r1", namespace="bench", poll_interval=0, max_wait=100, on_status=on_status)
    assert final.phase == ABSENT and state["deleted"]


async def test_reconstruct_scoped_by_sweep_id(tmp_path):
    kube = FakeKubeClient()
    kube.program("t1", phases=["active"], labels={LABEL_SWEEP: "sw1"})
    kube.program("t2", phases=["succeeded"], labels={LABEL_SWEEP: "sw1"})
    kube.program("t3", phases=["active"], labels={LABEL_SWEEP: "sw2"})
    orch = BenchmarkOrchestrator(kube, workspace=tmp_path)
    statuses = await orch.reconstruct(namespace="bench", sweep_id="sw1")
    assert {s.name for s in statuses} == {job_name("t1"), job_name("t2")}


async def test_reconstruct_ands_session_and_sweep(tmp_path):
    kube = FakeKubeClient()
    kube.program("t1", phases=["active"], labels={LABEL_SWEEP: "sw1", LABEL_SESSION: "sessA"})
    kube.program("t2", phases=["active"], labels={LABEL_SWEEP: "sw1", LABEL_SESSION: "sessB"})
    orch = BenchmarkOrchestrator(kube, workspace=tmp_path)
    statuses = await orch.reconstruct(namespace="bench", session_id="sessA", sweep_id="sw1")
    assert {s.name for s in statuses} == {job_name("t1")}   # both labels ANDed


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
