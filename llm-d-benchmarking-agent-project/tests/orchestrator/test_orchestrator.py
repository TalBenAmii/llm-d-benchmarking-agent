"""Phase 3a — orchestrator foundation: the allowlist additions for managing K8s Jobs via
kubectl, and the RealKubeClient that shells out to those allowlisted commands.

The KubeClient is exercised against the hermetic CaptureRunner (records argv, replays canned
output) so we assert exact commands + JSON parsing + workspace confinement with no cluster.
"""
from __future__ import annotations

import json
import os

import pytest

from app.config import Settings
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
    JobStatus,
    build_job_manifest,
    classify_job_status,
    job_name,
)
from app.orchestrator.kube import KubeError, RealKubeClient, parse_items
from app.security.allowlist import MUTATING, READ_ONLY, Allowlist
from app.tools.context import ToolContext
from tests.flows.catalog_snapshot import frozen_catalog
from tests.flows.harness import CaptureRunner
from tests.orchestrator_fakes import FakeKubeClient, make_job, make_pod

# ---- allowlist: the new kubectl surface -----------------------------------

ALLOW = [
    (["kubectl", "apply", "-f", "/ws/job.yaml", "-n", "bench"], MUTATING),
    (["kubectl", "apply", "--filename", "/ws/run-1.yml", "--namespace", "bench"], MUTATING),
    (["kubectl", "get", "jobs", "-n", "bench", "-l", "run-id=abc", "-o", "json"], READ_ONLY),
    (["kubectl", "get", "jobs", "-l", "run-id=abc", "--watch", "-o", "json"], READ_ONLY),
    (["kubectl", "get", "pods", "-n", "bench", "-l", "job-name=x", "-o", "json"], READ_ONLY),
    (["kubectl", "logs", "-l", "job-name=x", "-n", "bench", "--tail", "200", "-f"], READ_ONLY),
    (["kubectl", "delete", "job", "my-job", "-n", "bench"], MUTATING),
    (["kubectl", "delete", "jobs", "my-job", "--ignore-not-found"], MUTATING),
]

DENY = [
    ["kubectl", "delete", "pod", "my-pod"],            # delete restricted to jobs
    ["kubectl", "delete", "namespace", "bench"],       # cannot remove arbitrary objects
    ["kubectl", "apply", "-f", "/etc/passwd"],         # -f must be a .yaml
    ["kubectl", "apply", "-f", "/ws/job.yaml;rm"],     # shell metachar screen
    ["kubectl", "logs", "somepod"],                    # logs by selector only, no positional
]


@pytest.mark.parametrize("argv,mode", ALLOW, ids=[" ".join(a) for a, _ in ALLOW])
def test_orchestrator_kubectl_allowed(allowlist, catalog, argv, mode):
    d = allowlist.validate(argv, catalog=catalog)
    assert d.allowed and d.mode == mode


@pytest.mark.parametrize("argv", DENY, ids=[" ".join(a) for a in DENY])
def test_orchestrator_kubectl_denied(allowlist, catalog, argv):
    assert not allowlist.validate(argv, catalog=catalog).allowed


# ---- parse_items ----------------------------------------------------------

def test_parse_items_list_object():
    out = json.dumps({"kind": "List", "items": [{"metadata": {"name": "a"}}, {"metadata": {"name": "b"}}]})
    items = parse_items(out)
    assert [i["metadata"]["name"] for i in items] == ["a", "b"]


def test_parse_items_single_object_is_wrapped():
    out = json.dumps({"kind": "Job", "metadata": {"name": "solo"}})
    assert parse_items(out) == [{"kind": "Job", "metadata": {"name": "solo"}}]


def test_parse_items_empty_and_garbage():
    assert parse_items("") == []
    assert parse_items("not json") == []
    assert parse_items(json.dumps({"kind": "List", "items": None})) == []


def test_parse_items_drops_non_dict_elements():
    # Defense-in-depth at the SOURCE: a forged/corrupt `kubectl get ... -o json` whose `items`
    # carries non-dict elements (a bare string / number / list / null) must be filtered out, so
    # NO consumer (controller.classify_job_status, classify_failure, parse_checkpoint) can
    # AttributeError on a `.get` of a non-dict. Mirrors the sibling parsers
    # (readiness.diagnostics._parse_items, tools.probe._items_from_json).
    out = json.dumps({
        "kind": "List",
        "items": [{"metadata": {"name": "good"}}, "bad-string", 42, None, ["nested"]],
    })
    items = parse_items(out)
    assert items == [{"metadata": {"name": "good"}}]
    assert all(isinstance(i, dict) for i in items)


# ---- RealKubeClient against the CaptureRunner -----------------------------

def _ctx(tmp_path, *, canned=None):
    settings = Settings(_env_file=None, repos_dir=tmp_path / "repos", workspace_dir=tmp_path / "ws")

    async def approve(kind, payload):
        return True

    runner = CaptureRunner(settings.repo_paths, canned=canned or {})
    ctx = ToolContext(
        settings=settings,
        allowlist=Allowlist.from_file(settings.allowlist_path),
        runner=runner,
        workspace=settings.resolved_workspace_dir / "sessions" / "s1",
        request_approval=approve,
    )
    frozen = frozen_catalog()
    ctx._catalog = frozen
    ctx.catalog = lambda *, refresh=False: frozen
    return ctx, runner


async def test_apply_builds_argv_and_confines_to_workspace(tmp_path):
    ctx, runner = _ctx(tmp_path)
    kube = RealKubeClient(ctx)
    manifest = ctx.workspace / "jobs" / "run-1.yaml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("apiVersion: batch/v1\nkind: Job\n")

    res = await kube.apply(manifest, namespace="bench")
    assert res.exit_code == 0
    assert runner.calls[-1]["argv"] == ["kubectl", "apply", "-f", str(manifest.resolve()), "-n", "bench"]


async def test_apply_refuses_manifest_outside_workspace(tmp_path):
    ctx, runner = _ctx(tmp_path)
    kube = RealKubeClient(ctx)
    with pytest.raises(KubeError):
        await kube.apply("/etc/evil.yaml", namespace="bench")
    assert runner.calls == []  # nothing ran


async def test_apply_refuses_symlink_escape(tmp_path):
    # A symlink INSIDE the workspace pointing OUTSIDE must be refused (resolve() follows it).
    ctx, runner = _ctx(tmp_path)
    kube = RealKubeClient(ctx)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "evil.yaml").write_text("kind: Job\n")
    ctx.workspace.mkdir(parents=True, exist_ok=True)
    link = ctx.workspace / "link.yaml"
    os.symlink(outside / "evil.yaml", link)
    with pytest.raises(KubeError):
        await kube.apply(link, namespace="bench")
    assert runner.calls == []


async def test_apply_refuses_dotdot_escape(tmp_path):
    ctx, runner = _ctx(tmp_path)
    kube = RealKubeClient(ctx)
    escape = ctx.workspace / ".." / ".." / ".." / ".." / "evil.yaml"  # resolves above the workspace
    with pytest.raises(KubeError):
        await kube.apply(escape, namespace="bench")
    assert runner.calls == []


async def test_apply_allows_symlink_within_workspace(tmp_path):
    # Positive control: a symlink resolving to a real manifest INSIDE the workspace is fine.
    ctx, runner = _ctx(tmp_path)
    kube = RealKubeClient(ctx)
    real = ctx.workspace / "real" / "m.yaml"
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_text("kind: Job\n")
    link = ctx.workspace / "good.yaml"
    os.symlink(real, link)
    res = await kube.apply(link, namespace="bench")
    assert res.exit_code == 0 and runner.calls  # it ran


async def test_list_jobs_parses_and_passes_selector(tmp_path):
    jobs_json = json.dumps({"kind": "List", "items": [{"metadata": {"name": "j1"}}]})
    ctx, runner = _ctx(tmp_path, canned={"get jobs": jobs_json})
    kube = RealKubeClient(ctx)

    jobs = await kube.list_jobs(namespace="bench", selector="run-id=abc")
    assert [j["metadata"]["name"] for j in jobs] == ["j1"]
    argv = runner.calls[-1]["argv"]
    assert argv == ["kubectl", "get", "jobs", "-n", "bench", "-o", "json", "-l", "run-id=abc"]


async def test_list_jobs_filters_forged_non_dict_items(tmp_path):
    # End-to-end at the boundary: a forged `kubectl get jobs -o json` with a non-dict element
    # must NOT reach a consumer's `.get` — the source filter in parse_items drops it before
    # RealKubeClient.list_jobs returns.
    jobs_json = json.dumps({"kind": "List", "items": [{"metadata": {"name": "j1"}}, "forged"]})
    ctx, runner = _ctx(tmp_path, canned={"get jobs": jobs_json})
    kube = RealKubeClient(ctx)
    assert [j["metadata"]["name"] for j in await kube.list_jobs(namespace="bench")] == ["j1"]


async def test_list_pods_argv(tmp_path):
    ctx, runner = _ctx(tmp_path, canned={"get pods": json.dumps({"items": []})})
    kube = RealKubeClient(ctx)
    await kube.list_pods(namespace="bench", selector="job-name=j1")
    assert runner.calls[-1]["argv"] == ["kubectl", "get", "pods", "-n", "bench", "-o", "json", "-l", "job-name=j1"]


async def test_logs_streams_and_returns_output(tmp_path):
    ctx, runner = _ctx(tmp_path, canned={"logs": "line-1\nline-2"})
    kube = RealKubeClient(ctx)
    out = await kube.logs(namespace="bench", selector="job-name=j1", tail=200, follow=True)
    assert "line-1" in out and "line-2" in out
    assert runner.calls[-1]["argv"] == ["kubectl", "logs", "-l", "job-name=j1", "-n", "bench", "--tail", "200", "-f"]


async def test_delete_job_argv(tmp_path):
    ctx, runner = _ctx(tmp_path)
    kube = RealKubeClient(ctx)
    await kube.delete_job("run-1", namespace="bench")
    assert runner.calls[-1]["argv"] == ["kubectl", "delete", "job", "run-1", "-n", "bench", "--ignore-not-found"]



# ── test_orchestrator_controller.py ──
# Phase 3b — the Job lifecycle controller: manifest generation, submit, watch-to-terminal,
# log streaming, and cluster-only reconstruction. Hermetic (FakeKubeClient, no cluster).

def _spec_controller(run_id="r1", **kw):
    base = dict(run_id=run_id, namespace="bench", image="ghcr.io/llm-d/bench:0",
                command=["llmdbenchmark", "run"], session_id="sess1", spec="cicd/kind",
                harness="inference-perf", workload="sanity_random.yaml")
    base.update(kw)
    return JobSpec(**base)


# ---- manifest (pure) ------------------------------------------------------

def test_build_job_manifest_shape():
    m = build_job_manifest(_spec_controller(active_deadline_seconds=600, cpu="2", memory="4Gi"))
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
    # BUG-023: a forged/corrupt status with NON-NUMERIC counts must not raise ValueError out of
    # classify (which would abort the whole watch/reconstruct loop). Non-numeric counts read as 0,
    # so a status with only bogus counts and no terminal signal is PENDING — never a crash.
    forged = {"metadata": {"name": "j"}, "status": {"active": "lots", "succeeded": "x", "failed": None}}
    st = classify_job_status(forged)
    assert st.phase == PENDING and st.active == 0 and st.succeeded == 0 and st.failed == 0


def test_classify_survives_malformed_conditions():
    # A forged/corrupt `kubectl get job -o json` whose `conditions` is not a clean list of
    # dicts must NOT raise out of classify (same crash class as BUG-029 for classify_failure):
    # a non-dict condition element (`c.get(...)` on a str/None) would AttributeError and abort
    # the whole watch()/reconstruct() loop the orchestrator's stateless recovery depends on.
    # Non-dict elements are ignored; a real terminal signal among them still classifies.
    bad_elems = {"metadata": {"name": "j"}, "status": {
        "active": 1, "conditions": ["Complete", None, 7, {"type": "Failed", "status": "True",
                                                          "reason": "BackoffLimitExceeded"}]}}
    st = classify_job_status(bad_elems)
    assert st.phase == FAILED and st.reason == "BackoffLimitExceeded"  # real cond still honored
    # conditions that is not a list at all (e.g. a forged scalar/mapping) → ignored, no crash.
    non_list = {"metadata": {"name": "j"}, "status": {"active": 1, "conditions": "Complete"}}
    assert classify_job_status(non_list).phase == ACTIVE
    # all condition elements malformed + no terminal count → PENDING, never a raise.
    all_bad = {"metadata": {"name": "j"}, "status": {"conditions": [None, "x", 3]}}
    assert classify_job_status(all_bad).phase == PENDING


def test_classify_survives_non_dict_job_obj():
    # `kube.parse_items` does NOT filter non-dict `items` elements, so a forged/corrupt
    # `kubectl get jobs -o json` whose `items` carries a non-dict element (a bare string,
    # null, or number) flows straight into `classify_job_status(j)` from `status()`/
    # `reconstruct()`. The interior was hardened for malformed CHILDREN (BUG-023 counts,
    # BUG-037 conditions) but NOT for a non-dict TOP-LEVEL job — `job_obj.get(...)` would
    # AttributeError and abort the whole watch()/reconstruct() loop. The sibling
    # `classify_failure` (BUG-029) already filters non-dict pods; this is the missing twin.
    for bad in ("not-a-job", None, 7, ["a", "b"]):
        st = classify_job_status(bad)  # must NOT raise
        assert st.phase == ABSENT and st.active == 0 and st.succeeded == 0 and st.failed == 0


# ---- submit ---------------------------------------------------------------

async def test_submit_writes_manifest_and_applies(tmp_path):
    kube = FakeKubeClient()
    orch = BenchmarkOrchestrator(kube, workspace=tmp_path)
    name = await orch.submit(_spec_controller(active_deadline_seconds=300))
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



# ── test_orchestrator_faults.py ──
# Phase 3c — fault classification: map a failed Job + its pods to a stable fault kind
# (OOM / timeout / eviction / unschedulable / image / run error). Pure + via the controller.

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



# ── test_orchestrator_retry.py ──
# Phase 3d — retry + dead-letter. Transient faults (eviction, unknown) get a fresh Job
# attempt; deterministic faults (OOM) dead-letter immediately; exhausting the budget
# dead-letters. Each attempt is a distinct Job (<run_id>-a<N>). Hermetic.

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
