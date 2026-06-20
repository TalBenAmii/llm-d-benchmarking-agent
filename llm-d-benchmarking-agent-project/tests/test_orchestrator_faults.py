"""Phase 3c — fault classification: map a failed Job + its pods to a stable fault kind
(OOM / timeout / eviction / unschedulable / image / run error). Pure + via the controller."""
from __future__ import annotations

from app.orchestrator.controller import BenchmarkOrchestrator
from app.orchestrator.faults import (
    EVICTED,
    IMAGE_ERROR,
    NONE,
    OOM,
    RUN_ERROR,
    TIMEOUT,
    UNKNOWN,
    UNSCHEDULABLE,
    classify_failure,
)
from app.orchestrator.job import JobStatus
from tests.orchestrator_fakes import FakeKubeClient, make_pod


def _failed(reason=""):
    return JobStatus(name="llmd-bench-r1", phase="failed", reason=reason)


def test_timeout_from_job_deadline():
    f = classify_failure(_failed(reason="DeadlineExceeded"), [])
    assert f.kind == TIMEOUT


def test_oom_from_container_terminated():
    pod = make_pod("r1", phase="Failed", terminated="OOMKilled", exit_code=137)
    f = classify_failure(_failed(), [pod])
    assert f.kind == OOM and f.exit_code == 137 and f.container == "benchmark"


def test_unschedulable_from_pod_condition():
    pod = make_pod("r1", phase="Pending")
    pod["status"]["conditions"] = [{"type": "PodScheduled", "status": "False",
                                    "reason": "Unschedulable", "message": "0/1 nodes: insufficient cpu"}]
    f = classify_failure(_failed(), [pod])
    assert f.kind == UNSCHEDULABLE and "insufficient cpu" in f.message


def test_evicted_from_pod_status():
    pod = make_pod("r1", phase="Failed", reason="Evicted")
    f = classify_failure(_failed(), [pod])
    assert f.kind == EVICTED


def test_image_error_from_waiting():
    pod = make_pod("r1", phase="Pending", waiting="ImagePullBackOff")
    f = classify_failure(_failed(), [pod])
    assert f.kind == IMAGE_ERROR


def test_run_error_from_nonzero_exit():
    pod = make_pod("r1", phase="Failed", terminated="Error", exit_code=2)
    f = classify_failure(_failed(), [pod])
    assert f.kind == RUN_ERROR and f.exit_code == 2


def test_oom_wins_over_run_error():
    # An OOM kill usually also yields a non-zero exit; OOM is the more actionable root cause.
    oom = make_pod("r1", phase="Failed", terminated="OOMKilled", exit_code=137)
    f = classify_failure(_failed(), [oom])
    assert f.kind == OOM


def test_timeout_wins_over_pod_signals():
    pod = make_pod("r1", phase="Failed", terminated="Error", exit_code=1)
    f = classify_failure(_failed(reason="DeadlineExceeded"), [pod])
    assert f.kind == TIMEOUT


def test_no_failure_returns_none():
    assert classify_failure(JobStatus(name="x", phase="active"), []).kind == NONE


def test_failed_without_pod_signal_is_unknown():
    assert classify_failure(_failed(), []).kind == UNKNOWN


def test_classify_failure_never_crashes_on_malformed_pods():
    """BUG-029: classification must never raise on a malformed/forged pods shape — it degrades to
    UNKNOWN, honoring the documented 'classification never crashes' invariant (cf. job.py _as_int).
    The ``... or []`` fallbacks only catch falsy values, so a truthy non-list conditions/
    containerStatuses, or a non-dict pod element, used to crash the scanners with AttributeError."""
    malformed = [
        [{"status": {"conditions": "broken"}, "metadata": {"name": "p"}}],   # conditions: non-list
        [None, {"status": {}}],                                              # None pod element
        ["just-a-string"],                                                   # non-dict pod element
        [{"status": {"containerStatuses": "x"}}],                            # containerStatuses: non-list
        [{"status": {"conditions": ["x", 5]}}],                             # non-dict condition elements
    ]
    for pods in malformed:
        assert classify_failure(_failed(), pods).kind == UNKNOWN  # must not raise
    # A well-formed OOM pod still classifies correctly (the guards don't suppress real signals).
    oom = [{"status": {"containerStatuses": [
        {"name": "c", "state": {"terminated": {"reason": "OOMKilled", "exitCode": 137}}}]},
        "metadata": {"name": "p"}}]
    assert classify_failure(_failed(), oom).kind == OOM


async def test_controller_diagnose_lists_pods_and_classifies(tmp_path):
    kube = FakeKubeClient()
    kube.program("r1", phases=["failed"],
                 pods=[make_pod("r1", phase="Failed", terminated="OOMKilled", exit_code=137)])
    orch = BenchmarkOrchestrator(kube, workspace=tmp_path)
    f = await orch.diagnose("r1", namespace="bench")
    assert f.kind == OOM and f.exit_code == 137
