"""Tool registry / schema validation and SessionPlan checks."""
from __future__ import annotations

import pytest

from app.tools.registry import REGISTRY, dispatch, tool_definitions
from app.validation.session_plan import SessionPlan, validate_plan


def test_tool_definitions_complete():
    defs = tool_definitions()
    names = {d["name"] for d in defs}
    expected = {
        "probe_environment", "list_catalog", "advise_accelerators",
        "read_knowledge", "read_repo_doc", "fetch_key_docs",
        "propose_session_plan", "check_capacity", "aggregate_runs", "provision_hf_secret",
        "check_endpoint_readiness",
        "ensure_repos", "run_setup",
        "write_and_validate_config", "convert_guide_to_scenario",
        "generate_doe_experiment", "execute_llmdbenchmark",
        "run_command", "locate_and_parse_report", "compare_reports", "compare_harness_runs",
        "analyze_results", "orchestrate_benchmark_run", "observe_run_metrics",
        "result_history", "cancel_run",
    }
    assert names == expected
    for d in defs:
        assert d["description"] and d["input_schema"]["type"] == "object"


def test_tool_definitions_have_no_title_keys():
    """Pydantic auto-adds 'title' keys; tool_definitions() strips them recursively to
    save tokens with zero behavioral change. Assert none survive anywhere in the schema
    (top-level, properties, $defs, anyOf/items, …)."""
    def find_titles(node) -> bool:
        if isinstance(node, dict):
            return "title" in node or any(find_titles(v) for v in node.values())
        if isinstance(node, list):
            return any(find_titles(v) for v in node)
        return False

    defs = tool_definitions()
    assert defs  # non-empty
    for d in defs:
        assert not find_titles(d["input_schema"]), f"{d['name']} still has a title key"
    # Structural keys the LLM DOES need must survive the strip.
    by_name = {d["name"]: d for d in defs}
    plan_schema = by_name["propose_session_plan"]["input_schema"]
    assert plan_schema["type"] == "object" and "properties" in plan_schema


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
