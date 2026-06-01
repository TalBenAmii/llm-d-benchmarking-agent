"""Tool registry / schema validation and SessionPlan checks."""
from __future__ import annotations

import pytest

from app.tools.registry import REGISTRY, dispatch, tool_definitions
from app.validation.session_plan import SessionPlan, validate_plan


def test_tool_definitions_complete():
    defs = tool_definitions()
    names = {d["name"] for d in defs}
    expected = {
        "probe_environment", "list_catalog", "read_repo_doc", "fetch_key_docs",
        "propose_session_plan", "check_capacity", "ensure_repos", "run_setup",
        "write_and_validate_config", "execute_llmdbenchmark", "run_command",
        "locate_and_parse_report", "compare_reports", "analyze_results",
        "orchestrate_benchmark_run", "observe_run_metrics",
    }
    assert names == expected
    for d in defs:
        assert d["description"] and d["input_schema"]["type"] == "object"


async def test_dispatch_rejects_bad_args(tool_ctx):
    # read_repo_doc requires 'path'
    result = await dispatch(tool_ctx, "read_repo_doc", {})
    assert "error" in result and result["error"] == "invalid arguments"


async def test_dispatch_unknown_tool(tool_ctx):
    result = await dispatch(tool_ctx, "rm_rf", {})
    assert "error" in result and "valid_tools" in result


async def test_dispatch_list_catalog_runs(tool_ctx):
    if not tool_ctx.settings.bench_repo.is_dir():
        pytest.skip("repo not present")
    result = await dispatch(tool_ctx, "list_catalog", {"kinds": ["harnesses"]})
    assert "harnesses" in result


def test_session_plan_validation_catches_bad_enums(catalog):
    plan = SessionPlan(
        use_case_summary="chat", spec="guides/nope", namespace="ns",
        harness="made-up", workload="sanity_random.yaml",
    )
    errors = validate_plan(plan, catalog)
    assert any("spec" in e for e in errors)
    assert any("harness" in e for e in errors)


def test_session_plan_valid(catalog):
    plan = SessionPlan(
        use_case_summary="chat", spec="cicd/kind", namespace="llmd-quickstart",
        harness="inference-perf", workload="sanity_random.yaml",
    )
    assert validate_plan(plan, catalog) == []


def test_session_plan_bad_namespace(catalog):
    plan = SessionPlan(
        use_case_summary="chat", spec="cicd/kind", namespace="Bad_NS",
        harness="inference-perf", workload="sanity_random.yaml",
    )
    assert any("namespace" in e for e in validate_plan(plan, catalog))


def test_registry_handlers_callable():
    for spec in REGISTRY.values():
        assert callable(spec.handler)
