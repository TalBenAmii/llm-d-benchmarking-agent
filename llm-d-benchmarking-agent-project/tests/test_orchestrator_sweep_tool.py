"""G7 — the orchestrate_sweep agent tool: exposes the orchestrator's parallel-treatment
run_sweep (concurrency cap + per-treatment retry/dead-letter + cluster-checkpointed resume)
as a real benchmark path, end-to-end through dispatch + the allowlisted kubectl runner
(CaptureRunner), no cluster. Mirrors tests/test_orchestrator_tool.py's hermetic setup."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import Settings
from app.orchestrator.checkpoint import SweepCheckpoint, build_configmap_manifest
from app.security.allowlist import Allowlist
from app.tools.context import ToolContext, ToolError
from app.tools.orchestrate import _sweep_run_id, orchestrate_sweep
from app.tools.registry import dispatch
from tests.flows.catalog_snapshot import frozen_catalog
from tests.flows.harness import CaptureRunner

SUCCEEDED_JOB = json.dumps({"items": [{
    "metadata": {"name": "llmd-bench-x", "labels": {}},
    "status": {"succeeded": 1, "conditions": [{"type": "Complete", "status": "True"}]},
}]})

FAILED_JOB = json.dumps({"items": [{
    "metadata": {"name": "llmd-bench-y", "labels": {}},
    "status": {"failed": 1, "conditions": [
        {"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded"}]},
}]})

# The readiness gate (shared with orchestrate_benchmark_run) wants a Service with a ready
# backing endpoint; stand it up READY by default so the sweep mechanics are exercised.
ENDPOINTS_READY = json.dumps({"items": [
    {"metadata": {"name": "kubernetes"}, "subsets": [{"addresses": [{"ip": "10.96.0.1"}]}]},
    {"metadata": {"name": "llm-d-inference"}, "subsets": [{"addresses": [{"ip": "10.244.0.7"}]}]},
]})


def _ctx(tmp_path, *, canned=None, image="ghcr.io/llm-d/bench:0"):
    settings = Settings(_env_file=None, repos_dir=tmp_path / "repos",
                        workspace_dir=tmp_path / "ws", orchestrator_image=image)

    async def approve(kind, payload):
        return True

    canned = {"get endpoints": ENDPOINTS_READY, **(canned or {})}
    runner = CaptureRunner(settings.repo_paths, canned=canned)
    ctx = ToolContext(
        settings=settings, allowlist=Allowlist.from_file(settings.allowlist_path),
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
