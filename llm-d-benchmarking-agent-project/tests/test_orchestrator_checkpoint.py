"""Phase 22 — DOE checkpoint/resume for long sweeps.

A sweep interrupted at treatment k/N resumes from k+1, consistent with the stateless design
(proposal §3.3/§4): the per-sweep progress lives in a cluster **ConfigMap** (the source of
truth), NOT in local workspace files. On resume, already-completed treatments are skipped and
only the remainder run; prior outcomes are merged so the final result covers all N. Resume is
idempotent — re-running the same sweep id continues rather than restarting.

All hermetic: FakeKubeClient persists the checkpoint ConfigMap in-memory exactly as
`kubectl apply`/`kubectl get` would. No real cluster, no GPU, no network.
"""
from __future__ import annotations

import json

import pytest

from app.orchestrator.checkpoint import (
    COMPLETED,
    IN_FLIGHT,
    CheckpointStore,
    SweepCheckpoint,
    build_configmap_manifest,
    checkpoint_name,
    parse_checkpoint,
)
from app.orchestrator.controller import BenchmarkOrchestrator
from app.orchestrator.job import LABEL_MANAGED, LABEL_SWEEP, MANAGED_BY, JobSpec
from tests.orchestrator_fakes import FakeKubeClient, make_pod

SWEEP = "sw1"
NS = "bench"


def _spec(run_id: str, **kw) -> JobSpec:
    base = dict(run_id=run_id, namespace=NS, image="img", command=["llmdbenchmark", "run"],
                session_id="sessA", sweep_id=SWEEP)
    base.update(kw)
    return JobSpec(**base)


def _program_success(kube: FakeKubeClient, treatments: list[str]) -> None:
    """Program each treatment's single attempt (`-a1`) to succeed."""
    for t in treatments:
        kube.program(f"{t}-a1", phases=["succeeded"])


def _applied_run_ids(kube: FakeKubeClient) -> list[str]:
    """The logical treatment ids whose JOB manifest was applied (skips ConfigMap applies)."""
    ids = []
    for _ns, manifest in kube.applied:
        if manifest.get("kind") != "Job":
            continue
        rid = manifest["metadata"]["labels"]["llmd-bench/run-id"]
        # strip the -aN attempt suffix back to the logical treatment id
        ids.append(rid.rsplit("-a", 1)[0])
    return ids


# --------------------------------------------------------------------------- pure layer


def test_checkpoint_name_is_dns_safe_and_validated():
    assert checkpoint_name("sw1") == "llmd-bench-sweep-sw1"
    with pytest.raises(ValueError):
        checkpoint_name("Bad_Id")  # uppercase/underscore is not a DNS-1123 label


def test_configmap_manifest_is_labelled_for_reconstruction():
    cp = SweepCheckpoint(sweep_id=SWEEP)
    cp.record_completed("t1", succeeded=True, dead_lettered=False, fault_kind=None)
    cm = build_configmap_manifest(SWEEP, cp, namespace=NS)
    assert cm["kind"] == "ConfigMap"
    assert cm["metadata"]["name"] == "llmd-bench-sweep-sw1"
    assert cm["metadata"]["labels"] == {LABEL_MANAGED: MANAGED_BY, LABEL_SWEEP: SWEEP}
    doc = json.loads(cm["data"]["progress.json"])
    assert doc["treatments"][0]["run_id"] == "t1" and doc["treatments"][0]["state"] == COMPLETED


def test_checkpoint_round_trips_through_a_configmap():
    cp = SweepCheckpoint(sweep_id=SWEEP)
    cp.record_in_flight("t1")
    cp.record_completed("t2", succeeded=False, dead_lettered=True, fault_kind="oom")
    cm = build_configmap_manifest(SWEEP, cp, namespace=NS)
    back = parse_checkpoint(SWEEP, cm)
    assert back.treatments["t1"].state == IN_FLIGHT
    rec = back.treatments["t2"]
    assert rec.state == COMPLETED and rec.dead_lettered and rec.fault_kind == "oom"
    assert back.completed_ids() == {"t2"}
    assert back.is_completed("t2") and not back.is_completed("t1")


def test_parse_checkpoint_tolerates_absent_or_corrupt_configmap():
    assert parse_checkpoint(SWEEP, None).treatments == {}            # no ConfigMap yet
    assert parse_checkpoint(SWEEP, {"data": {}}).treatments == {}    # no progress key
    corrupt = {"data": {"progress.json": "{not json"}}
    assert parse_checkpoint(SWEEP, corrupt).treatments == {}         # unparseable → empty


def test_record_completed_is_not_downgraded_by_a_later_in_flight():
    cp = SweepCheckpoint(sweep_id=SWEEP)
    cp.record_completed("t1", succeeded=True, dead_lettered=False, fault_kind=None)
    cp.record_in_flight("t1")  # idempotent re-entry must NOT clobber a completed record
    assert cp.is_completed("t1")


# --------------------------------------------------------------------------- store I/O


async def test_store_persists_and_loads_via_the_cluster(tmp_path):
    kube = FakeKubeClient()
    store = CheckpointStore(kube, tmp_path)
    # A fresh sweep has no checkpoint in the cluster.
    empty = await store.load(SWEEP, namespace=NS)
    assert empty.treatments == {}

    cp = SweepCheckpoint(sweep_id=SWEEP)
    cp.record_completed("t1", succeeded=True, dead_lettered=False, fault_kind=None)
    await store.write(cp, namespace=NS)
    assert kube.configmap_writes == 1

    # A SEPARATE store (fresh "process") reads the same cluster ConfigMap — stateless recovery.
    reloaded = await CheckpointStore(kube, tmp_path).load(SWEEP, namespace=NS)
    assert reloaded.is_completed("t1")


# --------------------------------------------------------------------------- the acceptance


async def test_resume_runs_only_the_remaining_treatments_and_merges_all(tmp_path):
    """ACCEPTANCE: a sweep records k of N completed, then is re-invoked with the SAME sweep id;
    only the remaining N-k treatments execute and the result merges all N (no duplicate runs).

    k=2, N=5. We simulate the interruption by first running the sweep over only the first two
    treatments (they complete + checkpoint to the cluster ConfigMap), then resume with the full
    five against the SAME fake cluster + sweep id."""
    kube = FakeKubeClient()
    treatments = ["t1", "t2", "t3", "t4", "t5"]
    _program_success(kube, treatments)

    # --- run 1: the first k=2 treatments complete and are checkpointed to the cluster.
    orch = BenchmarkOrchestrator(kube, workspace=tmp_path)
    first = await orch.run_sweep([_spec("t1"), _spec("t2")], max_parallel=2, max_attempts=1,
                                 poll_interval=0, sweep_id=SWEEP, namespace=NS)
    assert sorted(first.succeeded) == ["t1", "t2"]
    assert first.resumed == []                       # nothing to resume on the first pass

    # The checkpoint is in the cluster (a ConfigMap), not a local file.
    cms = await kube.list_configmaps(namespace=NS,
                                     selector=f"{LABEL_SWEEP}={SWEEP}")
    assert len(cms) == 1
    persisted = parse_checkpoint(SWEEP, cms[0])
    assert persisted.completed_ids() == {"t1", "t2"}

    applied_after_run1 = _applied_run_ids(kube)
    assert sorted(applied_after_run1) == ["t1", "t2"]   # only the two ran

    # --- run 2 (RESUME): re-invoke with ALL N=5, same sweep id. Only t3,t4,t5 must execute.
    second = await orch.run_sweep([_spec(t) for t in treatments], max_parallel=2,
                                  max_attempts=1, poll_interval=0, sweep_id=SWEEP, namespace=NS)

    # Only the remaining N-k=3 treatments were newly applied (t1/t2 were NOT re-run).
    newly_applied = _applied_run_ids(kube)[len(applied_after_run1):]
    assert sorted(newly_applied) == ["t3", "t4", "t5"]

    # The merged result covers ALL N treatments, in input order, each succeeded exactly once.
    assert [o.run_id for o in second.outcomes] == treatments
    assert sorted(second.succeeded) == treatments
    assert sorted(second.resumed) == ["t1", "t2"]    # t1,t2 were skipped (resumed from checkpoint)
    assert second.all_succeeded

    # No duplicate runs: t1/t2 each applied exactly once across BOTH invocations.
    all_applied = _applied_run_ids(kube)
    assert all_applied.count("t1") == 1 and all_applied.count("t2") == 1

    # The cluster checkpoint now records all five completed.
    final = await orch.reconstruct_sweep(SWEEP, namespace=NS)
    assert final.completed_ids() == set(treatments)


async def test_resume_is_idempotent_a_full_rerun_runs_nothing(tmp_path):
    """Re-running an already-finished sweep with the same id runs NO treatment (idempotent) and
    still returns the complete merged result from the checkpoint."""
    kube = FakeKubeClient()
    treatments = ["t1", "t2", "t3"]
    _program_success(kube, treatments)
    orch = BenchmarkOrchestrator(kube, workspace=tmp_path)

    full = await orch.run_sweep([_spec(t) for t in treatments], max_parallel=3, max_attempts=1,
                                poll_interval=0, sweep_id=SWEEP, namespace=NS)
    assert full.all_succeeded
    applied_before = list(_applied_run_ids(kube))

    again = await orch.run_sweep([_spec(t) for t in treatments], max_parallel=3, max_attempts=1,
                                 poll_interval=0, sweep_id=SWEEP, namespace=NS)
    # Nothing new applied; every treatment was resumed from the checkpoint.
    assert _applied_run_ids(kube) == applied_before
    assert sorted(again.resumed) == treatments
    assert sorted(again.succeeded) == treatments
    assert [o.run_id for o in again.outcomes] == treatments


async def test_resume_preserves_a_dead_lettered_treatments_outcome(tmp_path):
    """A treatment that dead-lettered (e.g. OOM) before the interruption is preserved as
    dead-lettered on resume — its prior fault is merged, and it is NOT re-run."""
    kube = FakeKubeClient()
    # t1 succeeds; t2 OOMs (deterministic → dead-letter, no retry); both checkpointed in run 1.
    kube.program("t1-a1", phases=["succeeded"])
    kube.program("t2-a1", phases=["failed"],
                 pods=[make_pod("t2-a1", phase="Failed", terminated="OOMKilled", exit_code=137)])
    kube.program("t3-a1", phases=["succeeded"])
    orch = BenchmarkOrchestrator(kube, workspace=tmp_path)

    first = await orch.run_sweep([_spec("t1"), _spec("t2")], max_parallel=2, max_attempts=1,
                                 poll_interval=0, sweep_id=SWEEP, namespace=NS)
    assert first.succeeded == ["t1"] and first.dead_lettered == ["t2"]
    applied_run1 = list(_applied_run_ids(kube))

    # Resume with all three: only t3 runs; t2 stays dead-lettered with its recovered fault.
    second = await orch.run_sweep([_spec("t1"), _spec("t2"), _spec("t3")], max_parallel=2,
                                  max_attempts=1, poll_interval=0, sweep_id=SWEEP, namespace=NS)
    newly = _applied_run_ids(kube)[len(applied_run1):]
    assert newly == ["t3"]                              # only the remaining treatment ran
    assert sorted(second.succeeded) == ["t1", "t3"]
    assert second.dead_lettered == ["t2"]
    t2 = next(o for o in second.outcomes if o.run_id == "t2")
    assert t2.dead_lettered and t2.final_failure is not None and t2.final_failure.kind == "oom"
    assert sorted(second.resumed) == ["t1", "t2"]


async def test_checkpoint_written_on_each_completion_lives_in_cluster_not_workspace(tmp_path):
    """The checkpoint is persisted to the cluster (ConfigMap apply) per completion — verify
    writes happened in the cluster and that no sweep-state file is the source of truth (the
    workspace manifest is only the apply staging file)."""
    kube = FakeKubeClient()
    _program_success(kube, ["t1", "t2"])
    orch = BenchmarkOrchestrator(kube, workspace=tmp_path)
    await orch.run_sweep([_spec("t1"), _spec("t2")], max_parallel=1, max_attempts=1,
                         poll_interval=0, sweep_id=SWEEP, namespace=NS)
    # In-flight + completed writes per treatment ⇒ several ConfigMap applies reached the cluster.
    assert kube.configmap_writes >= 2
    # The cluster holds the authoritative checkpoint.
    cms = await kube.list_configmaps(namespace=NS, selector=f"{LABEL_SWEEP}={SWEEP}")
    assert len(cms) == 1 and parse_checkpoint(SWEEP, cms[0]).completed_ids() == {"t1", "t2"}


async def test_run_sweep_without_sweep_id_is_unchanged_no_checkpoint(tmp_path):
    """Backward compatibility: omitting sweep_id keeps the original stateless behavior — no
    ConfigMap is written, and `resumed` is empty."""
    kube = FakeKubeClient()
    _program_success(kube, ["t1", "t2"])
    orch = BenchmarkOrchestrator(kube, workspace=tmp_path)
    out = await orch.run_sweep([_spec("t1"), _spec("t2")], max_parallel=2, max_attempts=1,
                               poll_interval=0)
    assert out.all_succeeded and out.resumed == []
    assert kube.configmap_writes == 0
    assert await kube.list_configmaps(namespace=NS) == []


async def test_run_sweep_requires_namespace_with_sweep_id(tmp_path):
    kube = FakeKubeClient()
    orch = BenchmarkOrchestrator(kube, workspace=tmp_path)
    with pytest.raises(ValueError, match="namespace is required"):
        await orch.run_sweep([_spec("t1")], sweep_id=SWEEP, poll_interval=0)


async def test_checkpoint_write_failure_for_one_treatment_does_not_sink_the_sweep(tmp_path):
    """A checkpoint ConfigMap write (mutating `kubectl apply`, approval+quota gated) can raise
    for one treatment — quota exhausted mid-sweep, approval declined, a transient apply error.
    The sweep's per-treatment isolation must hold for that path too: the OTHER treatments must
    still complete and the sweep must not abort.

    Reproduces a checkpoint-only isolation gap distinct from
    `test_sweep_isolates_a_raising_treatment` (which has no sweep_id, so the checkpoint-write
    path never runs): there the raise is the JOB apply, INSIDE `_one`'s try/except; here it is
    the CONFIGMAP apply in `_persist_in_flight`/`_persist_completed`, OUTSIDE it."""
    from pathlib import Path

    import yaml as _yaml

    class _CheckpointApplyFails(FakeKubeClient):
        async def apply(self, manifest_path, *, namespace):
            m = _yaml.safe_load(Path(manifest_path).read_text())
            # Fail only the checkpoint ConfigMap apply (the Job applies must succeed so the
            # surviving treatments actually run). Mirrors a quota/approval/transient apply error
            # hitting the checkpoint write specifically.
            if m.get("kind") == "ConfigMap":
                raise RuntimeError("simulated checkpoint ConfigMap apply failure")
            return await super().apply(manifest_path, namespace=namespace)

    kube = _CheckpointApplyFails()
    _program_success(kube, ["t1", "t2", "t3"])
    orch = BenchmarkOrchestrator(kube, workspace=tmp_path)

    out = await orch.run_sweep([_spec("t1"), _spec("t2"), _spec("t3")], max_parallel=2,
                               max_attempts=1, poll_interval=0, sweep_id=SWEEP, namespace=NS)

    # Every treatment ran to a terminal outcome; the failing checkpoint write did NOT abort the
    # sweep. (Before the fix, the un-caught ConfigMap-apply error propagated out of `gather` and
    # the whole run_sweep raised RuntimeError, losing all results.)
    assert [o.run_id for o in out.outcomes] == ["t1", "t2", "t3"]
    assert sorted(out.succeeded) == ["t1", "t2", "t3"]
    assert out.all_succeeded
