"""Hermetic tests for per-knob vLLM scenario authoring (Phase 45).

Cover ``write_and_validate_config(artifact_type="scenario", …)`` in
``app/tools/setup/config_artifact.py``: the agent supplies per-knob OVERRIDES, the tool deep-merges
them onto a minimal ``scenario: [ {name, …} ]`` skeleton, SHAPE-validates the knobs against
the repo's own scenario examples (read LIVE — the read-only sibling repo is the only on-disk
dependency, and the validator degrades gracefully if it is absent), and writes the file into
the SESSION workspace — never into the read-only repo. No network, no cluster, no GPU.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.tools.setup.config_artifact import (
    _build_scenario_document,
    _build_spec_document,
    _scenario_reference,
    _spec_filename,
    validate_scenario_structure,
    write_and_validate_config,
)
from app.tools.context import ToolError
from app.tools.registry import dispatch, tool_definitions

# ---------------------------------------------------------------------------
# Pure mechanism: dotted-override deep-merge onto the scenario skeleton
# ---------------------------------------------------------------------------


def test_deep_merge_builds_nested_scenario_from_dotted_overrides():
    doc = _build_scenario_document(
        "kind-sim-eager",
        {
            "name": "kind-sim-eager",  # folded into the item name; never a knob
            "vllmCommon.flags.enforceEager": True,
            "vllmCommon.flags.noPrefixCaching": True,
            "vllmCommon.kvTransfer.enabled": True,
            "vllmCommon.kvTransfer.connector": "NixlConnector",
            "schedulerName": "custom-binpack-scheduler",
            "routing.servicePort": 8000,
            "decode.schedulerName": "gang-scheduler",
            "affinity.enabled": True,
            "affinity.nodeSelector": {"kubernetes.io/os": "linux"},
        },
    )
    assert list(doc) == ["scenario"]
    assert len(doc["scenario"]) == 1
    item = doc["scenario"][0]
    # The dotted paths became real nested mappings (not literal dotted keys).
    assert item["name"] == "kind-sim-eager"
    assert item["vllmCommon"]["flags"] == {"enforceEager": True, "noPrefixCaching": True}
    assert item["vllmCommon"]["kvTransfer"] == {"enabled": True, "connector": "NixlConnector"}
    assert item["schedulerName"] == "custom-binpack-scheduler"
    assert item["routing"]["servicePort"] == 8000
    assert item["decode"]["schedulerName"] == "gang-scheduler"
    assert item["affinity"] == {"enabled": True, "nodeSelector": {"kubernetes.io/os": "linux"}}
    # No literal dotted key leaked into the document.
    assert not any("." in k for k in item)


def test_deep_merge_is_deterministic_over_key_order():
    a = _build_scenario_document("s", {"vllmCommon.flags.enforceEager": True, "schedulerName": "x"})
    b = _build_scenario_document("s", {"schedulerName": "x", "vllmCommon.flags.enforceEager": True})
    assert yaml.safe_dump(a, sort_keys=False) == yaml.safe_dump(b, sort_keys=False)


# ---------------------------------------------------------------------------
# Structural validator (against the repo's scenario format)
# ---------------------------------------------------------------------------


def test_validate_accepts_known_knobs_against_reference():
    doc = _build_scenario_document(
        "ok",
        {"vllmCommon.flags.enforceEager": True, "schedulerName": "sched", "affinity.enabled": True},
    )
    # The repo's real scenario knob keys (a superset of what we set here).
    reference = {"knob_keys": ["name", "vllmCommon", "schedulerName", "affinity", "decode", "routing"]}
    assert validate_scenario_structure(doc, reference) == []


def test_validate_rejects_unknown_top_level_knob_against_reference():
    # `vllmComon` is a typo — not a repo scenario key, so it must be refused.
    doc = {"scenario": [{"name": "t", "vllmComon": {"flags": {}}}]}
    reference = {"knob_keys": ["name", "vllmCommon", "schedulerName"]}
    errors = validate_scenario_structure(doc, reference)
    assert any("vllmComon" in e for e in errors)


def test_validate_rejects_missing_scenario_list():
    errors = validate_scenario_structure({"name": "x"}, {})
    assert any("scenario" in e for e in errors)


def test_validate_rejects_unnamed_item():
    errors = validate_scenario_structure({"scenario": [{"schedulerName": "x"}]}, {})
    assert any("name" in e for e in errors)


def test_validate_rejects_item_without_knobs():
    errors = validate_scenario_structure({"scenario": [{"name": "x"}]}, {})
    assert any("override knob" in e for e in errors)


def test_validate_rejects_duplicate_item_names():
    doc = {"scenario": [{"name": "x", "schedulerName": "a"}, {"name": "x", "schedulerName": "b"}]}
    errors = validate_scenario_structure(doc, {"knob_keys": ["name", "schedulerName"]})
    assert any("duplicate" in e for e in errors)


def test_validate_empty_reference_only_checks_intrinsic_shape():
    # No reference → unknown keys are allowed (only the intrinsic shape contract applies).
    doc = {"scenario": [{"name": "x", "totallyMadeUpKey": 1}]}
    assert validate_scenario_structure(doc, {}) == []


# ---------------------------------------------------------------------------
# Live reference derived from the read-only repo (never vendored)
# ---------------------------------------------------------------------------


def test_scenario_reference_reads_real_repo_knob_keys(tool_ctx):
    if not tool_ctx.settings.bench_repo.joinpath("config", "scenarios").is_dir():
        import pytest

        pytest.skip("bench repo scenarios not present")
    ref = _scenario_reference(tool_ctx.settings.bench_repo)
    assert ref["examples"]  # at least one example scenario file was read
    keys = set(ref["knob_keys"])
    # The exact knob paths Phase 45 targets must all be repo-known top-level scenario keys.
    for k in ("vllmCommon", "affinity", "schedulerName", "routing", "decode", "prefill", "model"):
        assert k in keys, f"{k} missing from live repo scenario knob keys: {sorted(keys)}"


def test_scenario_reference_absent_repo_degrades_to_empty(tmp_path):
    assert _scenario_reference(tmp_path / "nonexistent-repo") == {}


# ---------------------------------------------------------------------------
# The tool: author into the workspace + validate (the ACCEPTANCE path)
# ---------------------------------------------------------------------------


async def test_author_scenario_writes_validated_file_into_workspace(tool_ctx):
    """ACCEPTANCE: the agent produces a validated scenario with custom vLLM/scheduling knobs;
    the file lands in the session workspace (never the repo) and passes structural validation
    against the repo's example shape."""
    out = await write_and_validate_config(
        tool_ctx,
        artifact_type="scenario",
        target_filename="custom-knobs.yaml",
        content={
            "name": "kind-sim-custom",
            "vllmCommon.flags.enforceEager": True,
            "vllmCommon.flags.noPrefixCaching": True,
            "vllmCommon.kvTransfer.enabled": True,
            "vllmCommon.kvTransfer.connector": "NixlConnector",
            "vllmCommon.priorityClassName": "nightly-critical",
            "vllmCommon.ephemeralStorage": "20Gi",
            "schedulerName": "custom-binpack-scheduler",
            "routing.servicePort": 8000,
            "affinity.enabled": True,
            "affinity.nodeSelector": {"kubernetes.io/os": "linux"},
        },
    )
    assert out["artifact_type"] == "scenario"
    assert out["valid"] is True
    assert out["errors"] == []
    assert out["scenario_name"] == "kind-sim-custom"
    # The knobs the agent set are reported (handy for the chat trail).
    assert "schedulerName" in out["knobs_set"]
    assert "vllmCommon.kvTransfer.connector" in out["knobs_set"]

    # The file landed INSIDE the session workspace (never the read-only repo).
    path = Path(out["path"])
    assert path.is_file()
    assert tool_ctx.workspace in path.parents
    bench_repo = tool_ctx.settings.bench_repo
    assert bench_repo not in path.parents  # NOT written into the repo

    # The emitted YAML parses and has the upstream scenario shape with our nested knobs.
    doc = yaml.safe_load(path.read_text())
    item = doc["scenario"][0]
    assert item["name"] == "kind-sim-custom"
    assert item["vllmCommon"]["flags"]["enforceEager"] is True
    assert item["vllmCommon"]["kvTransfer"]["connector"] == "NixlConnector"
    assert item["vllmCommon"]["priorityClassName"] == "nightly-critical"
    assert item["schedulerName"] == "custom-binpack-scheduler"
    assert item["routing"]["servicePort"] == 8000


async def test_author_scenario_validates_against_real_repo_examples(tool_ctx):
    """When the bench repo is present, the tool reports which example scenario files it
    validated the authored knobs against (read live, never vendored)."""
    if not tool_ctx.settings.bench_repo.joinpath("config", "scenarios").is_dir():
        import pytest

        pytest.skip("bench repo scenarios not present")
    out = await write_and_validate_config(
        tool_ctx,
        artifact_type="scenario",
        target_filename="against-examples.yaml",
        content={"name": "x", "schedulerName": "custom-sched"},
    )
    assert out["valid"] is True
    assert out["validated_against_examples"]


async def test_author_scenario_rejects_unknown_knob_against_real_repo(tool_ctx):
    """A misspelled/unknown top-level knob is refused against the LIVE repo reference, and NO
    file is written (the agent self-corrects)."""
    if not tool_ctx.settings.bench_repo.joinpath("config", "scenarios").is_dir():
        import pytest

        pytest.skip("bench repo scenarios not present")
    out = await write_and_validate_config(
        tool_ctx,
        artifact_type="scenario",
        target_filename="bad-knob.yaml",
        content={"name": "x", "vllmComon": {"flags": {}}},  # typo'd vllmCommon
    )
    assert out["valid"] is False
    assert any("vllmComon" in e for e in out["errors"])
    # No file was written for the rejected request.
    assert not (tool_ctx.workspace / "bad-knob.yaml").exists()


@pytest.mark.parametrize("filename,content,match", [
    ("noname.yaml", {"schedulerName": "x"}, "name"),                        # missing name
    ("bare.yaml", {"name": "x"}, "override knob"),                          # no override knob
    ("../escape.yaml", {"name": "x", "schedulerName": "y"}, None),          # path traversal
])
async def test_author_scenario_rejects_bad_input(tool_ctx, filename, content, match):
    with pytest.raises(ToolError, match=match):
        await write_and_validate_config(
            tool_ctx,
            artifact_type="scenario",
            target_filename=filename,
            content=content,
        )


# ---------------------------------------------------------------------------
# The MVP workload/run_config branch stays unchanged (regression guard)
# ---------------------------------------------------------------------------


async def test_workload_branch_still_writes_verbatim(tool_ctx):
    out = await write_and_validate_config(
        tool_ctx,
        artifact_type="workload",
        target_filename="wl.yaml",
        content={"a": 1, "b": [2, 3]},
    )
    assert out["artifact_type"] == "workload"
    assert out["valid"] is True
    doc = yaml.safe_load(Path(out["path"]).read_text())
    assert doc == {"a": 1, "b": [2, 3]}


async def test_unknown_artifact_type_rejected(tool_ctx):
    raised = False
    try:
        await write_and_validate_config(
            tool_ctx, artifact_type="bogus", target_filename="x.yaml", content={"a": 1}
        )
    except ToolError:
        raised = True
    assert raised


# ---------------------------------------------------------------------------
# Registry wiring + dispatch (the LLM-facing surface)
# ---------------------------------------------------------------------------


def test_scenario_artifact_type_in_schema():
    spec = next(d for d in tool_definitions() if d["name"] == "write_and_validate_config")
    enum = spec["input_schema"]["properties"]["artifact_type"]["enum"]
    assert "scenario" in enum
    # The description steers the agent to the knowledge file + the determinism gate.
    assert "vllm_overrides" in spec["description"]


async def test_dispatch_authors_scenario_end_to_end(tool_ctx):
    result = await dispatch(
        tool_ctx,
        "write_and_validate_config",
        {
            "artifact_type": "scenario",
            "target_filename": "e2e.yaml",
            "content": {"name": "e2e", "vllmCommon.flags.enforceEager": True},
        },
    )
    assert result["valid"] is True
    assert (tool_ctx.workspace / "e2e.yaml").is_file()


# ---------------------------------------------------------------------------
# Companion --spec file: the concrete plumbing into the determinism gate
# ---------------------------------------------------------------------------


def test_spec_filename_derives_companion_name():
    assert _spec_filename("custom-knobs.yaml") == "custom-knobs.spec.yaml"
    assert _spec_filename("x.yml") == "x.spec.yaml"


def test_build_spec_document_wires_scenario_and_repo_paths(tmp_path):
    bench_repo = tmp_path / "bench"
    scenario_path = tmp_path / "ws" / "custom-knobs.yaml"
    doc = _build_spec_document(bench_repo, scenario_path)
    # scenario_file points at the AUTHORED scenario; values/template at the read-only repo.
    assert doc["scenario_file"]["path"] == str(scenario_path)
    assert doc["values_file"]["path"] == str(
        bench_repo / "config" / "templates" / "values" / "defaults.yaml"
    )
    assert doc["template_dir"]["path"] == str(bench_repo / "config" / "templates" / "jinja")
    assert doc["base_dir"] == str(bench_repo)
    # All paths absolute & self-contained (no Jinja base_dir placeholder needed).
    assert "{{" not in yaml.safe_dump(doc)


async def test_author_scenario_also_writes_companion_spec_into_workspace(tool_ctx):
    """ACCEPTANCE (gate plumbing): authoring a scenario ALSO emits a companion --spec file in
    the workspace whose scenario_file points at the authored scenario. This is the artifact's
    demonstrated route into plan/--dry-run — without it, the gate could not target the file."""
    out = await write_and_validate_config(
        tool_ctx,
        artifact_type="scenario",
        target_filename="gateable.yaml",
        content={"name": "gateable", "vllmCommon.flags.enforceEager": True},
    )
    assert out["valid"] is True
    spec_path = Path(out["spec_path"])
    assert spec_path.is_file()
    assert spec_path.name == "gateable.spec.yaml"
    # The companion spec lives in the workspace — never the read-only repo.
    assert tool_ctx.workspace in spec_path.parents
    assert tool_ctx.settings.bench_repo not in spec_path.parents

    spec_doc = yaml.safe_load(spec_path.read_text())
    # It points the CLI at the AUTHORED scenario (the one we just wrote, in the workspace).
    assert spec_doc["scenario_file"]["path"] == out["path"]
    assert Path(spec_doc["scenario_file"]["path"]).is_file()
    # …and at the read-only repo's stock values/template (so plan renders like upstream).
    assert spec_doc["values_file"]["path"].endswith("config/templates/values/defaults.yaml")
    assert spec_doc["template_dir"]["path"].endswith("config/templates/jinja")
    # The note steers the agent to feed spec_path to the gate.
    assert out["spec_path"] in out["note"]
    assert "dry_run" in out["note"]


async def test_companion_spec_path_passes_the_allowlist_as_a_spec_value(tool_ctx, catalog):
    """The authored spec path must be an ACCEPTED --spec value so plan/--dry-run can target it.
    Build the exact argv execute_llmdbenchmark would and assert the real allowlist allows it
    (and classifies the dry-run plan as read-only). This closes the 'no demonstrated route into
    the gate' gap structurally, without needing a live cluster."""
    out = await write_and_validate_config(
        tool_ctx,
        artifact_type="scenario",
        target_filename="allowed.yaml",
        content={"name": "allowed", "schedulerName": "custom-sched"},
    )
    spec_path = out["spec_path"]
    argv = [
        "llmdbenchmark", "--spec", spec_path, "plan",
        "-p", "test-ns", "--dry-run",
    ]
    decision = tool_ctx.allowlist.validate(argv, catalog=catalog)
    assert decision.allowed, decision.reason
    # --dry-run is a read_only_trigger, so previewing the authored scenario is read-only.
    from app.security.allowlist import READ_ONLY

    assert decision.mode == READ_ONLY
    # A bogus non-workspace, non-catalog spec value is still refused (the gate didn't widen).
    bad = tool_ctx.allowlist.validate(
        ["llmdbenchmark", "--spec", "not-a-real-spec", "plan", "--dry-run"], catalog=catalog
    )
    assert not bad.allowed


async def test_plan_dry_run_executes_against_the_authored_spec(tool_ctx):
    """END-TO-END (mocked CLI): the agent authors a scenario, then runs the determinism gate
    THROUGH execute_llmdbenchmark with spec=<spec_path>. A recording runner stands in for the
    real CLI (no cluster), so we verify the authored spec actually reaches plan/--dry-run."""
    from app.tools.run.execute import execute_llmdbenchmark
    from tests.flows.harness import CaptureRunner

    out = await write_and_validate_config(
        tool_ctx,
        artifact_type="scenario",
        target_filename="gated.yaml",
        content={"name": "gated", "vllmCommon.flags.enforceEager": True},
    )
    spec_path = out["spec_path"]

    runner = CaptureRunner(tool_ctx.settings.repo_paths)
    tool_ctx.runner = runner
    # Feed the live on-disk catalog the spec value is validated against; the workspace
    # *.spec.yaml is admitted via the spec_workspace_path alternative, not the catalog.
    result = await execute_llmdbenchmark(
        tool_ctx, subcommand="plan", spec=spec_path,
        namespace="test-ns", flags={"dry_run": True},
    )
    assert result["exit_code"] == 0
    assert result["mode"] == "read_only"
    # The authored spec path actually reached the CLI argv (the gate targeted our file).
    assert "--spec" in result["argv"]
    assert spec_path in result["argv"]
    assert "--dry-run" in result["argv"]
    # And the runner really was invoked with that argv.
    assert any(spec_path in c["argv"] for c in runner.calls)
