"""Phase 6 — Capacity pre-flight (check_capacity).

Hermetic: NO network, NO GPU, NO benchmark venv. The pure pieces (spec/scenario
resolution, defaults+scenario merge, overrides, diagnostic classification) run against the
real on-disk repo (file reads only). The tool is exercised end-to-end through a
``CaptureRunner`` that fakes the ``capacity_check.py`` bridge's JSON stdout — so the whole
chain (render plan_config -> write request file -> allowlisted dispatch -> parse -> classify)
is covered without ever invoking the real planner.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.capacity.planner import (
    CapacityError,
    apply_overrides,
    classify_diagnostics,
    plan_config_for_spec,
    resolve_scenario_file,
)
from app.config import Settings, get_settings
from app.security.allowlist import MUTATING, READ_ONLY, Allowlist
from app.security.runner import CommandRunner, RunnerError
from app.tools.capacity import _parse_bridge_output, check_capacity
from app.tools.context import ToolContext, ToolError
from app.tools.registry import dispatch, tool_definitions
from tests.flows.harness import CaptureRunner

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST_PATH = PROJECT_ROOT / "security" / "allowlist.yaml"


# ---- diagnostic classification (the core verdict logic) -----------------------

def test_classify_feasible_when_only_info_and_warnings():
    diags = [
        "[decode] 24 GB per GPU, 24 x 0.9 = 21.6 GB available",
        "[decode] WARNING: TP=0 is invalid for facebook/opt-125m. Valid values: [1, 2]",
        "[decode] Max concurrent requests (worst case, each at max_model_len): 128",
    ]
    v = classify_diagnostics(diags)
    assert v.feasible is True
    assert v.will_fail is False
    assert v.errors == []
    assert len(v.warnings) == 1
    assert any("Max concurrent" in i for i in v.info)
    assert v.diagnostics == diags  # verbatim passthrough


def test_classify_infeasible_on_deployment_will_fail():
    diags = [
        "[decode] mistral requires 13.49 GB of memory",
        "[decode] ERROR: DEPLOYMENT WILL FAIL: Model loads but cannot serve any requests.",
        "[decode] ERROR: Available KV cache: 0.00 GB, required per request: 0.50 GB",
    ]
    v = classify_diagnostics(diags)
    assert v.feasible is False
    assert v.will_fail is True
    assert len(v.errors) == 2
    # An ERROR line is never miscounted as a warning or an info fact.
    assert v.warnings == []
    assert all("ERROR:" not in i for i in v.info)


def test_classify_infeasible_on_bare_error_without_fail_marker():
    # ERROR-tagged shortfall (enforce mode) with no explicit "WILL FAIL" header still gates.
    diags = ["[decode] ERROR: maxModelLen=16384 exceeds model limit of 1024 for opt-125m"]
    v = classify_diagnostics(diags)
    assert v.feasible is False
    assert v.will_fail is False  # no DEPLOYMENT WILL FAIL marker
    assert len(v.errors) == 1


def test_classify_empty_is_feasible():
    v = classify_diagnostics([])
    assert v.feasible is True and v.will_fail is False and v.diagnostics == []


# ---- spec -> scenario -> plan_config (reads real repo files, no network) ------

def test_resolve_scenario_file_reads_repo_truth(bench_repo):
    scen = resolve_scenario_file(bench_repo, "cicd/kind")
    assert scen.is_file()
    assert scen.name == "kind.yaml"
    assert "scenarios" in str(scen)


def test_resolve_scenario_file_unknown_spec_raises(bench_repo):
    with pytest.raises(CapacityError):
        resolve_scenario_file(bench_repo, "does/not-exist")


def test_plan_config_merges_scenario_over_defaults(bench_repo):
    pc, applied = plan_config_for_spec(bench_repo, "cicd/kind")
    assert applied == []  # no overrides
    # Scenario value wins over the defaults file (defaults maxModelLen is 16384).
    assert pc["model"]["maxModelLen"] == 1024
    assert pc["model"]["name"] == "facebook/opt-125m"
    # A defaults-only key that the scenario doesn't touch is still present (deep merge).
    assert "control" in pc and isinstance(pc["control"], dict)


def test_plan_config_overrides_applied_and_listed(bench_repo):
    pc, applied = plan_config_for_spec(
        bench_repo,
        "cicd/kind",
        overrides={
            "model": "meta-llama/Llama-3.1-8B",
            "max_model_len": 8192,
            "gpu_memory_gb": 80,
            "tensor_parallelism": 2,
        },
    )
    assert pc["model"]["name"] == "meta-llama/Llama-3.1-8B"
    assert pc["model"]["maxModelLen"] == 8192
    assert pc["accelerator"]["memory"] == 80
    assert pc["decode"]["parallelism"]["tensor"] == 2
    # Every applied override is reported for transparency.
    assert any("model.name" in a for a in applied)
    assert any("decode.parallelism.tensor = 2" in a for a in applied)


def test_unknown_override_key_rejected():
    with pytest.raises(CapacityError):
        apply_overrides({"model": {}}, {"totally_made_up_knob": 7})


def test_override_none_value_is_skipped():
    cfg = {"model": {"name": "x"}}
    applied = apply_overrides(cfg, {"max_model_len": None})
    assert applied == []
    assert "maxModelLen" not in cfg["model"]


# ---- bridge stdout parsing robustness -----------------------------------------

def test_parse_bridge_clean_json():
    out = json.dumps({"ok": True, "diagnostics": ["a", "b"]})
    parsed = _parse_bridge_output(out)
    assert parsed["ok"] is True and parsed["diagnostics"] == ["a", "b"]


def test_parse_bridge_tolerates_leading_log_noise():
    out = "WARNING: some hub chatter\n" + json.dumps({"ok": True, "diagnostics": []})
    parsed = _parse_bridge_output(out)
    assert parsed["ok"] is True


def test_parse_bridge_empty_is_not_ok():
    assert _parse_bridge_output("")["ok"] is False
    assert _parse_bridge_output("not json at all")["ok"] is False


# ---- the tool end-to-end (real plan_config + faked bridge) --------------------

def _real_repo_ctx(tmp_path, *, canned=None):
    """A ToolContext wired to the REAL repos (so plan_config resolution is genuine) but
    with a CaptureRunner that fakes the bridge subprocess. No approval channel needed —
    capacity_check is read-only and must auto-run."""
    s = get_settings()
    runner = CaptureRunner(s.repo_paths, canned=canned or {})
    emitted: list = []

    async def emit(t, p):
        emitted.append((t, p))

    ctx = ToolContext(
        settings=s,
        allowlist=Allowlist.from_file(ALLOWLIST_PATH),
        runner=runner,
        workspace=tmp_path / "ws",
        emit=emit,
    )
    return ctx, runner, emitted


_FEASIBLE_BRIDGE = json.dumps({
    "ok": True,
    "diagnostics": [
        "[decode] facebook/opt-125m requires 0.25 GB of memory",
        "[decode] Max concurrent requests (worst case): 256",
    ],
})

_INFEASIBLE_BRIDGE = json.dumps({
    "ok": True,
    "diagnostics": [
        "[decode] ERROR: DEPLOYMENT WILL FAIL: Insufficient GPU memory to load model.",
        "[decode] ERROR: Model requires 40.00 GB MORE memory than available.",
    ],
})


async def test_tool_feasible_path_writes_request_and_autoruns(tmp_path):
    ctx, runner, emitted = _real_repo_ctx(tmp_path, canned={"capacity_check.py": _FEASIBLE_BRIDGE})
    res = await check_capacity(ctx, spec="cicd/kind")

    assert res["ran"] is True
    assert res["feasible"] is True and res["will_fail"] is False
    assert any("Max concurrent" in i for i in res["info"])

    # The bridge ran exactly once, against a request file in the session workspace.
    bridge_calls = [c for c in runner.calls if c["argv"] and c["argv"][0] == "capacity_check.py"]
    assert len(bridge_calls) == 1
    req_path = Path(bridge_calls[0]["argv"][1])
    assert req_path.parent == ctx.workspace
    assert req_path.suffix == ".json"
    # The request file is a real, parseable plan_config request reflecting the spec.
    request = json.loads(req_path.read_text())
    assert request["ignore_failures"] is True  # enforce defaulted off
    assert request["plan_config"]["model"]["maxModelLen"] == 1024

    # Read-only => it auto-ran (no approval), and the command was announced.
    cmd_events = [p for t, p in emitted if t == "command"]
    assert cmd_events and cmd_events[0]["auto_run"] is True
    assert cmd_events[0]["mode"] == READ_ONLY


async def test_tool_infeasible_path_reports_will_fail(tmp_path):
    ctx, runner, _ = _real_repo_ctx(tmp_path, canned={"capacity_check.py": _INFEASIBLE_BRIDGE})
    res = await check_capacity(ctx, spec="cicd/kind", overrides={"model": "big/model"})
    assert res["ran"] is True
    assert res["feasible"] is False and res["will_fail"] is True
    assert len(res["errors"]) == 2
    assert "INFEASIBLE" in res["note"]
    assert any("model.name = 'big/model'" in a for a in res["applied_overrides"])


async def test_tool_enforce_sets_ignore_failures_false(tmp_path):
    ctx, runner, _ = _real_repo_ctx(tmp_path, canned={"capacity_check.py": _FEASIBLE_BRIDGE})
    await check_capacity(ctx, spec="cicd/kind", enforce=True)
    bridge_call = next(c for c in runner.calls if c["argv"][0] == "capacity_check.py")
    request = json.loads(Path(bridge_call["argv"][1]).read_text())
    assert request["ignore_failures"] is False  # enforce => strict (halting) read


async def test_tool_handles_bridge_not_ok(tmp_path):
    not_ok = json.dumps({"ok": False, "error": "could not import the capacity planner"})
    ctx, runner, _ = _real_repo_ctx(tmp_path, canned={"capacity_check.py": not_ok})
    res = await check_capacity(ctx, spec="cicd/kind")
    assert res["ran"] is False
    assert "could not import" in res["error"]
    # A non-verdict must NOT masquerade as feasible.
    assert "feasible" not in res


async def test_tool_unknown_spec_raises_toolerror(tmp_path):
    ctx, runner, _ = _real_repo_ctx(tmp_path, canned={"capacity_check.py": _FEASIBLE_BRIDGE})
    with pytest.raises(ToolError):
        await check_capacity(ctx, spec="no/such-spec")
    # It failed before running anything (plan_config could not be rendered).
    assert [c for c in runner.calls if c["argv"][0] == "capacity_check.py"] == []


async def test_tool_via_dispatch_validates_args(tmp_path):
    ctx, runner, _ = _real_repo_ctx(tmp_path, canned={"capacity_check.py": _FEASIBLE_BRIDGE})
    res = await dispatch(ctx, "check_capacity", {"spec": "cicd/kind"})
    assert res["ran"] is True and res["feasible"] is True
    # Bad args are returned, not raised, so the agent can self-correct.
    bad = await dispatch(ctx, "check_capacity", {})  # missing required 'spec'
    assert "error" in bad


def test_check_capacity_is_registered_as_a_tool():
    names = {d["name"] for d in tool_definitions()}
    assert "check_capacity" in names


# ---- allowlist + runner wiring -------------------------------------------------

def test_allowlist_capacity_check_is_read_only(allowlist):
    d = allowlist.validate(["capacity_check.py", "workspace/sessions/s1/capacity_request.json"])
    assert d.allowed and d.mode == READ_ONLY and not d.requires_approval


def test_allowlist_rejects_non_json_argument(allowlist):
    d = allowlist.validate(["capacity_check.py", "workspace/evil.sh"])
    assert not d.allowed


def test_allowlist_rejects_path_traversal(allowlist):
    d = allowlist.validate(["capacity_check.py", "../../etc/passwd.json"])
    assert not d.allowed


def test_allowlist_requires_the_positional(allowlist):
    d = allowlist.validate(["capacity_check.py"])
    assert not d.allowed  # missing required positional


def test_runner_resolves_python_via_bench_venv(tmp_path):
    """`python_via` prepends the benchmark venv's python to the vetted project script."""
    bench = tmp_path / "llm-d-benchmark"
    venv_bin = bench / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").write_text("")  # presence is enough for resolve()
    runner = CommandRunner({"llm-d-benchmark": bench, "llm-d": tmp_path / "llm-d"})

    entry = Allowlist.from_file(ALLOWLIST_PATH).executable("capacity_check.py")
    real, cwd = runner.resolve(["capacity_check.py", str(tmp_path / "req.json")], entry)
    assert real[0] == str(venv_bin / "python")
    assert real[1].endswith("scripts/capacity_check.py")
    assert real[2].endswith("req.json")


def test_runner_python_via_missing_venv_errors_clearly(tmp_path):
    bench = tmp_path / "llm-d-benchmark"
    bench.mkdir()
    runner = CommandRunner({"llm-d-benchmark": bench, "llm-d": tmp_path / "llm-d"})
    entry = Allowlist.from_file(ALLOWLIST_PATH).executable("capacity_check.py")
    with pytest.raises(RunnerError):
        runner.resolve(["capacity_check.py", str(tmp_path / "req.json")], entry)
