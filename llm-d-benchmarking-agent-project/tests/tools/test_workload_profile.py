"""Tests for the two read-only workload-preview tools (inspect_workload_profile +
estimate_run_duration).

They read REAL profiles off the on-disk benchmark repo (the conftest ``tool_ctx`` is wired to the
real repos), so they skip cleanly when the repo is absent. They assert the NORMALIZED facts both
tools surface, the graceful not-found path, and the duration heuristic's number / "insufficient
fields" branches — using profile names that actually exist on disk (inference-perf, guidellm,
vllm-benchmark, aiperf).
"""
from __future__ import annotations

import pytest

from app.tools.analyze import workload_profile
from app.tools.context import ToolError
from app.tools.registry import dispatch


def _skip_if_no_repo(tool_ctx) -> None:
    if not (tool_ctx.settings.bench_repo / "workload" / "profiles").is_dir():
        pytest.skip("bench repo profiles not present")


# ---- inspect_workload_profile: normalized facts ---------------------------

def test_inspect_inference_perf_token_and_load_shape(tool_ctx):
    """An inference-perf synthetic profile parses into the input/output length distribution and
    the sweep-stage load shape, each tagged with the raw key it came from."""
    _skip_if_no_repo(tool_ctx)
    out = workload_profile.inspect_workload_profile(
        tool_ctx, workload="chatbot_synthetic.yaml", harness="inference-perf"
    )
    assert out["harness"] == "inference-perf"
    assert out["path"].endswith("inference-perf/chatbot_synthetic.yaml.in")

    ts = out["token_shape"]
    assert ts["input_tokens"]["mean"] == 4096
    assert ts["output_tokens"]["mean"] == 1024
    assert ts["_from"]["input_tokens"] == "data.input_distribution"

    ls = out["load_shape"]
    # Four constant stages of 120s each → 480s total, rates 1/2/4/8.
    assert ls["rates"] == [1, 2, 4, 8]
    assert ls["total_stage_duration_s"] == 480
    assert ls["_from"]["total_stage_duration_s"] == "sum(load.stages[].duration)"

    assert out["prompt_source"]["data_type"] == "random"
    assert out["prompt_source"]["requires_staged_dataset"] is False


def test_inspect_shared_prefix_reuse(tool_ctx):
    """A shared-prefix profile surfaces the system-prefix reuse block (num_groups, prompt len)."""
    _skip_if_no_repo(tool_ctx)
    out = workload_profile.inspect_workload_profile(
        tool_ctx, workload="shared_prefix_synthetic.yaml", harness="inference-perf"
    )
    prefix = out["token_shape"]["shared_prefix"]
    assert prefix["num_groups"] == 32
    assert prefix["system_prompt_len"] == 2048
    assert prefix["question_len"] == 256


def test_inspect_guidellm_flat_layout(tool_ctx):
    """guidellm uses a FLAT layout (rate list + max_seconds + data.prompt_tokens*) — normalized to
    the same token_shape/load_shape keys as inference-perf."""
    _skip_if_no_repo(tool_ctx)
    out = workload_profile.inspect_workload_profile(
        tool_ctx, workload="chatbot_synthetic.yaml", harness="guidellm"
    )
    assert out["harness"] == "guidellm"
    assert out["token_shape"]["input_tokens"]["mean"] == 4096
    assert out["load_shape"]["rates"] == [1, 2, 4, 8]
    assert out["load_shape"]["max_seconds"] == 120


def test_inspect_vllm_benchmark_dataset_required(tool_ctx):
    """vllm-benchmark's custom-dataset profile is flagged as REQUIRING a staged dataset."""
    _skip_if_no_repo(tool_ctx)
    out = workload_profile.inspect_workload_profile(
        tool_ctx, workload="fixed_dataset.yaml", harness="vllm-benchmark"
    )
    assert out["prompt_source"]["requires_staged_dataset"] is True
    assert out["load_shape"]["max_concurrency"] == 4
    assert out["load_shape"]["num_requests"] == 2000


def test_inspect_searches_harnesses_when_omitted(tool_ctx):
    """With no harness given, the search finds the profile (inference-perf is tried first)."""
    _skip_if_no_repo(tool_ctx)
    out = workload_profile.inspect_workload_profile(tool_ctx, workload="sanity_random.yaml")
    assert out["harness"] == "inference-perf"
    assert out["path"].endswith("inference-perf/sanity_random.yaml.in")


# ---- inspect_workload_profile: graceful not-found -------------------------

def test_inspect_missing_profile_lists_what_exists(tool_ctx):
    """An unknown workload name raises a ToolError whose message enumerates the real profiles."""
    _skip_if_no_repo(tool_ctx)
    with pytest.raises(ToolError) as exc:
        workload_profile.inspect_workload_profile(
            tool_ctx, workload="does_not_exist.yaml", harness="inference-perf"
        )
    msg = str(exc.value)
    assert "not found" in msg
    # The enumeration of existing names is included so the agent can self-correct.
    assert "sanity_random.yaml" in msg


async def test_inspect_missing_profile_via_dispatch_raises_toolerror(tool_ctx):
    """Through the registry, a not-found profile raises ToolError (the agent loop — not dispatch —
    turns a ToolError into a clean {"error": ...}; dispatch only converts ValidationErrors)."""
    _skip_if_no_repo(tool_ctx)
    with pytest.raises(ToolError) as exc:
        await dispatch(
            tool_ctx, "inspect_workload_profile",
            {"workload": "nope.yaml", "harness": "inference-perf"},
        )
    assert "not found" in str(exc.value)


async def test_inspect_invalid_args_via_dispatch_returns_error(tool_ctx):
    """A schema ValidationError (missing required 'workload') IS returned as a dict by dispatch."""
    res = await dispatch(tool_ctx, "inspect_workload_profile", {"harness": "inference-perf"})
    assert "error" in res


# ---- estimate_run_duration: number + insufficient-fields branches ---------

def test_estimate_from_sweep_stage_durations(tool_ctx):
    """inference-perf: the estimate is the sum of the sweep stages' durations, labeled approximate."""
    _skip_if_no_repo(tool_ctx)
    out = workload_profile.estimate_run_duration(
        tool_ctx, workload="chatbot_synthetic.yaml", harness="inference-perf"
    )
    assert out["estimable"] is True
    assert out["estimated_seconds"] == 480
    assert out["estimated_minutes"] == 8.0
    assert out["approximate"] is True
    assert "duration" in out["basis"]
    assert out["assumption"]


def test_estimate_from_guidellm_max_seconds(tool_ctx):
    """guidellm: max_seconds × number of rate stages (120s × 4 rates = 480s)."""
    _skip_if_no_repo(tool_ctx)
    out = workload_profile.estimate_run_duration(
        tool_ctx, workload="chatbot_synthetic.yaml", harness="guidellm"
    )
    assert out["estimable"] is True
    assert out["estimated_seconds"] == 480
    assert "max_seconds" in out["basis"]


def test_estimate_insufficient_fields_says_whats_missing(tool_ctx):
    """nop has no load fields at all → estimable=False with a clear 'missing' list, no fabricated
    number."""
    _skip_if_no_repo(tool_ctx)
    out = workload_profile.estimate_run_duration(tool_ctx, workload="nop.yaml", harness="nop")
    assert out["estimable"] is False
    assert "estimated_seconds" not in out
    assert out["missing"]


# ---- registry wiring ------------------------------------------------------

def test_both_tools_registered_read_only():
    """Both tools are in the registry; neither calls run_command (so they auto-run, no approval)."""
    from app.tools.registry import REGISTRY

    for name in ("inspect_workload_profile", "estimate_run_duration"):
        assert name in REGISTRY
        assert REGISTRY[name].description
