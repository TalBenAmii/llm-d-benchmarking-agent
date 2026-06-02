"""Phase 21 — real-time benchmark-pod log streaming.

While a benchmark Job runs, its pod logs are followed in a background task and each line is
surfaced as a live event (the SAME ``output`` event the UI already renders) — not just at the
end of the run. The tail is cancelled when the Job reaches a terminal state, and a failing tail
never affects the run. All hermetic against the FakeKubeClient — no cluster, no network.
"""
from __future__ import annotations

import asyncio

import pytest

from app.orchestrator.controller import BenchmarkOrchestrator
from app.orchestrator.job import LABEL_RUN, JobSpec
from app.orchestrator.kube import KubeClient
from tests.orchestrator_fakes import FakeKubeClient


def _spec(run_id="r1", **kw):
    base = dict(run_id=run_id, namespace="bench", image="img",
                command=["llmdbenchmark", "run"], session_id="sessA")
    base.update(kw)
    return JobSpec(**base)


async def _collect(coro_fn, *, lines_sink):
    """Run an orchestrator coroutine and return (result, captured_lines)."""
    return await coro_fn


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

    outcome = await orch.run_with_retries(_spec(), max_attempts=1, poll_interval=0,
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

    outcome = await orch.run_with_retries(_spec(), max_attempts=1, poll_interval=0)

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

    outcome = await orch.run_with_retries(_spec(), max_attempts=1, poll_interval=0,
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

    outcome = await orch.run_with_retries(_spec(), max_attempts=1, poll_interval=0,
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
        orch.run_with_retries(_spec(), max_attempts=1, poll_interval=0, on_log_line=on_log_line),
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

    out = await orch.run_sweep([_spec("t1"), _spec("t2")], max_parallel=2,
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

    out = await orch.run_sweep([_spec("t1"), _spec("t2")], max_parallel=2,
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
