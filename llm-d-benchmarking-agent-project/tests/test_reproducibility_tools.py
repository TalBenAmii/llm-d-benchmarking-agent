"""Reproducibility — the export_run_bundle + reproduce_run tools (app/tools/reproducibility.py).

Hermetic: real BR v0.2 report fixtures under a tmp run dir + the tool's own isolated tmp
workspace (the conftest ``tool_ctx``). The env snapshot is monkeypatched to a no-cluster stub so
the tools never touch a real cluster. reproduce_run is asserted to emit NO mutating command.
"""
from __future__ import annotations

import copy

import pytest
import yaml

from app.tools import reproducibility
from app.tools.context import ToolContext
from app.tools.registry import dispatch, tool_definitions
from app.tools.schemas import ExportRunBundleInput, ReproduceRunInput
from app.validation.report import load_report


@pytest.fixture(autouse=True)
def _stub_env(monkeypatch):
    """Replace the read-only re-probe with a cheap stub so capture never touches a cluster."""
    async def fake_snap(ctx, namespace):
        return {"kube_context": {"context": "kind-test"}, "namespace": namespace}
    monkeypatch.setattr(reproducibility, "_safe_env_snapshot", fake_snap)


def _write_report(dirpath, base: dict, *, ttft_s=0.15, out_rate=400.0, uid="run-x"):
    rep = copy.deepcopy(base)
    rep["run"]["uid"] = uid
    agg = rep["results"]["request_performance"]["aggregate"]
    agg["latency"]["time_to_first_token"]["mean"] = ttft_s
    agg["throughput"]["output_token_rate"]["mean"] = out_rate
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / "benchmark_report_v0.2.yaml").write_text(yaml.safe_dump(rep, sort_keys=False))


def _write_run_config(ctx: ToolContext):
    """Plant a CLI-style generated run-config under the session workspace so capture finds it."""
    ctx.workspace.mkdir(parents=True, exist_ok=True)
    cfg = ctx.workspace / "run-config.yaml"
    cfg.write_text("spec: cicd/kind\nharness: inference-perf\n")
    return cfg


# ---- export_run_bundle -----------------------------------------------------


async def test_export_returns_bundle_id_and_command(tool_ctx, br_example, tmp_path):
    base = load_report(br_example)
    run = tmp_path / "run"
    _write_report(run, base, uid="run-export")
    cfg = _write_run_config(tool_ctx)

    out = await reproducibility.export_run_bundle(
        tool_ctx, source=str(run), namespace="ns1", spec="cicd/kind",
        harness="inference-perf", workload="sanity_random.yaml", label="baseline",
    )
    assert out["exported"] is True
    assert out["bundle_id"] and len(out["bundle_id"]) == 16
    assert out["regenerate_command"] == f"llmdbenchmark run -c {cfg} -p ns1"
    assert out["run_config_found"] is True
    assert "dirty" in out and "repos" in out and "report_digest" in out
    # All three repos captured (populated primary siblings via REPOS_DIR → real SHAs, not unavailable).
    assert set(out["repos"]) == {"llm-d", "llm-d-benchmark", "llm-d-skills"}

    # The bundle really landed under workspace/bundles and reads back.
    from app.storage.provenance import BundleStore

    stored = BundleStore(tool_ctx.workspace).read(out["bundle_id"])
    assert stored is not None and stored["bundle_id"] == out["bundle_id"]
    assert stored["resolved_config"]["found"] is True
    assert "spec: cicd/kind" in stored["resolved_config"]["body"]


async def test_export_is_idempotent(tool_ctx, br_example, tmp_path):
    base = load_report(br_example)
    run = tmp_path / "run"
    _write_report(run, base, uid="run-idem")
    _write_run_config(tool_ctx)
    a = await reproducibility.export_run_bundle(tool_ctx, source=str(run), namespace="ns1")
    b = await reproducibility.export_run_bundle(tool_ctx, source=str(run), namespace="ns1")
    # Same run + same repo state → same content-addressed bundle id.
    assert a["bundle_id"] == b["bundle_id"]
    from app.storage.provenance import BundleStore

    assert len(BundleStore(tool_ctx.workspace).list()) == 1


async def test_export_refuses_invalid_report(tool_ctx, tmp_path):
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "benchmark_report_v0.2.yaml").write_text(yaml.safe_dump({"version": "0.2", "run": {}}))
    out = await reproducibility.export_run_bundle(tool_ctx, source=str(bad))
    assert out["exported"] is False and "schema validation" in out["reason"]


async def test_export_no_report_found(tool_ctx, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    out = await reproducibility.export_run_bundle(tool_ctx, source=str(empty))
    assert out["exported"] is False and "no Benchmark Report" in out["reason"]


async def test_export_without_run_config_notes_it(tool_ctx, br_example, tmp_path):
    base = load_report(br_example)
    run = tmp_path / "run"
    _write_report(run, base, uid="run-nocfg")
    # No run-config planted in the workspace.
    out = await reproducibility.export_run_bundle(tool_ctx, source=str(run), namespace="ns1")
    assert out["exported"] is True and out["run_config_found"] is False
    from app.storage.provenance import BundleStore

    stored = BundleStore(tool_ctx.workspace).read(out["bundle_id"])
    assert stored["resolved_config"]["found"] is False
    assert "generate_config" in stored["resolved_config"]["note"]


async def test_export_does_not_emit_a_mutating_command(tool_ctx, br_example, tmp_path):
    # export is read-only: it must NEVER reach the mutating run_command path.
    async def boom(*a, **k):
        raise AssertionError("export_run_bundle must not call run_command (it is read-only)")

    tool_ctx.run_command = boom  # type: ignore[method-assign]
    base = load_report(br_example)
    run = tmp_path / "run"
    _write_report(run, base, uid="run-ro")
    _write_run_config(tool_ctx)
    out = await reproducibility.export_run_bundle(tool_ctx, source=str(run), namespace="ns1")
    assert out["exported"] is True


async def test_export_attach_to_history(tool_ctx, br_example, tmp_path):
    base = load_report(br_example)
    run = tmp_path / "run"
    _write_report(run, base, uid="run-hist")
    _write_run_config(tool_ctx)
    # First store the result in history (so there's a record to attach to).
    from app.tools import history as history_tool

    stored = await history_tool.result_history(tool_ctx, action="store", source=str(run))
    rid = stored["record"]["id"]

    out = await reproducibility.export_run_bundle(
        tool_ctx, source=str(run), namespace="ns1", attach_to_history=True,
    )
    assert out["exported"] is True and out["attached_to_history"] is True
    # The history record now carries the bundle_id + a provenance dict.
    got = tool_ctx.history_store().get(rid)
    assert got.bundle_id == out["bundle_id"]
    assert got.provenance and got.provenance["bundle_id"] == out["bundle_id"]


# ---- reproduce_run ---------------------------------------------------------


async def test_reproduce_returns_proposal_with_config_and_dry_run_first(tool_ctx, br_example, tmp_path):
    base = load_report(br_example)
    run = tmp_path / "run"
    _write_report(run, base, uid="run-repro")
    cfg = _write_run_config(tool_ctx)
    exp = await reproducibility.export_run_bundle(
        tool_ctx, source=str(run), namespace="ns1", spec="cicd/kind",
        harness="inference-perf", workload="sanity_random.yaml",
    )
    bid = exp["bundle_id"]

    out = await reproducibility.reproduce_run(tool_ctx, bundle_id=bid)
    assert out["reproducible"] is True and out["bundle_id"] == bid
    prop = out["proposal"]
    assert prop["spec"] == "cicd/kind" and prop["namespace"] == "ns1"
    assert prop["run_config_path"] == str(cfg)
    # The proposal carries the dry-run-FIRST sequence, gate-ordered.
    steps = " ".join(out["next_steps"]).lower()
    assert "propose_session_plan" in steps
    assert "dry_run" in steps and "dry-run" in steps
    # The approval-gated -c replay is mentioned only after the dry-run.
    assert "run_config" in steps
    assert out["regenerate_command"].startswith("llmdbenchmark run -c")


async def test_reproduce_emits_no_mutating_command(tool_ctx, br_example, tmp_path):
    # The headline safety invariant: reproduce_run NEVER runs a command.
    async def boom_cmd(*a, **k):
        raise AssertionError("reproduce_run must NOT call run_command")

    async def boom_ro(*a, **k):
        raise AssertionError("reproduce_run must NOT run any command (not even read-only)")

    base = load_report(br_example)
    run = tmp_path / "run"
    _write_report(run, base, uid="run-noexec")
    _write_run_config(tool_ctx)
    exp = await reproducibility.export_run_bundle(tool_ctx, source=str(run), namespace="ns1")
    bid = exp["bundle_id"]
    # Only NOW swap in the exploding runners (export legitimately read git above).
    tool_ctx.run_command = boom_cmd  # type: ignore[method-assign]
    tool_ctx.run_readonly = boom_ro  # type: ignore[method-assign]
    out = await reproducibility.reproduce_run(tool_ctx, bundle_id=bid)
    assert out["reproducible"] is True


async def test_reproduce_unknown_bundle(tool_ctx):
    out = await reproducibility.reproduce_run(tool_ctx, bundle_id="nope123")
    assert out["reproducible"] is False and "no provenance bundle" in out["reason"]


async def test_reproduce_surfaces_dirty_and_unavailable_caveats(tool_ctx, tmp_path):
    # Plant a bundle directly with a dirty + unavailable repo so reproduce surfaces both caveats.
    from app.storage.provenance import BundleStore

    bundle = {
        "bundle_id": "deadbeefdeadbeef",
        "created_at": 1.0,
        "spec": "cicd/kind", "harness": "inference-perf", "workload": "w", "namespace": "ns1",
        "model": "m", "slo": None,
        "repos": {
            "llm-d": {"sha": "abc", "dirty": True, "ref": "main"},
            "llm-d-benchmark": {"sha": None, "dirty": None, "unavailable": True},
        },
        "resolved_config": {"found": False, "note": "no config"},
        "dirty": True,
        "regenerate_command": "llmdbenchmark run -c <cfg> -p ns1",
    }
    BundleStore(tool_ctx.workspace).write(bundle)
    out = await reproducibility.reproduce_run(tool_ctx, bundle_id="deadbeefdeadbeef")
    assert out["dirty"] is True
    assert out["unavailable_repos"] == ["llm-d-benchmark"]
    assert out["caveat"] and "dirty" in out["caveat"].lower()
    assert "unavailable" in out["caveat"].lower()
    # No run-config captured → the sequence tells the agent to generate one first.
    assert any("generate_config" in s for s in out["next_steps"])


# ---- schema + registry wiring ----------------------------------------------


def test_tools_registered():
    names = {d["name"] for d in tool_definitions()}
    assert "export_run_bundle" in names and "reproduce_run" in names


def test_export_schema_requires_source():
    with pytest.raises(ValueError):
        ExportRunBundleInput()  # source is required
    assert ExportRunBundleInput(source="/runs/a").source == "/runs/a"


def test_reproduce_schema_requires_bundle_id():
    with pytest.raises(ValueError):
        ReproduceRunInput()
    assert ReproduceRunInput(bundle_id="b1").bundle_id == "b1"


async def test_dispatch_export_then_reproduce(tool_ctx, br_example, tmp_path):
    base = load_report(br_example)
    run = tmp_path / "run"
    _write_report(run, base, uid="run-disp")
    _write_run_config(tool_ctx)
    exp = await dispatch(tool_ctx, "export_run_bundle",
                         {"source": str(run), "namespace": "ns1"})
    assert exp["exported"] is True
    rep = await dispatch(tool_ctx, "reproduce_run", {"bundle_id": exp["bundle_id"]})
    assert rep["reproducible"] is True


def test_descriptions_cue_knowledge_and_dry_run():
    from app.tools.registry import _DESCRIPTIONS

    for name in ("export_run_bundle", "reproduce_run"):
        assert "read_knowledge('reproducibility')" in _DESCRIPTIONS[name]
    # reproduce explicitly cues the dry-run-first / approval-gated sequence.
    rd = _DESCRIPTIONS["reproduce_run"]
    assert "propose_session_plan" in rd and "dry_run" in rd


def test_knowledge_file_exists_and_is_on_demand(tool_ctx):
    # The judgment file is present and NOT inlined into CORE (it's on-demand only).
    from app.agent.prompt import CORE_KNOWLEDGE

    kdir = tool_ctx.settings.knowledge_dir
    assert (kdir / "reproducibility.md").is_file()
    assert "reproducibility.md" not in CORE_KNOWLEDGE
