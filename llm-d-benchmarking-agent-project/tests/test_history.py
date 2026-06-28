"""Phase 5 — historical result storage + trends.

Covers the disk-backed HistoryStore (add/list/get/delete, idempotency, cross-session
persistence), the pure trend math (ordering, direction, deltas, filtering), the
result_history tool (validate-before-store, all actions, dispatch + registration), the
ToolContext.history_store() rooting, and the read-only /api/history* endpoints.

Hermetic: real BR v0.2 reports written to temp dirs; an isolated temp workspace root;
no cluster, no GPU, no live runs.
"""
from __future__ import annotations

import copy
import json
import time

import pytest
import yaml
from fastapi.testclient import TestClient

from app.storage.history import (
    HistoryStore,
    available_metrics,
    compute_record_id,
    trend,
)
from app.tools import history as history_tool
from app.tools.registry import dispatch, tool_definitions
from app.tools.schemas import ResultHistoryInput
from app.validation.report import load_report
from tests._helpers import write_br_report

# ---- helpers ---------------------------------------------------------------

def _summary(*, model="m", run_uid="u", ttft_ms=None, out_rate=None, success=100.0):
    s: dict = {"model": model, "run_uid": run_uid, "duration": 10,
               "requests_total": 500, "success_rate_pct": success,
               "latency": {}, "throughput": {}}
    if ttft_ms is not None:
        s["latency"]["ttft"] = {"units": "ms", "mean": ttft_ms, "p99": ttft_ms}
    if out_rate is not None:
        s["throughput"]["output_token_rate"] = {"units": "tokens/s", "mean": out_rate}
    return s


def _add(store, *, stored_at=None, **kw):
    """Add a record and force a deterministic stored_at so trend ordering is testable."""
    summary = _summary(**{k: v for k, v in kw.items()
                          if k in ("model", "run_uid", "ttft_ms", "out_rate", "success")})
    rec, created = store.add(
        summary,
        label=kw.get("label"),
        tags=kw.get("tags"),
        report_path=kw.get("report_path"),
    )
    if stored_at is not None:
        rec.stored_at = stored_at
        (store.dir / f"{rec.id}.json").write_text(json.dumps(rec.to_json(), indent=2))
    return rec, created


def _write_report(dirpath, base: dict, ttft_s: float, out_rate: float, uid="run-x"):
    write_br_report(dirpath, base, ttft_s=ttft_s, out_rate=out_rate, p99=ttft_s, uid=uid)


# ---- HistoryStore ----------------------------------------------------------

def test_store_add_get_and_persist_across_instances(tmp_path):
    store = HistoryStore(tmp_path)
    rec, created = store.add(_summary(ttft_ms=120, out_rate=400), label="baseline",
                             tags=["8B"], report_path="/runs/a")
    assert created is True
    assert rec.label == "baseline" and rec.tags == ["8B"] and rec.model == "m"

    # A fresh store over the SAME root reloads the record (cross-session persistence).
    store2 = HistoryStore(tmp_path)
    got = store2.get(rec.id)
    assert got is not None and got.label == "baseline" and got.summary["latency"]["ttft"]["mean"] == 120
    assert [r.id for r in store2.list()] == [rec.id]


def test_store_is_idempotent_for_same_report(tmp_path):
    store = HistoryStore(tmp_path)
    s = _summary(ttft_ms=100, out_rate=300)
    rec1, c1 = store.add(s, report_path="/runs/a", label="first")
    rec2, c2 = store.add(s, report_path="/runs/a", label="second-attempt")
    assert c1 is True and c2 is False          # second store is a no-op
    assert rec1.id == rec2.id
    assert len(store.list()) == 1
    # The original record (and its label) is preserved, not overwritten.
    assert store.get(rec1.id).label == "first"


def test_store_distinguishes_different_runs(tmp_path):
    store = HistoryStore(tmp_path)
    store.add(_summary(ttft_ms=100, run_uid="a"), report_path="/runs/a")
    store.add(_summary(ttft_ms=200, run_uid="b"), report_path="/runs/b")
    assert len(store.list()) == 2


def test_store_list_filters_by_tag_and_model(tmp_path):
    store = HistoryStore(tmp_path)
    store.add(_summary(model="big", run_uid="1", ttft_ms=10), tags=["baseline"], report_path="/1")
    store.add(_summary(model="big", run_uid="2", ttft_ms=20), tags=["tuned"], report_path="/2")
    store.add(_summary(model="small", run_uid="3", ttft_ms=30), tags=["baseline"], report_path="/3")
    assert {r.run_uid for r in store.list(tag="baseline")} == {"1", "3"}
    assert {r.run_uid for r in store.list(model="big")} == {"1", "2"}
    assert {r.run_uid for r in store.list(tag="baseline", model="big")} == {"1"}


def test_store_delete(tmp_path):
    store = HistoryStore(tmp_path)
    rec, _ = store.add(_summary(ttft_ms=10), report_path="/a")
    assert store.delete(rec.id) is True
    assert store.get(rec.id) is None
    assert store.delete(rec.id) is False        # already gone


def test_store_rejects_unsafe_id(tmp_path):
    store = HistoryStore(tmp_path)
    assert store.get("../../etc/passwd") is None
    assert store.delete("a/b") is False


def test_store_record_carries_bundle_id_and_provenance(tmp_path):
    # Reproducibility: a record can carry an optional bundle_id + provenance dict; both round-trip.
    store = HistoryStore(tmp_path)
    prov = {"bundle_id": "b16", "repos": {"llm-d": {"sha": "abc"}}, "dirty": False,
            "regenerate_command": "llmdbenchmark run -c x.yaml -p ns"}
    rec, created = store.add(_summary(ttft_ms=120), report_path="/runs/a",
                            bundle_id="b16", provenance=prov)
    assert created is True and rec.bundle_id == "b16" and rec.provenance == prov
    # A fresh store over the same root reloads them.
    got = HistoryStore(tmp_path).get(rec.id)
    assert got is not None and got.bundle_id == "b16"
    assert got.provenance["regenerate_command"].startswith("llmdbenchmark run -c")


def test_old_records_without_bundle_fields_still_load(tmp_path):
    # A record written BEFORE the bundle_id/provenance fields existed must still load (additive).
    store = HistoryStore(tmp_path)
    store.dir.mkdir(parents=True, exist_ok=True)
    legacy = {
        "id": "legacy01", "stored_at": 1.0, "label": "old", "tags": [],
        "session_id": None, "report_path": "/r", "model": "m", "run_uid": "u",
        "spec": None, "harness": None, "workload": None, "namespace": None,
        "summary": {"model": "m"},
        # NOTE: no bundle_id / provenance keys at all.
    }
    (store.dir / "legacy01.json").write_text(json.dumps(legacy))
    got = store.get("legacy01")
    assert got is not None and got.label == "old"
    assert got.bundle_id is None and got.provenance is None


def test_store_add_defaults_bundle_fields_to_none(tmp_path):
    # A normal add (no bundle args) leaves the new fields None — unchanged behavior for callers.
    store = HistoryStore(tmp_path)
    rec, _ = store.add(_summary(ttft_ms=10), report_path="/a")
    assert rec.bundle_id is None and rec.provenance is None


def test_store_skips_corrupt_record_files(tmp_path):
    store = HistoryStore(tmp_path)
    store.add(_summary(ttft_ms=10), report_path="/a")
    store.dir.mkdir(parents=True, exist_ok=True)
    (store.dir / "garbage.json").write_text("{not json")
    (store.dir / "wrongshape.json").write_text('{"no": "summary"}')
    # The good record survives; the junk is silently skipped, never crashes list().
    assert len(store.list()) == 1


def test_compute_record_id_is_stable_and_content_addressed():
    s = _summary(ttft_ms=100, out_rate=300)
    a = compute_record_id(s, "/runs/a")
    assert a == compute_record_id(copy.deepcopy(s), "/runs/a")        # deterministic
    assert a != compute_record_id(_summary(ttft_ms=999), "/runs/a")  # value-sensitive
    assert a != compute_record_id(s, "/runs/b")                      # path-sensitive


# ---- trend math ------------------------------------------------------------

def test_trend_orders_oldest_to_newest_with_delta_and_direction(tmp_path):
    store = HistoryStore(tmp_path)
    # Insert out of chronological order; trend must reorder by stored_at.
    _add(store, run_uid="late", ttft_ms=150, stored_at=300.0, label="late")
    _add(store, run_uid="early", ttft_ms=100, stored_at=100.0, label="early")
    _add(store, run_uid="mid", ttft_ms=120, stored_at=200.0, label="mid")
    t = trend(store.list(), "ttft")
    assert t["better"] == "lower" and t["units"] == "ms" and t["n"] == 3
    assert [p["label"] for p in t["points"]] == ["early", "mid", "late"]
    assert [p["value"] for p in t["points"]] == [100, 120, 150]
    # first(100) -> last(150) = +50, +50%
    assert t["first_to_last"]["delta_abs"] == 50
    assert t["first_to_last"]["delta_pct"] == 50.0


def test_trend_throughput_direction_is_higher(tmp_path):
    store = HistoryStore(tmp_path)
    _add(store, run_uid="a", out_rate=200, stored_at=1.0)
    _add(store, run_uid="b", out_rate=260, stored_at=2.0)
    t = trend(store.list(), "output_token_rate")
    assert t["better"] == "higher"
    assert t["first_to_last"]["delta_pct"] == 30.0


def test_trend_skips_records_missing_the_metric(tmp_path):
    store = HistoryStore(tmp_path)
    _add(store, run_uid="has", ttft_ms=100, stored_at=1.0)
    _add(store, run_uid="lacks", out_rate=300, stored_at=2.0)   # no ttft
    t = trend(store.list(), "ttft")
    assert t["n"] == 1 and t["first_to_last"]["delta_pct"] is None


def test_trend_unknown_metric_reports_available(tmp_path):
    t = trend([], "nonsense")
    assert "error" in t and "ttft" in t["available_metrics"]


def test_available_metrics_covers_latency_and_throughput():
    m = available_metrics()
    assert {"ttft", "tpot", "request_latency", "output_token_rate", "success_rate_pct"} <= set(m)


# ---- Phase 49: §3.4 standard/serving metrics in the trend store -------------
#
# The 3 standard metrics (KV-cache hit rate, GPU utilization, schedule-delay queue-depth
# proxy) are already surfaced into the summary / analysis / report card; the only remaining
# slice is trending them. These tests assert the metric REGISTRATION (mechanism) and that a
# summary built by the real summarizer (with monitoring-produced results.observability)
# trends end-to-end through the store. Hermetic: pure report fixtures, temp dirs, no cluster.

from app.storage.history import _TREND_METRICS  # noqa: E402
from app.validation.report import summarize_report  # noqa: E402

# The three new keys and their authoritative direction labels (mirror standard_metrics.yaml).
_STANDARD_TREND_DIRECTIONS = {
    "kv_cache_hit_rate": "higher",
    "gpu_utilization": "higher",
    "schedule_delay": "lower",
}


def _obs_report(*, uid, kv, gpu, qdepth):
    """A BR-shaped report whose results.observability carries the 3 §3.4 metrics in the
    standardized ResourceMetrics shape (what `--monitoring` / Phase 27 produces)."""
    def _pct(mean):
        return {"units": "percent", "mean": mean, "p50": mean, "p99": mean}
    return {
        "version": "0.2",
        "run": {"uid": uid},
        "results": {
            "observability": {
                "components": [
                    {
                        "component_label": "vllm-svc-0",
                        "aggregate": {
                            "cache_hit_rate": _pct(kv),
                            "gpu_utilization": _pct(gpu),
                            "waiting_requests": {"units": "count", "mean": qdepth, "p99": qdepth},
                        },
                    }
                ]
            }
        },
    }


def test_trend_metrics_include_three_standard_serving_metrics():
    keys = set(available_metrics())
    assert {"kv_cache_hit_rate", "gpu_utilization", "schedule_delay"} <= keys
    for metric, direction in _STANDARD_TREND_DIRECTIONS.items():
        dotted, registered_dir = _TREND_METRICS[metric]
        # Direction matches the §3.4 catalog (kv/gpu higher, schedule_delay lower).
        assert registered_dir == direction
        # Each is keyed to the nested standard-metrics stat path the summarizer fills.
        assert dotted.startswith("standard_metrics.") and dotted.endswith(".value")
    # The hard latency/throughput objectives are untouched (no accidental clobber).
    assert _TREND_METRICS["ttft"] == ("latency.ttft", "lower")
    assert _TREND_METRICS["output_token_rate"] == ("throughput.output_token_rate", "higher")


def test_trend_over_standard_metric_flows_through_summarizer(tmp_path):
    # Build real summaries from observability-bearing reports and store them out of order;
    # the trend must reorder oldest->newest, read the standard-metric stat, and label the
    # direction factually — proving the registered path resolves end-to-end.
    store = HistoryStore(tmp_path)
    for uid, kv, sa in (("late", 80.0, 300.0), ("early", 40.0, 100.0), ("mid", 60.0, 200.0)):
        summary = summarize_report(_obs_report(uid=uid, kv=kv, gpu=70.0, qdepth=5.0))
        assert summary["standard_metrics"] is not None  # monitoring produced it
        rec, _ = store.add(summary, label=uid, report_path=f"/runs/{uid}")
        rec.stored_at = sa
        (store.dir / f"{rec.id}.json").write_text(json.dumps(rec.to_json(), indent=2))

    t = trend(store.list(), "kv_cache_hit_rate")
    assert t["better"] == "higher" and t["units"] == "percent" and t["n"] == 3
    assert [p["label"] for p in t["points"]] == ["early", "mid", "late"]
    assert [p["value"] for p in t["points"]] == [40.0, 60.0, 80.0]
    # first(40) -> last(80) = +40, +100%
    assert t["first_to_last"]["delta_abs"] == 40.0
    assert t["first_to_last"]["delta_pct"] == 100.0


def test_trend_schedule_delay_is_lower_better_queue_depth_proxy(tmp_path):
    store = HistoryStore(tmp_path)
    # schedule_delay maps to the waiting_requests queue-depth proxy (count units, lower better).
    for uid, qd, sa in (("a", 3.0, 1.0), ("b", 9.0, 2.0)):
        summary = summarize_report(_obs_report(uid=uid, kv=50.0, gpu=70.0, qdepth=qd))
        rec, _ = store.add(summary, label=uid, report_path=f"/runs/{uid}")
        rec.stored_at = sa
        (store.dir / f"{rec.id}.json").write_text(json.dumps(rec.to_json(), indent=2))
    t = trend(store.list(), "schedule_delay")
    assert t["better"] == "lower" and t["units"] == "count"
    assert [p["value"] for p in t["points"]] == [3.0, 9.0]  # queue depth grew (saturating)
    assert t["first_to_last"]["delta_abs"] == 6.0


def test_trend_standard_metric_skips_runs_without_monitoring(tmp_path):
    # A run done WITHOUT monitoring carries no standard_metrics -> it contributes no point
    # (the series skips it), never fabricating a value.
    store = HistoryStore(tmp_path)
    with_mon = summarize_report(_obs_report(uid="mon", kv=55.0, gpu=70.0, qdepth=4.0))
    store.add(with_mon, label="monitored", report_path="/runs/mon")
    # _summary() (no observability) yields standard_metrics == None.
    plain = _summary(model="m", run_uid="plain", ttft_ms=120)
    assert "standard_metrics" not in plain or plain.get("standard_metrics") is None
    store.add(plain, label="unmonitored", report_path="/runs/plain")
    t = trend(store.list(), "gpu_utilization")
    assert t["n"] == 1 and [p["label"] for p in t["points"]] == ["monitored"]


# ---- ToolContext.history_store rooting -------------------------------------

def test_context_history_store_rooted_outside_session_dir():
    # A session workspace is <root>/sessions/<id>; the store must root at <root> so it's
    # shared across sessions, not nested inside one session's dir.
    from pathlib import Path

    from app.config import get_settings
    from app.security.allowlist import Allowlist
    from app.security.runner import CommandRunner
    from app.tools.context import ToolContext
    from tests.conftest import ALLOWLIST_PATH

    s = get_settings()
    root = Path("/tmp/kqg-test-root-xyz")
    ctx = ToolContext(
        settings=s, allowlist=Allowlist.from_file(ALLOWLIST_PATH),
        runner=CommandRunner(s.repo_paths), workspace=root / "sessions" / "sess1",
    )
    assert ctx.history_store().dir == root / "history"


# ---- result_history tool (real reports on disk) ----------------------------

async def test_tool_store_validates_then_persists(tool_ctx, br_example, tmp_path):
    base = load_report(br_example)
    run = tmp_path / "run"
    _write_report(run, base, ttft_s=0.15, out_rate=400.0, uid="run-good")
    out = await history_tool.result_history(
        tool_ctx, action="store", source=str(run), label="my baseline",
        tags=["8B", "baseline"], harness="inference-perf",
    )
    assert out["stored"] is True and out["created"] is True
    assert out["record"]["label"] == "my baseline" and out["record"]["tags"] == ["8B", "baseline"]
    assert out["summary"]["model"] is not None
    # It really landed in the cross-session store.
    listed = await history_tool.result_history(tool_ctx, action="list")
    assert listed["n"] == 1 and listed["records"][0]["label"] == "my baseline"


async def test_tool_store_refuses_invalid_report(tool_ctx, tmp_path):
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "benchmark_report_v0.2.yaml").write_text(yaml.safe_dump({"version": "0.2", "run": {}}))
    out = await history_tool.result_history(tool_ctx, action="store", source=str(bad))
    assert out["stored"] is False and "schema validation" in out["reason"]
    # Nothing was persisted.
    assert (await history_tool.result_history(tool_ctx, action="list"))["n"] == 0


async def test_tool_store_requires_source(tool_ctx):
    out = await history_tool.result_history(tool_ctx, action="store")
    assert out["stored"] is False and "source" in out["reason"]


async def test_tool_store_no_report_found(tool_ctx, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    out = await history_tool.result_history(tool_ctx, action="store", source=str(empty))
    assert out["stored"] is False and "no Benchmark Report" in out["reason"]


async def test_tool_store_is_idempotent(tool_ctx, br_example, tmp_path):
    base = load_report(br_example)
    run = tmp_path / "run"
    _write_report(run, base, 0.15, 400.0, uid="run-idem")
    first = await history_tool.result_history(tool_ctx, action="store", source=str(run))
    second = await history_tool.result_history(tool_ctx, action="store", source=str(run))
    assert first["created"] is True and second["created"] is False
    assert (await history_tool.result_history(tool_ctx, action="list"))["n"] == 1


async def test_tool_get_and_delete(tool_ctx, br_example, tmp_path):
    base = load_report(br_example)
    run = tmp_path / "run"
    _write_report(run, base, 0.15, 400.0, uid="run-gd")
    stored = await history_tool.result_history(tool_ctx, action="store", source=str(run))
    rid = stored["record"]["id"]
    got = await history_tool.result_history(tool_ctx, action="get", record_id=rid)
    assert got["found"] is True and "summary" in got
    deleted = await history_tool.result_history(tool_ctx, action="delete", record_id=rid)
    assert deleted["deleted"] is True
    assert (await history_tool.result_history(tool_ctx, action="get", record_id=rid))["found"] is False


async def test_tool_trend_over_stored_runs(tool_ctx, br_example, tmp_path):
    base = load_report(br_example)
    # Two stored runs with rising TTFT; trend must report the +direction factually.
    for i, ttft in enumerate((0.10, 0.20)):
        run = tmp_path / f"run{i}"
        _write_report(run, base, ttft, 300.0, uid=f"run-{i}")
        await history_tool.result_history(tool_ctx, action="store", source=str(run),
                                          tags=["sweep"])
        time.sleep(0.01)  # ensure distinct stored_at ordering
    t = await history_tool.result_history(tool_ctx, action="trend", metric="ttft",
                                          filter_tag="sweep")
    assert t["n"] == 2 and t["better"] == "lower"
    assert t["points"][-1]["value"] > t["points"][0]["value"]   # got slower over time


async def test_tool_trend_requires_metric(tool_ctx):
    out = await history_tool.result_history(tool_ctx, action="trend")
    assert "error" in out and "available_metrics" in out


async def test_tool_unknown_action(tool_ctx):
    out = await history_tool.result_history(tool_ctx, action="frobnicate")
    assert "error" in out and "store" in out["valid_actions"]


# ---- registry / schema wiring ----------------------------------------------

def test_result_history_registered():
    assert "result_history" in {d["name"] for d in tool_definitions()}


def test_result_history_schema_accepts_actions():
    for a in ("store", "list", "get", "trend", "delete"):
        assert ResultHistoryInput(action=a).action == a
    with pytest.raises(ValueError):
        ResultHistoryInput(action="nope")


async def test_dispatch_result_history_store(tool_ctx, br_example, tmp_path):
    base = load_report(br_example)
    run = tmp_path / "run"
    _write_report(run, base, 0.12, 350.0, uid="run-disp")
    out = await dispatch(tool_ctx, "result_history",
                         {"action": "store", "source": str(run), "label": "disp"})
    assert out["stored"] is True
    listed = await dispatch(tool_ctx, "result_history", {"action": "list"})
    assert listed["n"] == 1


# ---- API endpoints (read-only browser / trends) ----------------------------

@pytest.fixture()
def history_app(tmp_path, monkeypatch):
    """The real FastAPI app with the history store pointed at an isolated temp root."""
    from app import main
    store = HistoryStore(tmp_path)
    monkeypatch.setattr(main, "_history_store", lambda: store)
    return main.app, store


def test_api_history_lists_stored_records(history_app):
    app, store = history_app
    store.add(_summary(model="big", run_uid="1", ttft_ms=120), label="baseline",
              tags=["8B"], report_path="/1")
    with TestClient(app) as client:
        r = client.get("/api/history")
        assert r.status_code == 200
        body = r.json()
        assert body["records"][0]["label"] == "baseline"
        assert "ttft" in body["metrics"]
        # tag/model filters work through the query string
        assert client.get("/api/history", params={"tag": "8B"}).json()["records"]
        assert client.get("/api/history", params={"tag": "nope"}).json()["records"] == []


def test_api_history_trend(history_app):
    app, store = history_app
    _add(store, run_uid="a", ttft_ms=100, stored_at=1.0)
    _add(store, run_uid="b", ttft_ms=150, stored_at=2.0)
    with TestClient(app) as client:
        t = client.get("/api/history/trend", params={"metric": "ttft"}).json()
        assert t["n"] == 2 and t["better"] == "lower"
        assert t["first_to_last"]["delta_pct"] == 50.0


def test_list_and_trend_survive_corrupt_stored_at(tmp_path):
    """BUG-020: a record whose on-disk ``stored_at`` is non-numeric (null/string — it bypasses the
    validated ``add()`` path) must not crash ``list()``/``trend()`` for EVERY record. The sort key
    would otherwise raise ``TypeError: '<' not supported between NoneType and float``, breaking the
    whole history list + all trends + the analyzer's history pull. The corrupt record stays listed,
    sorted as oldest (coerced to 0.0)."""
    hdir = tmp_path / "history"
    hdir.mkdir()
    (hdir / "aaaaaaaa.json").write_text(json.dumps(
        {"summary": {"latency": {"ttft": {"units": "ms", "mean": 10.0}}},
         "stored_at": 100.0, "label": "good", "tags": [], "model": "m"}))
    (hdir / "bbbbbbbb.json").write_text(json.dumps(
        {"summary": {"latency": {"ttft": {"units": "ms", "mean": 20.0}}},
         "stored_at": None, "label": "corrupt", "tags": [], "model": "m"}))
    store = HistoryStore(tmp_path)
    recs = store.list()  # must not raise
    assert {r.label for r in recs} == {"good", "corrupt"}
    assert recs[0].label == "good"  # valid 100.0 newest-first; corrupt (-> 0.0) sorts last
    # trend() sorts on the same key; it must complete (not raise) with the corrupt record present.
    assert isinstance(trend(recs, "ttft"), dict)


async def test_tool_list_and_trend_date_filter_survive_corrupt_stored_at(tool_ctx, tmp_path):
    """The result_history DATE filter (`_filter_by_date`) compares each record's stored_at against
    the resolved bound. A corrupt record whose on-disk ``stored_at`` is non-numeric (null/string —
    same BUG-020 class that bypasses the validated add() path) would make ``r.stored_at >= lo``
    raise ``TypeError: '>=' not supported between NoneType and float`` — breaking list/trend for
    EVERY record the moment ANY start_date/end_date is supplied (BUG-020's fix only hardened the
    store's SORT key, not this tool-layer filter). The corrupt record must be treated as oldest
    (coerced 0.0), not crash. tool_ctx's history store is rooted at tmp_path (ws.parent)."""
    hdir = tmp_path / "history"
    hdir.mkdir()
    (hdir / "aaaaaaaa.json").write_text(json.dumps(
        {"summary": {"model": "m", "run_uid": "good",
                     "latency": {"ttft": {"units": "ms", "mean": 10.0}}},
         "stored_at": time.time(), "label": "good", "tags": [], "model": "m"}))
    (hdir / "bbbbbbbb.json").write_text(json.dumps(
        {"summary": {"model": "m", "run_uid": "corrupt",
                     "latency": {"ttft": {"units": "ms", "mean": 20.0}}},
         "stored_at": None, "label": "corrupt", "tags": [], "model": "m"}))

    # list WITH a date bound must not raise; both records are still returned (corrupt -> 0.0, so a
    # start_date drops it as "before the window" — but the call SUCCEEDS rather than crashing).
    listed = await history_tool.result_history(tool_ctx, action="list", start_date="1970-01-02")
    assert isinstance(listed.get("n"), int)
    # An open-ended (end-only) bound keeps both records and must also not raise.
    both = await history_tool.result_history(tool_ctx, action="list", end_date="2999-01-01")
    assert both["n"] == 2

    # trend WITH a date bound takes the same filter path — it too must complete, not raise.
    trended = await history_tool.result_history(
        tool_ctx, action="trend", metric="ttft", end_date="2999-01-01")
    assert "error" not in trended and isinstance(trended.get("points"), list)
