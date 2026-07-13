"""Orchestrator agent-tool tests: orchestrate_benchmark_run + orchestrate_sweep + the
parallel-sweep controller path.

Merged from test_orchestrator_tool.py + test_orchestrator_sweep_tool.py +
test_orchestrator_sweep.py. Each source file's original module docstring is preserved verbatim
as a comment under its separator below.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.config import Settings
from app.orchestrator.checkpoint import SweepCheckpoint, build_configmap_manifest
from app.orchestrator.controller import BenchmarkOrchestrator
from app.orchestrator.job import LABEL_SESSION, JobSpec, job_name
from app.security.policy import CommandPolicy
from app.tools.context import ToolContext, ToolError
from app.tools.registry import dispatch
from app.tools.run.orchestrate import _sweep_run_id, orchestrate_benchmark_run, orchestrate_sweep
from tests.flows.catalog_snapshot import frozen_catalog
from tests.flows.harness import CaptureRunner
from tests.orchestrator_fakes import FakeKubeClient, make_pod

# ── test_orchestrator_tool.py ──
# Phase 3e — the orchestrate_benchmark_run agent tool: wires the orchestrator to the agent,
# end-to-end through dispatch + the policy-allowed kubectl runner (CaptureRunner), no cluster.

SUCCEEDED_JOB = json.dumps({"items": [{
    "metadata": {"name": "llmd-bench-x", "labels": {}},
    "status": {"succeeded": 1, "conditions": [{"type": "Complete", "status": "True"}]},
}]})

# Phase 24: orchestrate_benchmark_run now gates submission on a real endpoint-readiness check.
# These tests exercise the submit/watch/retry/manifest mechanics, so they stand the endpoint
# up READY by default — the gate is transparent when the inference endpoint is serving (a
# Service with a ready backing address). Tests asserting the gate BLOCKS live in
# tests/orchestrator/test_endpoint_readiness.py.
ENDPOINTS_READY = json.dumps({"items": [
    {"metadata": {"name": "kubernetes"}, "subsets": [{"addresses": [{"ip": "10.96.0.1"}]}]},
    {"metadata": {"name": "llm-d-inference"}, "subsets": [{"addresses": [{"ip": "10.244.0.7"}]}]},
]})


def _ctx_tool(tmp_path, *, canned=None, image=""):
    settings = Settings(_env_file=None, repos_dir=tmp_path / "repos",
                        workspace_dir=tmp_path / "ws", orchestrator_image=image)

    async def approve(kind, payload):
        return True

    # Default the endpoint to READY so the readiness gate passes; a test can override.
    canned = {"get endpoints": ENDPOINTS_READY, **(canned or {})}
    runner = CaptureRunner(settings.repo_paths, canned=canned)
    ctx = ToolContext(
        settings=settings, policy=CommandPolicy.from_file(settings.command_policy_path),
        runner=runner, workspace=settings.resolved_workspace_dir / "sessions" / "s1",
        request_approval=approve,
    )
    frozen = frozen_catalog()
    ctx._catalog = frozen
    ctx.catalog = lambda *, refresh=False: frozen
    return ctx, runner


async def test_tool_requires_an_image(tmp_path):
    ctx, runner = _ctx_tool(tmp_path, image="")  # none configured, none passed
    with pytest.raises(ToolError):
        await orchestrate_benchmark_run(ctx, namespace="bench", spec="cicd/kind",
                                        harness="inference-perf", workload="sanity_random.yaml")
    assert runner.calls == []  # refused before touching the cluster


async def test_tool_submits_watches_and_succeeds(tmp_path):
    ctx, runner = _ctx_tool(tmp_path, canned={"get jobs": SUCCEEDED_JOB})
    res = await dispatch(ctx, "orchestrate_benchmark_run", {
        "namespace": "bench", "spec": "cicd/kind", "harness": "inference-perf",
        "workload": "sanity_random.yaml", "image": "ghcr.io/llm-d/bench:0",
        "poll_interval": 0, "watch": True,
    })
    assert res["succeeded"] is True and res["dead_lettered"] is False
    applies = [c["argv"] for c in runner.calls if c["argv"][:2] == ["kubectl", "apply"]]
    assert applies and applies[-1][-2:] == ["-n", "bench"]      # a Job was applied to the ns


async def test_tool_streams_pod_logs_as_output_events(tmp_path):
    """Phase 21 end-to-end: through dispatch + the REAL RealKubeClient + the policy-allowed
    `kubectl logs -f` runner path, the benchmark pod's log lines surface as `output` events
    (the SAME event the UI renders) DURING the run — not just at the end."""
    pod_logs = "starting benchmark\nload point 1/2\nload point 2/2\nbenchmark complete: 30/30 ok"
    ctx, runner = _ctx_tool(tmp_path, canned={"get jobs": SUCCEEDED_JOB, "logs": pod_logs})

    events: list[tuple[str, dict]] = []

    async def emit(t, p):
        events.append((t, p))

    ctx.emit = emit

    res = await dispatch(ctx, "orchestrate_benchmark_run", {
        "namespace": "bench", "spec": "cicd/kind", "harness": "inference-perf",
        "workload": "sanity_random.yaml", "image": "ghcr.io/llm-d/bench:0",
        "poll_interval": 0, "watch": True,
    })
    assert res["succeeded"] is True

    # The pod log lines were emitted as `output` events, in order, via the standard transport.
    output_lines = [p["line"] for (t, p) in events if t == "output"]
    for expected in pod_logs.splitlines():
        assert expected in output_lines
    assert output_lines.index("starting benchmark") < output_lines.index("benchmark complete: 30/30 ok")

    # And it really used the policy-allowed `kubectl logs -f` path (read-only, argv-only).
    log_calls = [c["argv"] for c in runner.calls if c["argv"][:2] == ["kubectl", "logs"]]
    assert log_calls and "-f" in log_calls[-1]


async def test_tool_submit_only_does_not_watch(tmp_path):
    ctx, runner = _ctx_tool(tmp_path, image="ghcr.io/llm-d/bench:0")
    res = await dispatch(ctx, "orchestrate_benchmark_run", {
        "namespace": "bench", "spec": "cicd/kind", "watch": False,
    })
    assert res["submitted"] is True and res["job"].startswith("llmd-bench-")
    assert not any(c["argv"][:3] == ["kubectl", "get", "jobs"] for c in runner.calls)  # never watched


async def test_tool_retries_transient_then_succeeds(tmp_path):
    """End-to-end through dispatch: max_attempts>1 flows into the orchestrator, a transient
    failure retries as a distinct Job (-a1, -a2), and the run finally succeeds."""
    from app.security.runner import RunResult

    failed_job = json.dumps({"items": [{"metadata": {"name": "j"}, "status": {
        "failed": 1, "conditions": [{"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded"}]}}]})

    class _SeqRunner(CaptureRunner):
        """Returns a FAILED job for the first `get jobs`, SUCCEEDED for the next. The endpoint
        is READY (canned) so the Phase 24 readiness gate passes and the run reaches submission."""
        def __init__(self, repo_paths):
            super().__init__(repo_paths, canned={"get endpoints": ENDPOINTS_READY})
            self._gj = [failed_job, SUCCEEDED_JOB]
            self._i = 0

        async def execute(self, logical_argv, entry, *, on_line=None, timeout=None, cwd=None, extra_env=None):
            if "get jobs" in " ".join(logical_argv):
                out = self._gj[min(self._i, len(self._gj) - 1)]
                self._i += 1
                self.calls.append({"argv": list(logical_argv), "entry": entry, "cwd": None})
                return RunResult(exit_code=0, duration_s=0.0, real_argv=list(logical_argv), cwd=None, output=out)
            return await super().execute(logical_argv, entry, on_line=on_line, timeout=timeout, cwd=cwd, extra_env=extra_env)

    settings = Settings(_env_file=None, repos_dir=tmp_path / "repos",
                        workspace_dir=tmp_path / "ws", orchestrator_image="img")

    async def approve(kind, payload):
        return True

    runner = _SeqRunner(settings.repo_paths)
    ctx = ToolContext(settings=settings, policy=CommandPolicy.from_file(settings.command_policy_path),
                      runner=runner, workspace=settings.resolved_workspace_dir / "sessions" / "s1",
                      request_approval=approve)
    frozen = frozen_catalog()
    ctx._catalog = frozen
    ctx.catalog = lambda *, refresh=False: frozen

    res = await dispatch(ctx, "orchestrate_benchmark_run", {
        "namespace": "bench", "spec": "cicd/kind", "max_attempts": 2, "poll_interval": 0, "watch": True,
    })
    assert res["succeeded"] is True and res["dead_lettered"] is False
    runids = [a["run_id"] for a in res["attempts"]]
    assert len(runids) == 2 and runids[0].endswith("-a1") and runids[1].endswith("-a2")
    applies = [c for c in runner.calls if c["argv"][:2] == ["kubectl", "apply"]]
    assert len(applies) == 2   # two distinct Job submissions


async def test_tool_default_command_embeds_run_invocation(tmp_path):
    ctx, runner = _ctx_tool(tmp_path, image="ghcr.io/llm-d/bench:0")
    await dispatch(ctx, "orchestrate_benchmark_run", {
        "namespace": "bench", "spec": "cicd/kind", "harness": "inference-perf",
        "workload": "sanity_random.yaml", "watch": False,
    })
    import yaml
    manifest = yaml.safe_load(next((ctx.workspace / "jobs").glob("*.yaml")).read_text())
    cmd = manifest["spec"]["template"]["spec"]["containers"][0]["command"]
    assert cmd == ["llmdbenchmark", "--spec", "cicd/kind", "run", "-p", "bench",
                   "-l", "inference-perf", "-w", "sanity_random.yaml"]
    assert manifest["spec"]["backoffLimit"] == 0
    # No SA configured/passed → the pod uses the namespace default (no serviceAccountName key).
    assert "serviceAccountName" not in manifest["spec"]["template"]["spec"]


async def test_tool_runs_job_under_configured_service_account(tmp_path):
    """Phase 8: the least-privilege SA the deploy creates flows into the submitted Job, so an
    in-cluster orchestrated run authenticates as that SA (resolving the Phase-3 RBAC gap)."""
    import yaml

    settings = Settings(_env_file=None, repos_dir=tmp_path / "repos", workspace_dir=tmp_path / "ws",
                        orchestrator_image="ghcr.io/llm-d/bench:0",
                        orchestrator_service_account="llm-d-benchmarking-agent")

    async def approve(kind, payload):
        return True

    runner = CaptureRunner(settings.repo_paths, canned={"get endpoints": ENDPOINTS_READY})
    ctx = ToolContext(settings=settings, policy=CommandPolicy.from_file(settings.command_policy_path),
                      runner=runner, workspace=settings.resolved_workspace_dir / "sessions" / "s1",
                      request_approval=approve)
    frozen = frozen_catalog()
    ctx._catalog = frozen
    ctx.catalog = lambda *, refresh=False: frozen

    await dispatch(ctx, "orchestrate_benchmark_run", {
        "namespace": "bench", "spec": "cicd/kind", "watch": False,
    })
    manifest = yaml.safe_load(next((ctx.workspace / "jobs").glob("*.yaml")).read_text())
    assert manifest["spec"]["template"]["spec"]["serviceAccountName"] == "llm-d-benchmarking-agent"



# ── test_orchestrator_sweep_tool.py ──
# G7 — the orchestrate_sweep agent tool: exposes the orchestrator's parallel-treatment
# run_sweep (concurrency cap + per-treatment retry/dead-letter + cluster-checkpointed resume)
# as a real benchmark path, end-to-end through dispatch + the policy-allowed kubectl runner
# (CaptureRunner), no cluster. Mirrors tests/orchestrator/test_orchestrator_tools.py's hermetic setup.

FAILED_JOB = json.dumps({"items": [{
    "metadata": {"name": "llmd-bench-y", "labels": {}},
    "status": {"failed": 1, "conditions": [
        {"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded"}]},
}]})


def _ctx(tmp_path, *, canned=None, image="ghcr.io/llm-d/bench:0"):
    settings = Settings(_env_file=None, repos_dir=tmp_path / "repos",
                        workspace_dir=tmp_path / "ws", orchestrator_image=image)

    async def approve(kind, payload):
        return True

    canned = {"get endpoints": ENDPOINTS_READY, **(canned or {})}
    runner = CaptureRunner(settings.repo_paths, canned=canned)
    ctx = ToolContext(
        settings=settings, policy=CommandPolicy.from_file(settings.command_policy_path),
        runner=runner, workspace=settings.resolved_workspace_dir / "sessions" / "s1",
        request_approval=approve,
    )
    frozen = frozen_catalog()
    ctx._catalog = frozen
    ctx.catalog = lambda *, refresh=False: frozen
    return ctx, runner


def _applied_run_ids(runner) -> list[str]:
    """The run-id label on each applied Job manifest (skips ConfigMap checkpoint applies)."""
    import yaml
    out = []
    for c in runner.calls:
        if c["argv"][:2] != ["kubectl", "apply"]:
            continue
        path = c["argv"][c["argv"].index("-f") + 1]
        manifest = yaml.safe_load(Path(path).read_text())
        if manifest.get("kind") == "Job":
            out.append(manifest["metadata"]["labels"]["llmd-bench/run-id"])
    return out


async def test_sweep_requires_an_image(tmp_path):
    ctx, runner = _ctx(tmp_path, image="")  # none configured, none passed
    with pytest.raises(ToolError):
        await orchestrate_sweep(ctx, namespace="bench", spec="cicd/kind",
                                treatments=[{"name": "t1"}])
    assert runner.calls == []  # refused before touching the cluster


async def test_sweep_rejects_duplicate_treatment_names(tmp_path):
    ctx, runner = _ctx(tmp_path)
    with pytest.raises(ToolError):
        await orchestrate_sweep(ctx, namespace="bench", spec="cicd/kind",
                                treatments=[{"name": "dup"}, {"name": "dup"}])
    assert runner.calls == []  # rejected before any cluster access


async def test_sweep_runs_all_treatments_in_parallel_and_succeeds(tmp_path):
    ctx, runner = _ctx(tmp_path, canned={"get jobs": SUCCEEDED_JOB})
    res = await dispatch(ctx, "orchestrate_sweep", {
        "namespace": "bench", "spec": "cicd/kind", "harness": "inference-perf",
        "treatments": [
            {"name": "rate-10", "workload": "sanity_random.yaml"},
            {"name": "rate-20", "workload": "sanity_random.yaml"},
            {"name": "rate-40", "workload": "sanity_random.yaml"},
        ],
        "max_parallel": 2, "max_attempts": 1, "poll_interval": 0,
    })
    assert res["all_succeeded"] is True
    assert res["n_treatments"] == 3 and res["n_succeeded"] == 3 and res["n_dead_lettered"] == 0
    assert sorted(res["succeeded"]) == ["rate-10", "rate-20", "rate-40"]
    # Each treatment was submitted as its own distinct Job (a base run-id from its name).
    run_ids = _applied_run_ids(runner)
    assert len(run_ids) == 3
    sid = res["sweep_id"]
    assert {rid.rsplit("-a", 1)[0] for rid in run_ids} == {
        f"{sid}-rate-10", f"{sid}-rate-20", f"{sid}-rate-40"}
    # Per-treatment outcome rows carry the human treatment name back (not internal run-ids).
    assert {t["treatment"] for t in res["treatments"]} == {"rate-10", "rate-20", "rate-40"}


async def test_sweep_dead_letters_one_without_sinking_the_rest(tmp_path):
    """A persistently-failing treatment dead-letters; the others still succeed (the proposal's
    cross-treatment isolation). Needles are matched by run-id substring, most-specific first."""
    ctx, runner = _ctx(tmp_path)
    sid = "swfix01"
    bad_run_id = _sweep_run_id(sid, "bad")  # f"{sid}-bad"
    runner._canned = {
        f"run-id={bad_run_id}": FAILED_JOB,   # this treatment's Job reads FAILED
        "get endpoints": ENDPOINTS_READY,
        "get jobs": SUCCEEDED_JOB,            # every other treatment succeeds
    }
    res = await dispatch(ctx, "orchestrate_sweep", {
        "namespace": "bench", "spec": "cicd/kind", "sweep_id": sid,
        "treatments": [{"name": "good-a"}, {"name": "bad"}, {"name": "good-b"}],
        "max_parallel": 3, "max_attempts": 1, "poll_interval": 0,
    })
    assert res["all_succeeded"] is False
    assert sorted(res["succeeded"]) == ["good-a", "good-b"]
    assert res["dead_lettered"] == ["bad"]
    # The whole sweep still completed (all three treatments were attempted as Jobs).
    assert len(_applied_run_ids(runner)) == 3


async def test_sweep_resumes_skipping_completed_treatments(tmp_path):
    """With a cluster checkpoint already recording a treatment COMPLETED, a resume (same
    sweep_id + treatments) SKIPS it — no Job submitted for it — and merges its prior outcome."""
    sid = "swresume"
    done_run_id = _sweep_run_id(sid, "t1")
    checkpoint = SweepCheckpoint(sweep_id=sid)
    checkpoint.record_completed(done_run_id, succeeded=True, dead_lettered=False, fault_kind=None)
    cm = build_configmap_manifest(sid, checkpoint, namespace="bench")
    canned_cm = json.dumps({"items": [cm]})

    ctx, runner = _ctx(tmp_path, canned={"get configmaps": canned_cm, "get jobs": SUCCEEDED_JOB})
    res = await dispatch(ctx, "orchestrate_sweep", {
        "namespace": "bench", "spec": "cicd/kind", "sweep_id": sid,
        "treatments": [{"name": "t1"}, {"name": "t2"}, {"name": "t3"}],
        "max_parallel": 3, "max_attempts": 1, "poll_interval": 0,
    })
    # t1 was resumed from the checkpoint (skipped), t2/t3 ran fresh; the roll-up covers all 3.
    assert res["resumed"] == ["t1"]
    assert sorted(res["succeeded"]) == ["t1", "t2", "t3"]
    assert res["n_treatments"] == 3
    # No Job was submitted for the already-completed t1; only t2 and t3 were applied.
    submitted = {rid.rsplit("-a", 1)[0] for rid in _applied_run_ids(runner)}
    assert submitted == {f"{sid}-t2", f"{sid}-t3"}


def test_sweep_run_id_is_pure_in_sweep_id_and_name_for_empty_slug_names():
    """_sweep_run_id is documented as 'a PURE function of (sweep_id, name)' so that a resume
    maps the SAME treatment name to the SAME run-id (and the cluster checkpoint can skip it).
    For a name that slugs to nothing (e.g. all punctuation/non-ASCII — schema-allowed: the
    `name` field has no pattern), the fallback must NOT depend on the treatment's POSITION in
    the list: otherwise reordering / inserting a treatment between a sweep and its same-sweep_id
    resume yields a different run-id, and the already-completed treatment is re-run as a
    DUPLICATE Job — breaking the resume idempotency invariant. Purity in position is now
    STRUCTURAL: the signature is (sweep_id, name) only, so position cannot leak in."""
    sid = "sw-fixed"
    empty_slug_name = "###"            # _slug("###") == "" → the fallback path
    # Same (sweep_id, name) is stable across calls (it never saw position to begin with).
    assert _sweep_run_id(sid, empty_slug_name) == _sweep_run_id(sid, empty_slug_name)
    # Distinct empty-slug names must still map to DISTINCT run-ids (no fallback collision).
    assert _sweep_run_id(sid, "###") != _sweep_run_id(sid, "***")


async def test_sweep_resume_reordered_empty_slug_treatment_is_skipped_not_rerun(tmp_path):
    """End-to-end resume idempotency for an empty-slug-named treatment under REORDERING: a
    treatment whose name slugs to nothing is completed + checkpointed in pass 1, then a resume
    (same sweep_id) supplies the SAME treatments in a DIFFERENT order. The completed treatment
    must be SKIPPED (its run-id is stable per name), not re-submitted as a duplicate Job."""
    sid = "swreorder"
    empty_name = "###"
    done_run_id = _sweep_run_id(sid, empty_name)
    checkpoint = SweepCheckpoint(sweep_id=sid)
    checkpoint.record_completed(done_run_id, succeeded=True, dead_lettered=False, fault_kind=None)
    cm = build_configmap_manifest(sid, checkpoint, namespace="bench")
    canned_cm = json.dumps({"items": [cm]})

    ctx, runner = _ctx(tmp_path, canned={"get configmaps": canned_cm, "get jobs": SUCCEEDED_JOB})
    # Resume with the completed empty-slug treatment now at a DIFFERENT position (was index 1,
    # now index 2). With an index-dependent fallback its run-id would change and it would re-run.
    res = await dispatch(ctx, "orchestrate_sweep", {
        "namespace": "bench", "spec": "cicd/kind", "sweep_id": sid,
        "treatments": [{"name": "x"}, {"name": empty_name}],
        "max_parallel": 2, "max_attempts": 1, "poll_interval": 0,
    })
    # The completed empty-slug treatment is resumed (skipped), only "x" runs fresh.
    assert res["resumed"] == [empty_name]
    submitted = {rid.rsplit("-a", 1)[0] for rid in _applied_run_ids(runner)}
    assert submitted == {_sweep_run_id(sid, "x")}  # NOT the empty-slug treatment


async def test_sweep_checkpoint_false_writes_no_configmap(tmp_path):
    ctx, runner = _ctx(tmp_path, canned={"get jobs": SUCCEEDED_JOB})
    res = await dispatch(ctx, "orchestrate_sweep", {
        "namespace": "bench", "spec": "cicd/kind", "checkpoint": False,
        "treatments": [{"name": "a"}, {"name": "b"}],
        "max_parallel": 2, "max_attempts": 1, "poll_interval": 0,
    })
    assert res["checkpointed"] is False and res["all_succeeded"] is True
    # No checkpoint ConfigMap was ever applied (stateless one-shot sweep).
    import yaml
    applied_kinds = []
    for c in runner.calls:
        if c["argv"][:2] == ["kubectl", "apply"]:
            path = c["argv"][c["argv"].index("-f") + 1]
            applied_kinds.append(yaml.safe_load(Path(path).read_text()).get("kind"))
    assert "ConfigMap" not in applied_kinds


async def test_sweep_gates_on_endpoint_readiness(tmp_path):
    """Not-ready endpoint → NOTHING submitted (mirrors orchestrate_benchmark_run's gate)."""
    not_ready = json.dumps({"items": [
        {"metadata": {"name": "llm-d-inference"}, "subsets": []},  # Service exists, no ready addrs
    ]})
    ctx, runner = _ctx(tmp_path, canned={"get endpoints": not_ready, "get jobs": SUCCEEDED_JOB})
    res = await dispatch(ctx, "orchestrate_sweep", {
        "namespace": "bench", "spec": "cicd/kind",
        "treatments": [{"name": "t1"}, {"name": "t2"}],
        "max_parallel": 2, "max_attempts": 1, "poll_interval": 0,
    })
    assert res["submitted"] is False and res["ready"] is False
    # No Job was applied (the sweep was gated before any submission).
    assert _applied_run_ids(runner) == []


async def test_sweep_generates_and_returns_a_sweep_id(tmp_path):
    ctx, runner = _ctx(tmp_path, canned={"get jobs": SUCCEEDED_JOB})
    res = await dispatch(ctx, "orchestrate_sweep", {
        "namespace": "bench", "spec": "cicd/kind",
        "treatments": [{"name": "only"}],
        "max_parallel": 1, "max_attempts": 1, "poll_interval": 0,
    })
    assert res["sweep_id"].startswith("sw-") and res["checkpointed"] is True


async def test_sweep_rejects_a_malformed_sweep_id(tmp_path):
    ctx, runner = _ctx(tmp_path)
    with pytest.raises(ToolError):
        await orchestrate_sweep(ctx, namespace="bench", spec="cicd/kind",
                                sweep_id="Bad_ID/with.slash",
                                treatments=[{"name": "t1"}])
    assert runner.calls == []



# ── test_orchestrator_sweep.py ──
# Phase 3e — parallel sweep orchestration + cleanup. Treatments run as parallel Jobs under
# a concurrency cap, each with its own retry/dead-letter budget; a persistently-failing
# treatment dead-letters without sinking the sweep. Cleanup reaps only terminal Jobs. Hermetic.

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


async def test_cleanup_only_terminal_false_reaps_active(tmp_path):
    # only_terminal=False reaps even an ACTIVE run (distinct from the default, which preserves it).
    kube = FakeKubeClient()
    kube.program("r1", phases=["succeeded"], labels={LABEL_SESSION: "sessA"})
    kube.program("r2", phases=["active"], labels={LABEL_SESSION: "sessA"})
    orch = BenchmarkOrchestrator(kube, workspace=tmp_path)

    deleted = await orch.cleanup(namespace="bench", session_id="sessA", only_terminal=False)
    assert sorted(deleted) == sorted([job_name("r1"), job_name("r2")])
    assert ("bench", job_name("r2")) in kube.deleted   # the ACTIVE job WAS reaped


async def test_sweep_isolates_a_raising_treatment(tmp_path):
    # If a treatment RAISES (not just fails), the others must still complete — the sweep's
    # gather is exception-isolated per treatment.
    from pathlib import Path

    import yaml as _yaml

    class _RaisingApply(FakeKubeClient):
        async def apply(self, manifest_path, *, namespace):
            m = _yaml.safe_load(Path(manifest_path).read_text())
            if "t2" in m["metadata"]["labels"]["llmd-bench/run-id"]:
                raise RuntimeError("simulated apply failure for t2")
            return await super().apply(manifest_path, namespace=namespace)

    kube = _RaisingApply()
    kube.program("t1-a1", phases=["succeeded"])
    kube.program("t3-a1", phases=["succeeded"])
    orch = BenchmarkOrchestrator(kube, workspace=tmp_path)

    out = await orch.run_sweep([_spec("t1"), _spec("t2"), _spec("t3")],
                               max_parallel=3, max_attempts=1, poll_interval=0)
    assert sorted(out.succeeded) == ["t1", "t3"]      # survivors
    assert out.dead_lettered == ["t2"]                # the raiser was isolated + dead-lettered
