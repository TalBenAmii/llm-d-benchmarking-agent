"""Hermetic tests for convert_guide_to_scenario (Phase 53).

Cover the agent's WORKSPACE-ONLY variant of upstream's skills/convert-guide. Upstream writes
ai.<name>.sh + ai.<name>.yaml INTO the read-only benchmark repo; this tool must write ONLY into
the session workspace. The agent supplies an already-resolved LLMDBENCH_* env map (the mapping
JUDGMENT is knowledge/convert_guide.md). Python only EMITS the sorted, shell-quoted scenario .sh
and a VALIDATABLE companion YAML scenario + .spec.yaml twin (reusing the Phase-45 mechanism) so
the required "validate via plan/--dry-run" gate has a real, allowlisted target.

No network, no cluster, no GPU — the read-only sibling repo (for the live scenario examples) is
the only on-disk dependency, and the structural validator degrades gracefully if it is absent.
"""
from __future__ import annotations

import shlex
from pathlib import Path

import pytest
import yaml

from app.tools.context import ToolError
from app.tools.convert_guide import (
    _build_scenario_sh,
    _emit_export_lines,
    _scenario_twin_content,
    _validate_name,
    convert_guide_to_scenario,
)
from app.tools.registry import dispatch, tool_definitions

# ---------------------------------------------------------------------------
# Name screen (mechanism that keeps every output inside the workspace)
# ---------------------------------------------------------------------------


def test_validate_name_accepts_a_bare_token():
    assert _validate_name("inference-scheduling") == "inference-scheduling"
    assert _validate_name("pd.disagg_v2") == "pd.disagg_v2"


@pytest.mark.parametrize("bad", ["", "  ", "a/b", "../escape", "a b", "x/../y", "name\nx"])
def test_validate_name_rejects_path_or_metachars(bad):
    with pytest.raises(ToolError):
        _validate_name(bad)


# ---------------------------------------------------------------------------
# Export-line emission: prefix enforcement, sorting, shell-quoting, sources
# ---------------------------------------------------------------------------


def test_emit_export_lines_sorted_and_shell_quoted():
    lines = _emit_export_lines(
        {
            "LLMDBENCH_VLLM_MODELSERVICE_DECODE_REPLICAS": "2",
            "LLMDBENCH_DEPLOY_MODEL_LIST": "Qwen/Qwen3-32B",
        },
        None,
    )
    # Keys are emitted in sorted order (deterministic output).
    assert lines == [
        "export LLMDBENCH_DEPLOY_MODEL_LIST=Qwen/Qwen3-32B",
        "export LLMDBENCH_VLLM_MODELSERVICE_DECODE_REPLICAS=2",
    ]


def test_emit_export_lines_quotes_values_with_spaces_and_metachars():
    val = 'python3 /setup/x.py; source $HOME/env.sh && echo "hi"'
    lines = _emit_export_lines({"LLMDBENCH_VLLM_COMMON_PREPROCESS": val}, None)
    assert len(lines) == 1
    export = lines[0]
    assert export.startswith("export LLMDBENCH_VLLM_COMMON_PREPROCESS=")
    quoted = export.split("=", 1)[1]
    # shlex.quote makes the value a single, injection-safe shell token that round-trips.
    assert shlex.split(quoted) == [val]


def test_emit_export_lines_rejects_non_llmdbench_key():
    with pytest.raises(ToolError):
        _emit_export_lines({"PATH": "/usr/bin"}, None)


def test_emit_export_lines_rejects_non_string_value():
    with pytest.raises(ToolError):
        _emit_export_lines({"LLMDBENCH_X": 2}, None)  # type: ignore[dict-item]


def test_emit_export_lines_includes_source_comment_above_export():
    lines = _emit_export_lines(
        {"LLMDBENCH_DEPLOY_MODEL_LIST": "Qwen/Qwen3-32B"},
        {"LLMDBENCH_DEPLOY_MODEL_LIST": "ms/values.yaml  lines 12-13"},
    )
    assert lines[0] == "# SOURCE: ms/values.yaml lines 12-13"  # whitespace collapsed to one line
    assert lines[1] == "export LLMDBENCH_DEPLOY_MODEL_LIST=Qwen/Qwen3-32B"


def test_build_scenario_sh_has_header_and_source_ref_and_body():
    text = _build_scenario_sh(
        name="inf-sched",
        env={"LLMDBENCH_DEPLOY_MODEL_LIST": "Qwen/Qwen3-32B"},
        sources=None,
        source_ref="https://github.com/llm-d/llm-d/tree/main/guides/inference-scheduling",
    )
    assert text.startswith("# inf-sched — scenario converted from an llm-d guide")
    assert "# Source guide: https://github.com/llm-d/llm-d/tree/main/guides/inference-scheduling" in text
    assert "read_knowledge('convert_guide')" in text  # points the reader at the mapping judgment
    assert "export LLMDBENCH_DEPLOY_MODEL_LIST=Qwen/Qwen3-32B" in text


# ---------------------------------------------------------------------------
# The validatable twin content shaping
# ---------------------------------------------------------------------------


def test_twin_content_minimal_when_no_scenario_override():
    content = _scenario_twin_content("inf-sched", None)
    assert content["name"] == "inf-sched"
    # A repo-known knob so the twin validates + renders.
    assert content["model.shortName"] == "inf-sched"


def test_twin_content_uses_override_and_forces_name():
    content = _scenario_twin_content(
        "inf-sched", {"name": "ignored", "decode.parallelism.tensor": 2}
    )
    assert content["name"] == "inf-sched"  # forced to the guide name, override name ignored
    assert content["decode.parallelism.tensor"] == 2


# ---------------------------------------------------------------------------
# The tool: author all four artifacts into the workspace (ACCEPTANCE)
# ---------------------------------------------------------------------------


async def test_convert_writes_sh_yaml_and_spec_into_workspace(tool_ctx):
    """ACCEPTANCE: the agent produces a validated workspace-local scenario from a guide map;
    NO write into the read-only repo."""
    out = await convert_guide_to_scenario(
        tool_ctx,
        name="inference-scheduling",
        env={
            "LLMDBENCH_DEPLOY_MODEL_LIST": "Qwen/Qwen3-32B",
            "LLMDBENCH_VLLM_MODELSERVICE_DECODE_REPLICAS": "2",
            "LLMDBENCH_VLLM_MODELSERVICE_DECODE_MODEL_COMMAND": "custom",
        },
        sources={"LLMDBENCH_DEPLOY_MODEL_LIST": "ms/values.yaml lines 12-13"},
        source_ref="https://github.com/llm-d/llm-d/tree/main/guides/inference-scheduling",
    )
    assert out["valid"] is True
    assert out["errors"] == []
    assert out["scenario_name"] == "inference-scheduling"

    sh_path = Path(out["scenario_sh_path"])
    yaml_path = Path(out["scenario_yaml_path"])
    spec_path = Path(out["spec_path"])
    for p in (sh_path, yaml_path, spec_path):
        assert p.is_file()
        # Every output is INSIDE the session workspace — never the read-only repo.
        assert tool_ctx.workspace in p.parents
        assert tool_ctx.settings.bench_repo not in p.parents

    # Upstream-shaped file names with the ai. prefix.
    assert sh_path.name == "ai.inference-scheduling.sh"
    assert yaml_path.name == "ai.inference-scheduling.yaml"
    assert spec_path.name == "ai.inference-scheduling.spec.yaml"

    # The .sh carries the resolved exports + the SOURCE provenance + the harness/profile defaults.
    sh = sh_path.read_text()
    assert "export LLMDBENCH_DEPLOY_MODEL_LIST=Qwen/Qwen3-32B" in sh
    assert "# SOURCE: ms/values.yaml lines 12-13" in sh
    assert "export LLMDBENCH_HARNESS_NAME=inference-perf" in sh
    assert "export LLMDBENCH_HARNESS_EXPERIMENT_PROFILE=sanity_random.yaml" in sh

    # The companion spec points at the AUTHORED yaml twin (in the workspace).
    spec_doc = yaml.safe_load(spec_path.read_text())
    assert spec_doc["scenario_file"]["path"] == str(yaml_path)
    # …and at the read-only repo's stock values/template, so plan renders like upstream.
    assert spec_doc["values_file"]["path"].endswith("config/templates/values/defaults.yaml")
    assert spec_doc["template_dir"]["path"].endswith("config/templates/jinja")

    # The note steers the agent to the determinism gate with the spec path.
    assert out["spec_path"] in out["note"]
    assert "dry_run" in out["note"]


async def test_convert_records_harness_and_profile_overrides(tool_ctx):
    out = await convert_guide_to_scenario(
        tool_ctx,
        name="pd-disagg",
        env={"LLMDBENCH_DEPLOY_MODEL_LIST": "meta-llama/Llama-3.1-8B"},
        harness="vllm-benchmark",
        profile="shared_prefix_synthetic.yaml",
    )
    assert out["harness"] == "vllm-benchmark"
    assert out["profile"] == "shared_prefix_synthetic.yaml"
    sh = Path(out["scenario_sh_path"]).read_text()
    assert "export LLMDBENCH_HARNESS_NAME=vllm-benchmark" in sh
    assert "export LLMDBENCH_HARNESS_EXPERIMENT_PROFILE=shared_prefix_synthetic.yaml" in sh


async def test_convert_does_not_clobber_explicit_harness_in_env(tool_ctx):
    """If the guide map already sets the harness var explicitly, it wins over the default."""
    out = await convert_guide_to_scenario(
        tool_ctx,
        name="explicit",
        env={
            "LLMDBENCH_DEPLOY_MODEL_LIST": "x/y",
            "LLMDBENCH_HARNESS_NAME": "guidellm",
        },
    )
    assert out["harness"] == "guidellm"
    sh = Path(out["scenario_sh_path"]).read_text()
    assert "export LLMDBENCH_HARNESS_NAME=guidellm" in sh
    assert "export LLMDBENCH_HARNESS_NAME=inference-perf" not in sh


async def test_convert_twin_accepts_scenario_override_knobs(tool_ctx):
    """A richer `scenario` override is folded into the validatable YAML twin and rendered."""
    if not tool_ctx.settings.bench_repo.joinpath("config", "scenarios").is_dir():
        pytest.skip("bench repo scenarios not present")
    out = await convert_guide_to_scenario(
        tool_ctx,
        name="tuned",
        env={"LLMDBENCH_DEPLOY_MODEL_LIST": "x/y"},
        scenario={
            "model.shortName": "tuned-model",
            "decode.parallelism.tensor": 2,
            "vllmCommon.flags.enforceEager": True,
        },
    )
    assert out["valid"] is True
    doc = yaml.safe_load(Path(out["scenario_yaml_path"]).read_text())
    item = doc["scenario"][0]
    assert item["name"] == "tuned"
    assert item["model"]["shortName"] == "tuned-model"
    assert item["decode"]["parallelism"]["tensor"] == 2
    assert item["vllmCommon"]["flags"]["enforceEager"] is True


async def test_convert_rejects_empty_env(tool_ctx):
    with pytest.raises(ToolError):
        await convert_guide_to_scenario(tool_ctx, name="x", env={})


async def test_convert_rejects_bad_name_before_writing(tool_ctx):
    with pytest.raises(ToolError):
        await convert_guide_to_scenario(
            tool_ctx, name="../escape", env={"LLMDBENCH_X": "1"}
        )
    # Nothing was written into the workspace for the rejected request.
    assert not list(tool_ctx.workspace.glob("ai.*")) if tool_ctx.workspace.exists() else True


async def test_convert_invalid_twin_surfaces_errors_without_claiming_valid(tool_ctx):
    """If the `scenario` override has an unknown top-level knob, the twin fails structural
    validation against the live repo examples — the tool reports valid=False with errors and
    does NOT pretend the scenario is gate-ready (the .sh is still emitted)."""
    if not tool_ctx.settings.bench_repo.joinpath("config", "scenarios").is_dir():
        pytest.skip("bench repo scenarios not present")
    out = await convert_guide_to_scenario(
        tool_ctx,
        name="bad-knob",
        env={"LLMDBENCH_DEPLOY_MODEL_LIST": "x/y"},
        scenario={"vllmComon": {"flags": {}}},  # typo'd vllmCommon -> unknown repo knob
    )
    assert out["valid"] is False
    assert any("vllmComon" in e for e in out["errors"])
    # The upstream-shaped .sh was still written (it's a faithful export of the guide).
    assert Path(out["scenario_sh_path"]).is_file()


# ---------------------------------------------------------------------------
# The authored spec is a real, allowlisted route into the determinism gate
# ---------------------------------------------------------------------------


async def test_authored_spec_passes_the_allowlist_as_a_spec_value(tool_ctx, catalog):
    """The companion spec path must be an ACCEPTED --spec value so plan/--dry-run can target it
    — without any allowlist widening (workspace *.spec.yaml is admitted via spec_workspace_path).
    """
    out = await convert_guide_to_scenario(
        tool_ctx,
        name="gateable",
        env={"LLMDBENCH_DEPLOY_MODEL_LIST": "x/y"},
    )
    spec_path = out["spec_path"]
    argv = ["llmdbenchmark", "--spec", spec_path, "plan", "-p", "test-ns", "--dry-run"]
    decision = tool_ctx.allowlist.validate(argv, catalog=catalog)
    assert decision.allowed, decision.reason
    from app.security.allowlist import READ_ONLY

    assert decision.mode == READ_ONLY  # previewing the authored scenario is read-only


async def test_plan_dry_run_executes_against_the_authored_spec(tool_ctx):
    """END-TO-END (mocked CLI): author from a guide map, then run the determinism gate THROUGH
    execute_llmdbenchmark with spec=<spec_path>. A recording runner stands in for the real CLI
    (no cluster), so we verify the authored spec actually reaches plan/--dry-run."""
    from app.tools.execute import execute_llmdbenchmark
    from tests.flows.harness import CaptureRunner

    out = await convert_guide_to_scenario(
        tool_ctx,
        name="gated",
        env={"LLMDBENCH_DEPLOY_MODEL_LIST": "x/y"},
    )
    spec_path = out["spec_path"]

    tool_ctx.runner = CaptureRunner(tool_ctx.settings.repo_paths)
    result = await execute_llmdbenchmark(
        tool_ctx, subcommand="plan", spec=spec_path, namespace="test-ns",
        flags={"dry_run": True},
    )
    assert result["exit_code"] == 0
    assert result["mode"] == "read_only"
    assert "--spec" in result["argv"]
    assert spec_path in result["argv"]
    assert "--dry-run" in result["argv"]


# ---------------------------------------------------------------------------
# Registry / schema wiring + knowledge presence (the LLM-facing surface)
# ---------------------------------------------------------------------------


def test_tool_registered_with_workspace_and_knowledge_cues():
    spec = next(
        (d for d in tool_definitions() if d["name"] == "convert_guide_to_scenario"), None
    )
    assert spec is not None
    desc = spec["description"]
    # The description must steer the agent to the knowledge mapping + the gate, and assert
    # workspace-only output.
    assert "convert_guide" in desc
    assert "workspace" in desc.lower()
    assert "dry_run" in desc
    # Schema carries the required env map + the name token.
    props = spec["input_schema"]["properties"]
    assert "env" in props and "name" in props
    assert set(spec["input_schema"]["required"]) >= {"name", "env"}


async def test_dispatch_converts_end_to_end(tool_ctx):
    result = await dispatch(
        tool_ctx,
        "convert_guide_to_scenario",
        {
            "name": "e2e-guide",
            "env": {"LLMDBENCH_DEPLOY_MODEL_LIST": "x/y"},
        },
    )
    assert result["valid"] is True
    assert (tool_ctx.workspace / "ai.e2e-guide.sh").is_file()
    assert (tool_ctx.workspace / "ai.e2e-guide.yaml").is_file()
    assert (tool_ctx.workspace / "ai.e2e-guide.spec.yaml").is_file()


def test_convert_guide_knowledge_is_reachable(tool_ctx):
    """The mapping JUDGMENT lives in knowledge/convert_guide.md and is loadable via the same
    read_knowledge mechanism the tool description points at."""
    from app.tools.probe import read_knowledge

    res = read_knowledge(tool_ctx, name="convert_guide")
    assert res.get("topic") == "convert_guide"
    body = res["content"]
    # It documents the LLMDBENCH_* mapping + the standard practices (the thin-code DATA half).
    assert "LLMDBENCH_" in body
    assert "DECODE_MODEL_COMMAND=custom" in body
    assert "REPLACE_ENV" in body
