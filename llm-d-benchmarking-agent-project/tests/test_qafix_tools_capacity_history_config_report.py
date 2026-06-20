"""QA-fix regression tests (real-1/real-2/sim-1/sim-2 findings).

Covers five fixes, all in our wrapper layer (the upstream planner stays read-only):

  #1 check_capacity no longer crashes with AttributeError on minimal overrides — a
     scenario key with a bare `None` value (examples/gpu's `decode:`) must NOT clobber the
     default block (our _deep_merge now mirrors upstream's None-skip contract).
  #2 a 0-replica / un-sized run is INCONCLUSIVE (feasible=None), not feasible:true; and a
     `model` override also syncs model.huggingfaceId so the planner sizes + gates the
     OVERRIDE model, not the spec default.
  #3 locate_and_parse_report surfaces a generated_at timestamp (report time or file mtime).
  #4 result_history supports start_date/end_date filtering (on stored_at) and advertises
     supported_filters.
  #5 write_and_validate_config flags fabricated vLLM flag names in an advisory
     unrecognized_flags list (non-fatal).
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.capacity.planner import (
    _deep_merge,
    apply_overrides,
    classify_diagnostics,
    plan_config_for_spec,
)

# ---- #1 + #2 : merge None-skip, no crash, model/huggingfaceId sync -----------

def test_deep_merge_skips_none_override_keeping_default_dict():
    # A YAML key present-but-null (scenario `decode:` with all sub-keys commented) must NOT
    # wipe the default block — that None-deref is the real-1 #1 crash root cause.
    base = {"decode": {"enabled": True, "replicas": 1}, "model": {"name": "x"}}
    over = {"decode": None, "model": {"name": "y"}}
    out = _deep_merge(base, over)
    assert out["decode"] == {"enabled": True, "replicas": 1}  # default kept, not None
    assert out["model"]["name"] == "y"  # a real override still wins


def test_plan_config_gpu_spec_minimal_overrides_no_none_section(bench_repo):
    # real-1 #1: examples/gpu + {model, gpu_memory_gb} used to leave decode=None and crash the
    # planner. The merged decode block must now be a populated dict with the default replicas.
    pc, applied = plan_config_for_spec(
        bench_repo, "examples/gpu",
        overrides={"model": "meta-llama/Llama-3.1-70B-Instruct", "gpu_memory_gb": 80},
    )
    assert isinstance(pc.get("decode"), dict)
    assert pc["decode"].get("replicas") == 1  # default preserved, not wiped to None/0


def test_model_override_syncs_huggingface_id(bench_repo):
    # real-2 #2: the planner sizes/gates `model.huggingfaceId or model.name`; overriding only
    # model.name left huggingfaceId at the spec default (facebook/opt-125m). A `model` override
    # must drive BOTH so the EVALUATED model is the one the user asked for.
    pc, applied = plan_config_for_spec(
        bench_repo, "examples/gpu",
        overrides={"model": "meta-llama/Llama-3.1-405B-Instruct", "gpu_memory_gb": 10},
    )
    assert pc["model"]["name"] == "meta-llama/Llama-3.1-405B-Instruct"
    assert pc["model"]["huggingfaceId"] == "meta-llama/Llama-3.1-405B-Instruct"
    assert any("huggingfaceId" in a for a in applied)


def test_explicit_huggingface_id_override_not_clobbered_by_model():
    # If the caller passes both, the explicit huggingface_id wins (no silent overwrite).
    pc = {"model": {"name": "d", "huggingfaceId": "d"}}
    apply_overrides(pc, {"model": "org/Foo", "huggingface_id": "org/Foo-HF"})
    assert pc["model"]["name"] == "org/Foo"
    assert pc["model"]["huggingfaceId"] == "org/Foo-HF"


def test_classify_zero_replica_skip_is_inconclusive_not_feasible():
    # real-2 #2: every method skipped for 0 replicas => VRAM sizing bypassed => feasibility was
    # NOT evaluated. Must be feasible=None (inconclusive), never feasible:true.
    diags = [
        "Deployment method is modelservice -- checking decode and prefill configurations",
        "decode is disabled or has 0 replicas -- skipping",
        "prefill is disabled or has 0 replicas -- skipping",
    ]
    v = classify_diagnostics(diags)
    assert v.feasible is None
    assert v.sizing_evaluated is False
    assert "0 decode/prefill replicas" in v.inconclusive_reason


def test_classify_gpu_memory_check_skipped_is_inconclusive():
    # Sizing bypassed because model architecture / GPU memory was unknown — the count line is
    # NOT a fit verdict, so feasibility is inconclusive.
    diags = [
        "[decode] 10 GB per GPU, 10 x 0.95 = 9.5 GB available",
        "[decode] Each replica requires 1 GPUs, total available GPU memory = 9.5 GB",
        "[decode] WARNING: Model architecture info not available -- skipping memory checks.",
    ]
    v = classify_diagnostics(diags)
    assert v.feasible is None
    assert v.sizing_evaluated is False


def test_classify_real_sizing_stays_feasible():
    # A run where VRAM sizing actually completed (no skip line) stays feasible:true.
    diags = [
        "[decode] 80 GB per GPU, 80 x 0.9 = 72.0 GB available GPU memory",
        "[decode] mistral requires 13.49 GB; KV cache fits",
    ]
    v = classify_diagnostics(diags)
    assert v.feasible is True
    assert v.sizing_evaluated is True


def test_classify_empty_and_info_only_stay_feasible():
    # Absence of sizing facts is NOT evidence of a skip — must not regress to inconclusive.
    assert classify_diagnostics([]).feasible is True
    info_only = classify_diagnostics(["[decode] Max concurrent requests: 128"])
    assert info_only.feasible is True


def test_classify_hard_error_wins_over_skip():
    # An ERROR / will-fail is authoritative even when a skip line is also present.
    diags = [
        "decode is disabled or has 0 replicas -- skipping",
        "[decode] ERROR: DEPLOYMENT WILL FAIL: cannot serve",
    ]
    v = classify_diagnostics(diags)
    assert v.feasible is False
    assert v.will_fail is True


def test_classify_sizing_exception_is_inconclusive_not_feasible():
    # BUG-030: the "...available GPU memory" line is the GPU-COUNT summary, NOT proof sizing ran.
    # When a method's KV-cache sizing THROWS (only a WARNING under ignoreFailedValidation) while
    # the other method is 0-replica-skipped, feasibility was NOT evaluated -> must be inconclusive,
    # never feasible:true (else the agent tells the user a deployment "fits" before a doomed standup).
    unsized = [
        "[decode] Each replica requires 1 GPUs, total available GPU memory = 72.0 GB",
        "[decode] WARNING: Cannot estimate model memory or KV cache for meta/Llama-3.1-405B: HTTP 401",
        "prefill is disabled or has 0 replicas -- skipping",
    ]
    v = classify_diagnostics(unsized)
    assert v.feasible is None and v.sizing_evaluated is False
    # A genuine fit (the real KV-cache fit lines) with a 0-replica skip elsewhere stays feasible:true.
    fit = [
        "[decode] Allocatable KV cache memory: 30.00 GB",
        "[decode] Per-request KV cache (max_model_len=4096): 0.50 GB",
        "[decode] Max concurrent requests (worst case, each at max_model_len): 60",
        "prefill is disabled or has 0 replicas -- skipping",
    ]
    v2 = classify_diagnostics(fit)
    assert v2.feasible is True and v2.sizing_evaluated is True


# ---- #3 : locate_and_parse_report generated_at -------------------------------

def _write_min_report(path: Path, *, end: str | None) -> None:
    import yaml
    run: dict = {"uid": "run-123"}
    if end is not None:
        run["time"] = {"start": "2026-05-01T00:00:00Z", "end": end}
    path.write_text(yaml.safe_dump({"run": run, "scenario": {}, "results": {}}))


def test_report_generated_at_prefers_report_time(tool_ctx):
    from app.tools.report_locate import _report_generated_at
    from app.validation.report import load_report
    p = tool_ctx.workspace / "benchmark_report_v0.2.yaml"
    tool_ctx.workspace.mkdir(parents=True, exist_ok=True)
    _write_min_report(p, end="2026-06-10T12:00:00Z")
    when, source = _report_generated_at(load_report(p), p)
    assert when == "2026-06-10T12:00:00Z"
    assert "run.time.end" in source


def test_report_generated_at_falls_back_to_mtime(tool_ctx):
    from app.tools.report_locate import _report_generated_at
    from app.validation.report import load_report
    p = tool_ctx.workspace / "benchmark_report_v0.2.yaml"
    tool_ctx.workspace.mkdir(parents=True, exist_ok=True)
    _write_min_report(p, end=None)  # no run.time block
    when, source = _report_generated_at(load_report(p), p)
    assert when is not None  # an ISO mtime string
    assert "mtime" in source


def test_locate_report_result_carries_generated_at(tool_ctx):
    from app.tools.report_locate import locate_and_parse_report
    p = tool_ctx.workspace / "benchmark_report_v0.2.yaml"
    tool_ctx.workspace.mkdir(parents=True, exist_ok=True)
    _write_min_report(p, end="2026-06-10T12:00:00Z")
    res = locate_and_parse_report(tool_ctx)
    assert res["found"] is True
    assert "generated_at" in res
    assert res["generated_at"] == "2026-06-10T12:00:00Z"


# ---- #4 : result_history date filtering --------------------------------------

@pytest.mark.asyncio
async def test_result_history_list_advertises_supported_filters(tool_ctx):
    from app.tools.history import result_history
    res = await result_history(tool_ctx, action="list")
    assert "supported_filters" in res
    assert "start_date" in res["supported_filters"] and "end_date" in res["supported_filters"]


def test_filter_by_date_inclusive_bounds():
    from app.storage.history import HistoryRecord
    from app.tools.history import _filter_by_date

    def rec(epoch: float) -> HistoryRecord:
        return HistoryRecord(id=str(epoch), stored_at=epoch, label=None)

    # 2026-05-15, 2026-06-20 (UTC)
    import datetime as _dt
    may15 = _dt.datetime(2026, 5, 15, tzinfo=_dt.UTC).timestamp()
    jun20 = _dt.datetime(2026, 6, 20, tzinfo=_dt.UTC).timestamp()
    recs = [rec(may15), rec(jun20)]
    out, applied = _filter_by_date(recs, "2026-05-01", "2026-06-15")
    assert [r.stored_at for r in out] == [may15]  # jun20 excluded
    assert applied.get("start_date") == "2026-05-01"


def test_filter_by_date_end_is_inclusive_of_whole_day():
    import datetime as _dt

    from app.storage.history import HistoryRecord
    from app.tools.history import _filter_by_date
    # A record stored late on the end-date day must be INCLUDED (bare end date => 23:59:59).
    late = _dt.datetime(2026, 6, 15, 23, 30, tzinfo=_dt.UTC).timestamp()
    out, _ = _filter_by_date([HistoryRecord(id="x", stored_at=late, label=None)],
                             None, "2026-06-15")
    assert len(out) == 1


def test_filter_by_date_bad_input_reports_error_not_crash():
    from app.storage.history import HistoryRecord
    from app.tools.history import _filter_by_date
    recs = [HistoryRecord(id="x", stored_at=time.time(), label=None)]
    out, applied = _filter_by_date(recs, "not-a-date", None)
    assert "error" in applied
    assert out == recs  # unfiltered, but the error is machine-visible


# ---- #5 : unrecognized_flags advisory ----------------------------------------

def test_unrecognized_flags_catches_fabricated_vllm_flags(bench_repo):
    from app.tools.config_artifact import _scenario_reference, unrecognized_flags
    ref = _scenario_reference(bench_repo)
    content = {
        "name": "fab",
        "vllmCommon.flags.enablePrefixCachingV2": True,   # fabricated (sim-1)
        "vllmCommon.kvCacheSharingStrategy": "cross-session",
        "speculativeDecodeTokenBudget": 512,
        "vllmCommon.flags.enableChunkedPrefillV3": True,
    }
    flagged = unrecognized_flags(content, ref)
    assert "vllmCommon.flags.enablePrefixCachingV2" in flagged
    assert "speculativeDecodeTokenBudget" in flagged
    assert len(flagged) == 4


def test_unrecognized_flags_passes_real_flags(bench_repo):
    from app.tools.config_artifact import _scenario_reference, unrecognized_flags
    ref = _scenario_reference(bench_repo)
    content = {
        "name": "real",
        "vllmCommon.flags.enforceEager": True,
        "vllmCommon.flags.noPrefixCaching": True,
        "schedulerName": "custom",
        "routing.servicePort": 8080,
    }
    assert unrecognized_flags(content, ref) == []


def test_unrecognized_flags_no_reference_returns_empty():
    # No repo truth => we don't guess (advisory stays silent rather than false-flagging).
    from app.tools.config_artifact import unrecognized_flags
    assert unrecognized_flags({"foo.bar": 1}, {}) == []


@pytest.mark.asyncio
async def test_write_config_surfaces_unrecognized_flags_non_fatally(tool_ctx):
    from app.tools.config_artifact import write_and_validate_config
    res = await write_and_validate_config(
        tool_ctx,
        artifact_type="scenario",
        target_filename="fab.yaml",
        content={"name": "fab", "vllmCommon.flags.enablePrefixCachingV2": True,
                 "vllmCommon.flags.enforceEager": True},
    )
    # Non-fatal: the file is still authored (shape passes) but the fabricated flag is surfaced.
    assert res["valid"] is True
    assert "vllmCommon.flags.enablePrefixCachingV2" in res["unrecognized_flags"]
    assert "vllmCommon.flags.enforceEager" not in res["unrecognized_flags"]
    assert "unrecognized_flags_note" in res
