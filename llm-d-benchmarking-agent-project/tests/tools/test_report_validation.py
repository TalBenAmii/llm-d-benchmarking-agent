"""Benchmark Report v0.2 validation + summary, against the repo's real schema/example."""
from __future__ import annotations

import pytest

from app.validation.report import ReportError, load_report, summarize_report, validate_report


@pytest.fixture(scope="module")
def example(br_schema, br_example):
    if not br_schema.exists() or not br_example.exists():
        pytest.skip("llm-d-benchmark repo schema/example not present")
    return load_report(br_example)


def test_example_validates_against_schema(example, br_schema):
    v = validate_report(example, br_schema)
    assert v.valid, v.errors
    assert v.schema_version == "0.2"


def test_summary_extracts_key_metrics(example):
    s = summarize_report(example)
    assert s["model"] == "Qwen/Qwen3-0.6B"
    assert s["requests_total"] == 500
    assert s["requests_failures"] == 0
    assert s["success_rate_pct"] == 100.0
    # TTFT present with units (seconds in the example)
    assert s["latency"]["ttft"]["units"] == "s"
    assert "mean" in s["latency"]["ttft"]
    # throughput present
    assert "total_token_rate" in s["throughput"]


def test_invalid_report_is_rejected(br_schema):
    if not br_schema.exists():
        pytest.skip("schema not present")
    broken = {"version": "0.2", "run": {}}  # missing required 'results'
    v = validate_report(broken, br_schema)
    assert not v.valid
    assert v.errors


def test_summary_is_defensive_on_sparse_report():
    # Should not raise even with almost everything missing.
    s = summarize_report({"run": {}, "results": {}})
    assert s["requests_total"] is None
    assert s["latency"] == {} and s["throughput"] == {}


def test_summary_is_defensive_on_malformed_nondict_children():
    # Regression: compare_reports / compare_harness_runs call summarize_report BEFORE the validity
    # check, so a parseable-but-malformed report whose children are present-but-non-dict must NOT
    # crash with AttributeError — every nesting level coerces a non-dict to {}.
    bad = {
        "run": "2026-06-20",                              # scalar instead of a mapping
        "scenario": {"stack": "not-a-list", "load": "x"},
        "results": {"request_performance": "x"},
    }
    s = summarize_report(bad)                             # must not raise
    assert s["model"] is None and s["harness"] is None
    assert s["duration"] is None and s["requests_total"] is None
    # Truthy-non-dict at deeper levels (stack element, run.time, standardized) is tolerated too.
    bad2 = {"run": {"time": "2026"}, "scenario": {"stack": ["pod-a", {"standardized": "x"}]}}
    assert summarize_report(bad2)["duration"] is None


def test_load_report_raises_reporterror_on_corrupt(tmp_path):
    """BUG-027: a present-but-corrupt report (e.g. truncated by an OOM-killed run) must surface as a
    typed ReportError, not a raw json/yaml/OS exception that escapes the calling tool as an opaque
    'tool ... raised: ...' string. Covers .json (JSONDecodeError) and .yaml (YAMLError)."""
    bad_json = tmp_path / "benchmark_report_v0.2.json"
    bad_json.write_text('{"truncated": ')                   # invalid JSON
    with pytest.raises(ReportError):
        load_report(bad_json)
    bad_yaml = tmp_path / "benchmark_report_v0.2.yaml"
    bad_yaml.write_text("key: : : not valid\n  - broken")   # invalid YAML
    with pytest.raises(ReportError):
        load_report(bad_yaml)
    # A missing file (OSError) is also a clean ReportError, never a bare OSError.
    with pytest.raises(ReportError):
        load_report(tmp_path / "does_not_exist.json")


def test_find_report_honours_root_precedence_over_global_mtime(tmp_path):
    """An EXPLICIT results_dir must win over a newer, unrelated report elsewhere in the
    session workspace.

    locate_and_parse_report searches [results_dir, session_id-dir, ctx.workspace] in that
    most-specific-first order. The old _find_report merged every root into ONE global pool and
    returned the single newest-by-mtime — so when the caller pointed at run A via results_dir
    but the SAME workspace also held a LATER run B, it silently returned run B's report,
    surfacing another run's metrics under the dir the caller explicitly chose. _find_report must
    instead return the newest report from the FIRST root that contains any report; later, broader
    roots are only a fallback."""
    import os

    from app.tools.analyze.report_locate import _find_report

    workspace = tmp_path / "ws" / "sessions" / "sess1"
    run_a = workspace / "results" / "runA"      # what the caller explicitly asks for
    run_b = workspace / "results" / "runB"      # a later, unrelated run in the same workspace
    run_a.mkdir(parents=True)
    run_b.mkdir(parents=True)
    rep_a = run_a / "benchmark_report_v0.2.yaml"
    rep_b = run_b / "benchmark_report_v0.2.yaml"
    rep_a.write_text("run: {uid: A}\n")
    rep_b.write_text("run: {uid: B}\n")
    os.utime(rep_a, (1000, 1000))               # run A is OLDER
    os.utime(rep_b, (2000, 2000))               # run B is NEWER (the trap for global-max-mtime)

    # roots ordered as locate_and_parse_report builds them: explicit results_dir first, then ws.
    chosen = _find_report([run_a, workspace])
    assert chosen == rep_a, (
        f"explicit results_dir must win; got {chosen} (the newer, unrelated run B) instead of "
        f"the run A report the caller pointed at"
    )

    # Fallback still works: with NO report under the first root, fall through to the workspace.
    empty_first = tmp_path / "empty"
    empty_first.mkdir()
    assert _find_report([empty_first, workspace]) == rep_b  # newest within the fallback root


def _report_ctx(workspace):
    """A minimal ToolContext rooted at ``workspace`` for report-locate handler tests."""
    from app.config import get_settings
    from app.tools.context import ToolContext
    s = get_settings()
    return ToolContext(settings=s, policy=None, runner=None, workspace=workspace)


@pytest.mark.parametrize("evil", ["../outside", "../../../../etc", "/etc/passwd-dir"])
def test_locate_rejects_session_id_path_traversal(tmp_path, evil):
    """SECURITY: session_id is an UNVALIDATED agent-supplied string used to build
    ``ctx.workspace.parent / session_id``. A value containing ``..`` (or an absolute path)
    must NOT let the tool escape the sessions root and locate+read a benchmark_report
    elsewhere on disk. Fails before the fix (the out-of-root report is located and read);
    passes after (a ToolError is raised, so the loop relays a clean {"error": ...})."""
    from app.tools.analyze.report_locate import locate_and_parse_report
    from app.tools.context import ToolError

    sessions_root = tmp_path / "sessions"
    ws = sessions_root / "sess1"
    ws.mkdir(parents=True)
    # Plant a report OUTSIDE the sessions root that the traversal would otherwise reach.
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    (outside / "benchmark_report_v0.2.yaml").write_text("run: {uid: SHOULD-NOT-BE-READ}\n")

    ctx = _report_ctx(ws)
    with pytest.raises(ToolError):
        locate_and_parse_report(ctx, session_id=evil)


def test_locate_accepts_legitimate_session_id(tmp_path):
    """The fix must keep the normal path working: a plain session_id naming a sibling session
    inside the sessions root still locates that session's report (no false rejection)."""
    from app.tools.analyze.report_locate import _session_root

    sessions_root = tmp_path / "sessions"
    ws = sessions_root / "sess1"
    ws.mkdir(parents=True)
    sibling = sessions_root / "sess2"
    sibling.mkdir()
    rep = sibling / "benchmark_report_v0.2.yaml"
    rep.write_text("run: {uid: LEGIT}\n")

    ctx = _report_ctx(ws)
    root = _session_root(ctx, "sess2")
    assert root == sibling.resolve()
    assert root.is_relative_to(sessions_root.resolve())
