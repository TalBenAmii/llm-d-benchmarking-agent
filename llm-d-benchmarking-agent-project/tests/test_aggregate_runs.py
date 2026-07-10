"""Phase 51 — Jupyter / standalone plotting scripts surfacing.

Two halves, both hermetic (NO cluster, NO GPU, NO network, NO real benchmark run):

  (A) KNOWLEDGE SURFACING (the mandatory acceptance: the agent can EXPLAIN / POINT AT the
      interactive notebook + the exploratory plotting scripts). read_repo_doc reaches the
      upstream analysis README + pipeline doc; knowledge/analysis.md carries the artifact
      paths + the judgment that these are user-driven exploration the agent only points at;
      the registry descriptions route there.

  (B) THE OPTIONAL SCRIPTED STEP (run a standalone plot script against a results dir):
      the `aggregate_runs` tool + the vetted `scripts/bridges/aggregate_runs.py` wrapper, allowlisted
      READ-ONLY against a results dir. Covered three ways: the allowlist classification, the
      tool end-to-end through a CaptureRunner faking the bridge, and a REAL end-to-end run of
      the wrapper (importing the repo's OWN aggregate_runs module) over fixture BR v0.2
      reports — proving the import-and-call mechanism without reimplementing any math.
"""
from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from app.config import get_settings
from app.dig import parse_bridge_dict
from app.security.allowlist import READ_ONLY, Allowlist
from app.security.runner import CommandRunner, RunnerError
from app.tools.analyze.aggregate_runs import aggregate_runs
from app.tools.context import ToolError
from app.tools.registry import dispatch, tool_definitions
from app.validation.report import load_report
from tests._helpers import _real_repo_ctx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST_PATH = PROJECT_ROOT / "security" / "allowlist.yaml"
WRAPPER = PROJECT_ROOT / "scripts" / "bridges" / "aggregate_runs.py"


# ============================================================================
# (A) KNOWLEDGE SURFACING — the agent can EXPLAIN / POINT AT the notebook + scripts
# ============================================================================

def test_read_repo_doc_reaches_the_analysis_notebook_readme(tool_ctx):
    """The agent surfaces the notebook setup guide straight from the read-only repo."""
    from app.tools.access.knowledge_access import read_repo_doc

    doc = read_repo_doc(tool_ctx, path="llm-d-benchmark/docs/analysis/README.md")
    assert doc["path"].endswith("docs/analysis/README.md")
    assert "analysis.ipynb" in doc["content"]
    assert "Jupyter" in doc["content"]


def test_read_repo_doc_reaches_the_analysis_pipeline_overview(tool_ctx):
    from app.tools.access.knowledge_access import read_repo_doc

    doc = read_repo_doc(tool_ctx, path="llm-d-benchmark/docs/analysis.md")
    assert doc["path"].endswith("docs/analysis.md")
    # The overview names the three layers the agent points the user at.
    assert "--analyze" in doc["content"]


def test_knowledge_analysis_surfaces_notebook_and_scripts():
    """knowledge/analysis.md (the agent's brain) carries the real artifact paths + the
    judgment that these are pointer-only exploration, NOT part of the automated flow."""
    text = (PROJECT_ROOT / "knowledge" / "analysis/analysis.md").read_text()
    # The interactive notebook + its setup doc + the pipeline overview are named.
    assert "analysis.ipynb" in text
    assert "docs/analysis/README.md" in text
    assert "docs/analysis.md" in text
    # The one parameterizable script + the pointer-only templates are both named.
    assert "aggregate_runs.py" in text
    assert "to_be_incorporated" in text
    # The judgment is stated: NOT part of the automated probe->...->report flow.
    assert "NOT" in text and "automated flow" in text
    # The hardcoded-path caveat for the template scripts is recorded (don't run them).
    assert "../data/k8s/lmbenchmark" in text


def test_registry_read_repo_doc_points_at_the_notebook():
    """The read_repo_doc tool description routes the model to the notebook/pipeline docs."""
    defs = {d["name"]: d["description"] for d in tool_definitions()}
    assert "analysis.ipynb" in defs["read_repo_doc"]
    assert "docs/analysis" in defs["read_repo_doc"]


def test_the_template_plot_scripts_really_are_hardcoded():
    """Justify the pointer-only judgment with repo truth: the to_be_incorporated/ scripts
    hardcode a repo-relative data dir and write their PNG back INTO the repo dir — so they
    CANNOT be allowlisted read-only against a results dir (the agent must not run them)."""
    bench = get_settings().bench_repo
    script = bench / "docs" / "analysis" / "to_be_incorporated" / "plot_ttft_vs_qps.py"
    body = script.read_text()
    assert 'data", "k8s", "lmbenchmark"' in body  # hardcoded input dir, not a results-dir arg
    assert "dirname(__file__)" in body            # writes its PNG beside itself (into the repo)


# ============================================================================
# (B) THE OPTIONAL SCRIPTED STEP — allowlist classification
# ============================================================================

def test_allowlist_aggregate_runs_is_read_only(allowlist):
    d = allowlist.validate(["aggregate_runs.py", "workspace/sessions/s1/aggregate_request.json"])
    assert d.allowed and d.mode == READ_ONLY and not d.requires_approval


def test_allowlist_rejects_non_json_argument(allowlist):
    d = allowlist.validate(["aggregate_runs.py", "workspace/evil.sh"])
    assert not d.allowed


def test_allowlist_rejects_path_traversal(allowlist):
    d = allowlist.validate(["aggregate_runs.py", "../../etc/passwd.json"])
    assert not d.allowed


def test_allowlist_requires_the_positional(allowlist):
    d = allowlist.validate(["aggregate_runs.py"])
    assert not d.allowed  # missing required positional


def test_runner_resolves_wrapper_via_bench_venv(tmp_path):
    """`python_via` prepends the benchmark venv's python to the vetted wrapper script."""
    bench = tmp_path / "llm-d-benchmark"
    venv_bin = bench / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").write_text("")  # presence is enough for resolve()
    runner = CommandRunner({"llm-d-benchmark": bench, "llm-d": tmp_path / "llm-d"})

    entry = Allowlist.from_file(ALLOWLIST_PATH).executable("aggregate_runs.py")
    real, _cwd = runner.resolve(["aggregate_runs.py", str(tmp_path / "req.json")], entry)
    assert real[0] == str(venv_bin / "python")
    assert real[1].endswith("scripts/bridges/aggregate_runs.py")
    assert real[2].endswith("req.json")


def test_runner_wrapper_missing_venv_errors_clearly(tmp_path):
    bench = tmp_path / "llm-d-benchmark"
    bench.mkdir()
    runner = CommandRunner({"llm-d-benchmark": bench, "llm-d": tmp_path / "llm-d"})
    entry = Allowlist.from_file(ALLOWLIST_PATH).executable("aggregate_runs.py")
    with pytest.raises(RunnerError):
        runner.resolve(["aggregate_runs.py", str(tmp_path / "req.json")], entry)


# ============================================================================
# (B) THE TOOL end-to-end (real plan resolution + faked bridge via CaptureRunner)
# ============================================================================

_OK_BRIDGE = json.dumps({
    "ok": True,
    "output_dir": "/tmp/ws/aggregated",
    "summary_path": "/tmp/ws/aggregated/aggregated_summary.txt",
    "summary_json_path": "/tmp/ws/aggregated/aggregated_summary.json",
    "run_count": 2,
    "metrics": {"latency.time_to_first_token.mean": {"mean": 0.05, "std": 0.01, "min": 0.04, "max": 0.06, "count": 2}},
})


async def test_tool_writes_request_and_autoruns(tmp_path):
    ctx, runner, emitted = _real_repo_ctx(tmp_path, canned={"aggregate_runs.py": _OK_BRIDGE})
    results_dir = tmp_path / "results"
    results_dir.mkdir()

    res = await aggregate_runs(
        ctx,
        results_prefix=str(results_dir),
        harness="inference-perf",
        stack="llm-d-7b-base",
        run_ids=["r1", "r2"],
    )

    assert res["ran"] is True
    assert res["run_count"] == 2
    assert "latency.time_to_first_token.mean" in res["metrics"]

    # The bridge ran exactly once, against a request file in the session workspace.
    calls = [c for c in runner.calls if c["argv"] and c["argv"][0] == "aggregate_runs.py"]
    assert len(calls) == 1
    req_path = Path(calls[0]["argv"][1])
    assert req_path.parent == ctx.workspace and req_path.suffix == ".json"

    # The request reflects the args, points at the REAL repo analysis dir, and confines the
    # output strictly inside the session workspace.
    request = json.loads(req_path.read_text())
    assert request["results_prefix"] == str(results_dir.resolve())
    assert request["harness"] == "inference-perf" and request["stack"] == "llm-d-7b-base"
    assert request["run_ids"] == ["r1", "r2"]
    assert request["analysis_dir"].endswith("docs/analysis")
    assert Path(request["output"]).resolve().is_relative_to(ctx.workspace.resolve())

    # Read-only => auto-ran (no approval), announced as read-only.
    cmd_events = [p for t, p in emitted if t == "command"]
    assert cmd_events and cmd_events[0]["auto_run"] is True
    assert cmd_events[0]["mode"] == READ_ONLY


async def test_tool_custom_output_name_stays_in_workspace(tmp_path):
    ctx, _runner, _ = _real_repo_ctx(tmp_path, canned={"aggregate_runs.py": _OK_BRIDGE})
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    await aggregate_runs(
        ctx, results_prefix=str(results_dir), harness="inference-perf",
        stack="s", run_ids=["r1", "r2"], output_name="my_agg",
    )
    request = json.loads((ctx.workspace / "aggregate_request.json").read_text())
    assert Path(request["output"]).name == "my_agg"
    assert Path(request["output"]).resolve().is_relative_to(ctx.workspace.resolve())


async def test_tool_rejects_output_name_escape(tmp_path):
    ctx, runner, _ = _real_repo_ctx(tmp_path, canned={"aggregate_runs.py": _OK_BRIDGE})
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    with pytest.raises(ToolError):
        await aggregate_runs(
            ctx, results_prefix=str(results_dir), harness="h", stack="s",
            run_ids=["r1", "r2"], output_name="../escape",
        )
    # Nothing ran (refused before dispatching the bridge).
    assert [c for c in runner.calls if c["argv"][0] == "aggregate_runs.py"] == []


async def test_tool_requires_at_least_two_runs(tmp_path):
    ctx, runner, _ = _real_repo_ctx(tmp_path, canned={"aggregate_runs.py": _OK_BRIDGE})
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    with pytest.raises(ToolError):
        await aggregate_runs(
            ctx, results_prefix=str(results_dir), harness="h", stack="s", run_ids=["only-one"],
        )
    assert [c for c in runner.calls if c["argv"][0] == "aggregate_runs.py"] == []


async def test_tool_missing_results_dir_raises(tmp_path):
    ctx, runner, _ = _real_repo_ctx(tmp_path, canned={"aggregate_runs.py": _OK_BRIDGE})
    with pytest.raises(ToolError):
        await aggregate_runs(
            ctx, results_prefix=str(tmp_path / "nope"), harness="h", stack="s",
            run_ids=["r1", "r2"],
        )
    assert [c for c in runner.calls if c["argv"][0] == "aggregate_runs.py"] == []


async def test_tool_handles_bridge_not_ok(tmp_path):
    not_ok = json.dumps({"ok": False, "error": "fewer than 2 runs with reports"})
    ctx, _runner, _ = _real_repo_ctx(tmp_path, canned={"aggregate_runs.py": not_ok})
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    res = await aggregate_runs(
        ctx, results_prefix=str(results_dir), harness="h", stack="s", run_ids=["r1", "r2"],
    )
    assert res["ran"] is False
    assert "fewer than 2" in res["error"]
    # A non-summary must not masquerade as a successful aggregation.
    assert "metrics" not in res or res.get("run_count") is None


async def test_tool_via_dispatch_validates_args(tmp_path):
    ctx, _runner, _ = _real_repo_ctx(tmp_path, canned={"aggregate_runs.py": _OK_BRIDGE})
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    res = await dispatch(ctx, "aggregate_runs", {
        "results_prefix": str(results_dir), "harness": "inference-perf",
        "stack": "s", "run_ids": ["r1", "r2"],
    })
    assert res["ran"] is True
    # Bad args returned (not raised) so the agent can self-correct.
    bad = await dispatch(ctx, "aggregate_runs", {"results_prefix": str(results_dir)})
    assert "error" in bad


def test_aggregate_runs_is_registered_as_a_tool():
    assert "aggregate_runs" in {d["name"] for d in tool_definitions()}


def test_parse_bridge_empty_is_not_ok():
    assert parse_bridge_dict("", "aggregation")["ok"] is False
    assert parse_bridge_dict("not json at all", "aggregation")["ok"] is False


# ============================================================================
# (B) REAL end-to-end: the wrapper imports the repo's OWN aggregate_runs module
#     and aggregates fixture BR v0.2 reports — no reimplementation, no network.
# ============================================================================

def _write_run_report(results_dir: Path, run_id: str, harness: str, stack: str,
                      base: dict, ttft: float, out_rate: float) -> None:
    """Lay out a per-run results dir EXACTLY as the upstream script expects:
    ``{results_prefix}/{harness}_{run_id}_{stack}/benchmark_report_v0.2.yaml``."""
    rep = copy.deepcopy(base)
    agg = rep["results"]["request_performance"]["aggregate"]
    agg["latency"]["time_to_first_token"]["mean"] = ttft
    agg["throughput"]["output_token_rate"]["mean"] = out_rate
    run_dir = results_dir / f"{harness}_{run_id}_{stack}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "benchmark_report_v0.2.yaml").write_text(yaml.safe_dump(rep, sort_keys=False))


def test_wrapper_real_aggregation_over_fixture_reports(tmp_path, br_example):
    """Run the REAL wrapper (bench venv python) importing the REAL upstream aggregate_runs
    module over two fixture BR v0.2 reports. Asserts it writes the summary ONLY into the
    workspace output dir and the mean/std/min/max math is the upstream script's own."""
    s = get_settings()
    bench_python = s.bench_repo / ".venv" / "bin" / "python"
    if not bench_python.exists():
        pytest.skip("benchmark venv not installed (install.sh) — wrapper needs its interpreter")
    analysis_dir = s.bench_repo / "docs" / "analysis"
    if not (analysis_dir / "aggregate_runs.py").is_file():
        pytest.skip("benchmark repo docs/analysis/aggregate_runs.py not present")

    base = load_report(br_example)
    results_dir = tmp_path / "results"
    harness, stack = "inference-perf", "llm-d-7b-base"
    _write_run_report(results_dir, "run1", harness, stack, base, ttft=0.10, out_rate=100.0)
    _write_run_report(results_dir, "run2", harness, stack, base, ttft=0.20, out_rate=200.0)

    output_dir = tmp_path / "ws" / "aggregated"
    request = tmp_path / "ws" / "aggregate_request.json"
    request.parent.mkdir(parents=True, exist_ok=True)
    request.write_text(json.dumps({
        "analysis_dir": str(analysis_dir),
        "results_prefix": str(results_dir),
        "harness": harness,
        "stack": stack,
        "run_ids": ["run1", "run2"],
        "output": str(output_dir),
    }))

    proc = subprocess.run(
        [str(bench_python), str(WRAPPER), str(request)],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    out = json.loads(proc.stdout)
    assert out["ok"] is True
    assert out["run_count"] == 2

    # The summary landed ONLY in the workspace output dir (results dir + repo untouched).
    assert Path(out["summary_json_path"]).resolve().is_relative_to(output_dir.resolve())
    assert (output_dir / "aggregated_summary.json").is_file()
    assert not any(p.name.startswith("aggregated_summary") for p in results_dir.rglob("*"))

    # The cross-run TTFT mean is the average of the two runs (0.10, 0.20) -> 0.15, computed
    # by the UPSTREAM module (we only wired the inputs/outputs).
    metrics = out["metrics"]
    ttft_key = next(k for k in metrics if k.endswith("latency.time_to_first_token.mean"))
    assert metrics[ttft_key]["mean"] == pytest.approx(0.15)
    assert metrics[ttft_key]["min"] == pytest.approx(0.10)
    assert metrics[ttft_key]["max"] == pytest.approx(0.20)
    assert metrics[ttft_key]["count"] == 2


def test_wrapper_real_single_run_reports_too_few(tmp_path, br_example):
    """One run dir -> the upstream script needs >=2 -> the wrapper reports ok:false and
    writes nothing (no fabricated aggregation)."""
    s = get_settings()
    bench_python = s.bench_repo / ".venv" / "bin" / "python"
    analysis_dir = s.bench_repo / "docs" / "analysis"
    if not bench_python.exists() or not (analysis_dir / "aggregate_runs.py").is_file():
        pytest.skip("benchmark venv / aggregate_runs.py not present")

    base = load_report(br_example)
    results_dir = tmp_path / "results"
    _write_run_report(results_dir, "only", "inference-perf", "s", base, 0.1, 100.0)

    output_dir = tmp_path / "ws" / "aggregated"
    request = tmp_path / "ws" / "req.json"
    request.parent.mkdir(parents=True, exist_ok=True)
    request.write_text(json.dumps({
        "analysis_dir": str(analysis_dir), "results_prefix": str(results_dir),
        "harness": "inference-perf", "stack": "s", "run_ids": ["only"],
        "output": str(output_dir),
    }))

    proc = subprocess.run(
        [str(bench_python), str(WRAPPER), str(request)],
        capture_output=True, text=True, timeout=120,
    )
    out = json.loads(proc.stdout)
    assert out["ok"] is False
    assert not output_dir.exists() or not (output_dir / "aggregated_summary.json").exists()


def test_wrapper_bad_request_is_handled(tmp_path):
    """A malformed request (missing fields) -> ok:false, never a crash/traceback on stdout."""
    request = tmp_path / "req.json"
    request.write_text(json.dumps({"results_prefix": "x"}))  # missing the rest
    proc = subprocess.run(
        [sys.executable, str(WRAPPER), str(request)],
        capture_output=True, text=True, timeout=30,
    )
    out = json.loads(proc.stdout)
    assert out["ok"] is False
    assert "analysis_dir" in out["error"] or "non-empty" in out["error"]
