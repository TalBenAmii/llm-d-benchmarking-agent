"""Orchestrator state tests: DOE checkpoint/resume + real-time pod-log streaming.

Merged from test_orchestrator_checkpoint.py + test_orchestrator_logstream.py. Each source
file's original module docstring is preserved verbatim as a comment under its separator below.
"""
from __future__ import annotations

import asyncio
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
from app.orchestrator.job import LABEL_MANAGED, LABEL_RUN, LABEL_SWEEP, MANAGED_BY, JobSpec
from app.orchestrator.kube import KubeClient
from tests.orchestrator_fakes import FakeKubeClient, make_pod

# ── test_orchestrator_checkpoint.py ──
# Phase 22 — DOE checkpoint/resume for long sweeps.
#
# A sweep interrupted at treatment k/N resumes from k+1, consistent with the stateless design
# (proposal §3.3/§4): the per-sweep progress lives in a cluster **ConfigMap** (the source of
# truth), NOT in local workspace files. On resume, already-completed treatments are skipped and
# only the remainder run; prior outcomes are merged so the final result covers all N. Resume is
# idempotent — re-running the same sweep id continues rather than restarting.
#
# All hermetic: FakeKubeClient persists the checkpoint ConfigMap in-memory exactly as
# `kubectl apply`/`kubectl get` would. No real cluster, no GPU, no network.

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


@pytest.mark.parametrize("bad", ["not-a-configmap", 7, ["a", "b"], "x"])
def test_parse_checkpoint_survives_non_dict_configmap(bad):
    """BUG-062 sibling: ``kube.parse_items`` does NOT filter non-dict ``items`` elements, and
    ``CheckpointStore.load`` hands ``cms[0]`` (the first element of that unfiltered list) straight
    to ``parse_checkpoint``. A forged/corrupt ``kubectl get configmaps -o json`` whose ``items[0]``
    is a non-dict (a bare string / number / list) is TRUTHY, so it bypassed the ``not configmap``
    guard and raised ``AttributeError: 'str' object has no attribute 'get'`` — crashing
    ``reconstruct_sweep`` (the sweep restart-recovery path) and breaking the documented "never an
    error" contract. Fails before the fix (raises); passes after (degrades to an empty checkpoint,
    exactly like the absent/corrupt-JSON cases above)."""
    cp = parse_checkpoint(SWEEP, bad)  # must NOT raise
    assert cp.sweep_id == SWEEP
    assert cp.treatments == {}


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
    """A checkpoint ConfigMap write (mutating `kubectl apply`, approval-gated) can raise
    for one treatment — approval declined, a transient apply error. The sweep's
    per-treatment isolation must hold for that path too: the OTHER treatments must
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
            # surviving treatments actually run). Mirrors an approval/transient apply error
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



# ── test_orchestrator_logstream.py ──
# Phase 21 — real-time benchmark-pod log streaming.
#
# While a benchmark Job runs, its pod logs are followed in a background task and each line is
# surfaced as a live event (the SAME ``output`` event the UI already renders) — not just at the
# end of the run. The tail is cancelled when the Job reaches a terminal state, and a failing tail
# never affects the run. All hermetic against the FakeKubeClient — no cluster, no network.

def _spec_logstream(run_id="r1", **kw):
    base = dict(run_id=run_id, namespace="bench", image="img",
                command=["llmdbenchmark", "run"], session_id="sessA")
    base.update(kw)
    return JobSpec(**base)


# ---- run_with_retries surfaces pod logs as live events, in order ----------

async def test_run_with_retries_streams_pod_logs_in_order(tmp_path):
    kube = FakeKubeClient()
    # The attempt's run-id is "<base>-a1"; program its live log stream + a watch progression
    # long enough for the tail to drain while the Job is still active.
    kube.program("r1-a1", phases=["active", "active", "active", "succeeded"],
                 log_lines=["starting benchmark", "warming up", "load point 1/3",
                            "load point 2/3", "benchmark complete: 30/30 ok"])
    orch = BenchmarkOrchestrator(kube, workspace=tmp_path)

    seen: list[str] = []

    async def on_log_line(line: str) -> None:
        seen.append(line)

    outcome = await orch.run_with_retries(_spec_logstream(), max_attempts=1, poll_interval=0,
                                          on_log_line=on_log_line)

    assert outcome.succeeded is True
    # Every programmed line surfaced, in the order produced.
    assert seen == ["starting benchmark", "warming up", "load point 1/3",
                    "load point 2/3", "benchmark complete: 30/30 ok"]
    assert kube.stream_started == ["r1-a1"]   # the tail actually followed this attempt's pod


async def test_streaming_disabled_when_no_sink(tmp_path):
    # With no on_log_line, the run behaves exactly as before — no tail is started.
    kube = FakeKubeClient()
    kube.program("r1-a1", phases=["active", "succeeded"], log_lines=["should not stream"])
    orch = BenchmarkOrchestrator(kube, workspace=tmp_path)

    outcome = await orch.run_with_retries(_spec_logstream(), max_attempts=1, poll_interval=0)

    assert outcome.succeeded is True
    assert kube.stream_started == []          # no tail launched at all


# ---- a failing tail must NEVER fail the run -------------------------------

async def test_failing_log_stream_does_not_fail_run(tmp_path):
    kube = FakeKubeClient()
    kube.program("r1-a1", phases=["active", "active", "succeeded"],
                 log_lines=["line-1", "line-2-then-boom", "never-reached"])
    kube.stream_raises = {"r1-a1"}            # the stream raises after the first line
    orch = BenchmarkOrchestrator(kube, workspace=tmp_path)

    seen: list[str] = []

    async def on_log_line(line: str) -> None:
        seen.append(line)

    outcome = await orch.run_with_retries(_spec_logstream(), max_attempts=1, poll_interval=0,
                                          on_log_line=on_log_line)

    # The run still succeeded despite the tail raising mid-stream...
    assert outcome.succeeded is True
    # ...and whatever lines arrived before the failure were still surfaced (best-effort).
    assert "line-1" in seen
    assert "never-reached" not in seen


async def test_raising_sink_does_not_fail_run(tmp_path):
    # A sink (the UI emit) that raises on a line must not abort the tail or the run.
    kube = FakeKubeClient()
    kube.program("r1-a1", phases=["active", "active", "succeeded"],
                 log_lines=["a", "b", "c"])
    orch = BenchmarkOrchestrator(kube, workspace=tmp_path)

    seen: list[str] = []

    async def on_log_line(line: str) -> None:
        seen.append(line)
        if line == "b":
            raise RuntimeError("sink blew up on b")

    outcome = await orch.run_with_retries(_spec_logstream(), max_attempts=1, poll_interval=0,
                                          on_log_line=on_log_line)

    assert outcome.succeeded is True
    # The tail kept going after the sink raised on "b".
    assert seen == ["a", "b", "c"]


# ---- the tail is cancelled at terminal state ------------------------------

async def test_tail_cancelled_on_terminal_state(tmp_path):
    # An UNBOUNDED stream (more lines than the watch will run for) must be cancelled at
    # terminal state rather than streaming forever / blocking the run from returning.
    kube = FakeKubeClient()
    kube.program("r1-a1", phases=["active", "succeeded"],
                 log_lines=[f"line-{i}" for i in range(10_000)])
    kube.stream_line_delay = 0.01             # slow enough that not all 10k lines can drain
    orch = BenchmarkOrchestrator(kube, workspace=tmp_path)

    seen: list[str] = []

    async def on_log_line(line: str) -> None:
        seen.append(line)

    # If the tail were NOT cancelled at terminal state, this would take ~100s (10k * 0.01);
    # bounding it proves the run returns promptly and the tail is reaped.
    outcome = await asyncio.wait_for(
        orch.run_with_retries(_spec_logstream(), max_attempts=1, poll_interval=0, on_log_line=on_log_line),
        timeout=10.0,
    )
    assert outcome.succeeded is True
    assert len(seen) < 10_000                 # the tail was cancelled before draining everything


# ---- a sweep streams each treatment, attributable + isolated --------------

async def test_sweep_streams_each_treatment_tagged(tmp_path):
    kube = FakeKubeClient()
    kube.program("t1-a1", phases=["active", "active", "succeeded"],
                 log_lines=["t1 starting", "t1 done"])
    kube.program("t2-a1", phases=["active", "active", "succeeded"],
                 log_lines=["t2 starting", "t2 done"])
    orch = BenchmarkOrchestrator(kube, workspace=tmp_path)

    seen: list[str] = []

    async def on_log_line(line: str) -> None:
        seen.append(line)

    out = await orch.run_sweep([_spec_logstream("t1"), _spec_logstream("t2")], max_parallel=2,
                               max_attempts=1, poll_interval=0, on_log_line=on_log_line)

    assert sorted(out.succeeded) == ["t1", "t2"]
    # Lines are prefixed with the LOGICAL treatment run-id (the base the user reasons about,
    # not the internal attempt id) so interleaved output stays attributable...
    assert "[t1] t1 starting" in seen
    assert "[t1] t1 done" in seen
    assert "[t2] t2 starting" in seen
    assert "[t2] t2 done" in seen
    # ...and EVERY surfaced line is tagged (no bare, un-attributable lines).
    assert all(line.startswith("[t1] ") or line.startswith("[t2] ") for line in seen)
    # Per-treatment order is preserved within each treatment's own lines.
    t1 = [ln for ln in seen if ln.startswith("[t1] ")]
    assert t1 == ["[t1] t1 starting", "[t1] t1 done"]


async def test_sweep_one_failing_tail_does_not_sink_others(tmp_path):
    kube = FakeKubeClient()
    kube.program("t1-a1", phases=["active", "active", "succeeded"],
                 log_lines=["t1-l1", "t1-boom", "t1-l3"])
    kube.program("t2-a1", phases=["active", "active", "succeeded"],
                 log_lines=["t2-l1", "t2-l2"])
    kube.stream_raises = {"t1-a1"}            # t1's tail blows up; t2 must be unaffected
    orch = BenchmarkOrchestrator(kube, workspace=tmp_path)

    seen: list[str] = []

    async def on_log_line(line: str) -> None:
        seen.append(line)

    out = await orch.run_sweep([_spec_logstream("t1"), _spec_logstream("t2")], max_parallel=2,
                               max_attempts=1, poll_interval=0, on_log_line=on_log_line)

    assert sorted(out.succeeded) == ["t1", "t2"]   # both runs still succeed
    assert "[t2] t2-l1" in seen and "[t2] t2-l2" in seen   # t2 fully streamed


# ---- the streaming primitive itself ---------------------------------------

async def test_stream_log_lines_selects_by_run_label(tmp_path):
    kube = FakeKubeClient()
    kube.program("r1", log_lines=["one", "two", "three"])
    out: list[str] = []
    async for ln in kube.stream_log_lines(namespace="bench", selector=f"{LABEL_RUN}=r1"):
        out.append(ln)
    assert out == ["one", "two", "three"]


def test_fake_satisfies_kube_client_protocol():
    # The fake (and therefore the real client it mirrors) must satisfy the extended protocol,
    # so the new stream_log_lines method is part of the contract, not an ad-hoc addition.
    assert isinstance(FakeKubeClient(), KubeClient)


if __name__ == "__main__":   # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
