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
    rep = copy.deepcopy(base)
    rep["run"]["uid"] = uid
    agg = rep["results"]["request_performance"]["aggregate"]
    agg["latency"]["time_to_first_token"]["mean"] = ttft_s
    agg["latency"]["time_to_first_token"]["p99"] = ttft_s
    agg["throughput"]["output_token_rate"]["mean"] = out_rate
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / "benchmark_report_v0.2.yaml").write_text(yaml.safe_dump(rep, sort_keys=False))


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
