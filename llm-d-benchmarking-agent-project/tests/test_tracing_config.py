"""Hermetic tests for the distributed-tracing scenario block (Phase 54).

The benchmark CONFIGURES OpenTelemetry tracing on the deployed modelservice pods via a
``tracing:`` block on a scenario item (endpoint, sampling rate, service names) — it never
COLLECTS or shows traces. These tests cover authoring + validating that block through the
existing ``write_and_validate_config(artifact_type="scenario", …)`` mechanism:

* the dotted ``tracing.*`` overrides deep-merge into a nested ``tracing:`` mapping;
* the shape validator ACCEPTS the ``tracing`` top-level knob even though NO scenario example
  sets it (the ``_SOFT_OPTIONAL_KNOBS`` union) — while still rejecting genuine typos;
* the live repo reference yields ``tracing`` as an allowed knob key (so the block validates
  against the real repo too);
* the authored block lands in the SESSION workspace (never the read-only repo), emits a
  companion ``--spec`` file, and that spec path passes the real allowlist for plan/--dry-run;
* the upstream jinja template + benchmark docs ground the keys + the config-only limitation.

No network, no cluster, no GPU. The read-only sibling repo is the only on-disk dependency and
the validator degrades gracefully when it is absent.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from app.tools.registry import tool_definitions
from app.tools.setup.config_artifact import (
    _SOFT_OPTIONAL_KNOBS,
    _build_scenario_document,
    _scenario_reference,
    unrecognized_flags,
    validate_scenario_structure,
    write_and_validate_config,
)

# The exact dotted tracing knob paths the upstream modelservice jinja renders. Kept in lockstep
# with config/templates/jinja/13_ms-values.yaml.j2 and knowledge/observability.md §4.
_TRACING_OVERRIDES = {
    "tracing.enabled": True,
    "tracing.otlpEndpoint": "http://otel-collector:4317",
    "tracing.sampling.sampler": "parentbased_traceidratio",
    "tracing.sampling.samplerArg": "0.1",
    "tracing.serviceNames.vllmDecode": "vllm-decode",
    "tracing.serviceNames.vllmPrefill": "vllm-prefill",
    "tracing.serviceNames.routingProxy": "routing-proxy",
    "tracing.vllm.collectDetailedTraces": "true",
}


# ---------------------------------------------------------------------------
# Deep-merge: dotted tracing.* overrides become the nested tracing: block
# ---------------------------------------------------------------------------


def test_dotted_tracing_overrides_build_nested_block():
    doc = _build_scenario_document("traced", {"name": "traced", **_TRACING_OVERRIDES})
    item = doc["scenario"][0]
    assert item["name"] == "traced"
    # The dotted paths became a real nested mapping, not literal dotted keys.
    tracing = item["tracing"]
    assert tracing["enabled"] is True
    assert tracing["otlpEndpoint"] == "http://otel-collector:4317"
    assert tracing["sampling"] == {"sampler": "parentbased_traceidratio", "samplerArg": "0.1"}
    assert tracing["serviceNames"] == {
        "vllmDecode": "vllm-decode",
        "vllmPrefill": "vllm-prefill",
        "routingProxy": "routing-proxy",
    }
    assert tracing["vllm"] == {"collectDetailedTraces": "true"}
    # No literal dotted key leaked into the document.
    assert not any("." in k for k in item)
    assert not any("." in k for k in tracing)


def test_tracing_block_matches_upstream_jinja_keys():
    """The keys we author must be exactly the ones the upstream modelservice jinja renders, so
    plan/--dry-run actually emits a tracing: block. Ground them against the real template if the
    read-only repo is present (no copy is vendored)."""
    from app.config import get_settings

    bench_repo = get_settings().bench_repo
    tmpl = bench_repo / "config" / "templates" / "jinja" / "13_ms-values.yaml.j2"
    if not tmpl.is_file():
        import pytest

        pytest.skip("bench repo jinja template not present")
    text = tmpl.read_text()
    # The block is gated on tracing.enabled and renders each key we expose.
    assert "tracing is defined and tracing.enabled is defined" in text
    for fragment in (
        "tracing.otlpEndpoint",
        "tracing.sampling.sampler",
        "tracing.sampling.samplerArg",
        "tracing.serviceNames.vllmDecode",
        "tracing.serviceNames.vllmPrefill",
        "tracing.serviceNames.routingProxy",
        "tracing.vllm.collectDetailedTraces",
    ):
        assert fragment in text, f"{fragment} not rendered by the upstream jinja template"


# ---------------------------------------------------------------------------
# Validator: tracing is accepted even though no example sets it (soft-optional)
# ---------------------------------------------------------------------------


def test_validator_accepts_tracing_via_soft_optional_even_without_example():
    """The crux of Phase 54: a derived reference that does NOT list 'tracing' (because no
    scenario example uses it) must STILL accept a tracing block — that is what the soft-optional
    union fixes. Simulate a reference exactly as _scenario_reference unions it."""
    assert "tracing" in _SOFT_OPTIONAL_KNOBS
    doc = _build_scenario_document("traced", {"name": "traced", **_TRACING_OVERRIDES})
    # A reference whose example-derived keys do NOT include tracing, but DO include the union.
    base_keys = {"name", "vllmCommon", "schedulerName"}
    reference = {"knob_keys": sorted(base_keys | _SOFT_OPTIONAL_KNOBS)}
    assert validate_scenario_structure(doc, reference) == []


def test_tracing_subkeys_are_not_falsely_flagged_as_unrecognized():
    """REGRESSION: the ``unrecognized_flags`` ADVISORY must NOT flag the documented, upstream-real
    ``tracing.*`` sub-knobs as fabricated/unrecognized.

    The soft-optional union only added the TOP-LEVEL name ``tracing`` to ``known_leaf_keys`` — but
    the advisory keys on each dotted key's LEAF segment, and the tracing SUB-leaves
    (``otlpEndpoint``, ``samplerArg``, ``vllmDecode`` …) appear in NO scenario example or stock
    defaults BY DESIGN (the jinja renders them behind ``is defined`` guards — exactly why the
    soft-optional knob exists). So every valid tracing.* knob was being reported as
    ``unrecognized_flags`` → the tool then attached the ``unrecognized_flags_note`` telling the
    agent to warn the user the config is likely a typo / nonexistent flag. These ARE the real
    upstream fields (config/templates/jinja/13_ms-values.yaml.j2)."""
    # A reference exactly as _scenario_reference unions it for a repo where NO example/default sets
    # any tracing sub-leaf: only the top-level ``tracing`` name is in known_leaf_keys.
    reference = {"known_leaf_keys": sorted({"vllmCommon", "flags", "enabled"} | _SOFT_OPTIONAL_KNOBS)}
    flagged = unrecognized_flags(_TRACING_OVERRIDES, reference)
    assert flagged == [], (
        "documented upstream tracing.* knobs were falsely flagged as unrecognized: " f"{flagged}"
    )


def test_unrecognized_flags_still_flags_a_genuinely_fabricated_flag():
    """The fix must NOT blanket-mute the advisory: a fabricated flag with NO soft-optional root
    (the sim-1 ``enablePrefixCachingV2`` case) is still surfaced so the agent can warn the user."""
    reference = {"known_leaf_keys": sorted({"vllmCommon", "flags", "enforceEager"} | _SOFT_OPTIONAL_KNOBS)}
    flagged = unrecognized_flags(
        {
            "vllmCommon.flags.enforceEager": True,  # corroborated → not flagged
            "vllmCommon.flags.enablePrefixCachingV2": True,  # fabricated → flagged
        },
        reference,
    )
    assert flagged == ["vllmCommon.flags.enablePrefixCachingV2"]


def test_validator_still_rejects_a_typoed_tracing_key():
    """The soft-optional union must NOT blanket-allow unknown keys: a misspelled 'tracign' is
    still refused (the typo-screen is intact for everything outside the union)."""
    doc = {"scenario": [{"name": "t", "tracign": {"enabled": True}}]}
    reference = {"knob_keys": sorted({"name", "vllmCommon"} | _SOFT_OPTIONAL_KNOBS)}
    errors = validate_scenario_structure(doc, reference)
    assert any("tracign" in e for e in errors)


def test_scenario_reference_includes_tracing_when_repo_present(tool_ctx):
    """Against the LIVE repo reference, 'tracing' must be an allowed top-level knob key even
    though it appears in NO scenario example (added by the soft-optional union)."""
    if not tool_ctx.settings.bench_repo.joinpath("config", "scenarios").is_dir():
        import pytest

        pytest.skip("bench repo scenarios not present")
    ref = _scenario_reference(tool_ctx.settings.bench_repo)
    keys = set(ref["knob_keys"])
    assert "tracing" in keys, f"tracing missing from reference knob keys: {sorted(keys)}"
    # Sanity: it really is NOT in any example (so the union is doing the work, not the examples).
    scen_dir = tool_ctx.settings.bench_repo / "config" / "scenarios"
    example_keys: set[str] = set()
    for path in scen_dir.rglob("*.yaml"):
        try:
            data = yaml.safe_load(path.read_text())
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(data, dict) and isinstance(data.get("scenario"), list):
            for row in data["scenario"]:
                if isinstance(row, dict):
                    example_keys.update(row.keys())
    assert "tracing" not in example_keys, "an example now sets tracing; soft-optional is moot"


# ---------------------------------------------------------------------------
# The tool: author + validate the tracing block (the ACCEPTANCE path)
# ---------------------------------------------------------------------------


async def test_author_tracing_block_validates_against_repo_shape(tool_ctx):
    """ACCEPTANCE: the agent authors a validated tracing: block; the file lands in the session
    workspace (never the repo) and passes structural validation against the repo example shape
    (with the soft-optional tracing knob accepted)."""
    out = await write_and_validate_config(
        tool_ctx,
        artifact_type="scenario",
        target_filename="traced.yaml",
        content={"name": "traced-baseline", **_TRACING_OVERRIDES},
    )
    assert out["artifact_type"] == "scenario"
    assert out["valid"] is True, out.get("errors")
    assert out["errors"] == []
    assert out["scenario_name"] == "traced-baseline"
    # The tracing knobs the agent set are reported back.
    assert "tracing.enabled" in out["knobs_set"]
    assert "tracing.otlpEndpoint" in out["knobs_set"]
    assert "tracing.sampling.samplerArg" in out["knobs_set"]

    # The file landed INSIDE the workspace, never the read-only repo.
    path = Path(out["path"])
    assert path.is_file()
    assert tool_ctx.workspace in path.parents
    assert tool_ctx.settings.bench_repo not in path.parents

    # The emitted YAML has the upstream-shaped nested tracing block.
    doc = yaml.safe_load(path.read_text())
    tracing = doc["scenario"][0]["tracing"]
    assert tracing["enabled"] is True
    assert tracing["otlpEndpoint"] == "http://otel-collector:4317"
    assert tracing["sampling"]["samplerArg"] == "0.1"
    assert tracing["serviceNames"]["routingProxy"] == "routing-proxy"


async def test_author_minimal_tracing_block(tool_ctx):
    """Even the minimal on-switch (just tracing.enabled + the endpoint) authors + validates."""
    out = await write_and_validate_config(
        tool_ctx,
        artifact_type="scenario",
        target_filename="traced-min.yaml",
        content={
            "name": "traced-min",
            "tracing.enabled": True,
            "tracing.otlpEndpoint": "http://otel-collector:4317",
        },
    )
    assert out["valid"] is True, out.get("errors")
    doc = yaml.safe_load(Path(out["path"]).read_text())
    assert doc["scenario"][0]["tracing"] == {
        "enabled": True,
        "otlpEndpoint": "http://otel-collector:4317",
    }


async def test_author_tracing_emits_gateable_spec_and_passes_allowlist(tool_ctx, catalog):
    """The authored tracing scenario must have a real, allowlisted route into plan/--dry-run:
    it emits a companion --spec file whose path the live allowlist accepts as a read-only
    plan --dry-run target. This is the determinism-gate plumbing for the tracing block."""
    out = await write_and_validate_config(
        tool_ctx,
        artifact_type="scenario",
        target_filename="traced-gate.yaml",
        content={"name": "traced-gate", **_TRACING_OVERRIDES},
    )
    assert out["valid"] is True, out.get("errors")
    spec_path = out["spec_path"]
    assert Path(spec_path).is_file()
    # The companion spec points the CLI at the AUTHORED scenario in the workspace.
    spec_doc = yaml.safe_load(Path(spec_path).read_text())
    assert spec_doc["scenario_file"]["path"] == out["path"]

    argv = ["llmdbenchmark", "--spec", spec_path, "plan", "-p", "test-ns", "--dry-run"]
    decision = tool_ctx.allowlist.validate(argv, catalog=catalog)
    assert decision.allowed, decision.reason
    from app.security.allowlist import READ_ONLY

    assert decision.mode == READ_ONLY  # previewing the tracing block is read-only


async def test_plan_dry_run_renders_authored_tracing_block(tool_ctx):
    """END-TO-END (recording runner, no cluster): the agent authors the tracing scenario, then
    drives the determinism gate THROUGH execute_llmdbenchmark(spec=<spec_path>, dry_run). The
    authored spec must reach the CLI argv — that is the validation gate the acceptance asks for."""
    from app.tools.run.execute import execute_llmdbenchmark
    from tests.flows.harness import CaptureRunner

    out = await write_and_validate_config(
        tool_ctx,
        artifact_type="scenario",
        target_filename="traced-e2e.yaml",
        content={"name": "traced-e2e", **_TRACING_OVERRIDES},
    )
    spec_path = out["spec_path"]
    runner = CaptureRunner(tool_ctx.settings.repo_paths)
    tool_ctx.runner = runner
    result = await execute_llmdbenchmark(
        tool_ctx, subcommand="plan", spec=spec_path,
        namespace="test-ns", flags={"dry_run": True},
    )
    assert result["exit_code"] == 0
    assert result["mode"] == "read_only"
    assert "--spec" in result["argv"]
    assert spec_path in result["argv"]
    assert "--dry-run" in result["argv"]
    assert any(spec_path in c["argv"] for c in runner.calls)


# ---------------------------------------------------------------------------
# Discoverability + the config-only limitation (the JUDGMENT half)
# ---------------------------------------------------------------------------


def test_schema_description_points_to_tracing_and_observability_knowledge():
    spec = next(d for d in tool_definitions() if d["name"] == "write_and_validate_config")
    desc = spec["input_schema"]["properties"]["content"]["description"]
    # The tracing.* family + its judgment pointer are surfaced to the agent.
    assert "tracing.enabled" in desc
    assert "tracing.otlpEndpoint" in desc
    assert "tracing.sampling.samplerArg" in desc
    assert "observability" in desc  # read_knowledge('observability') for the limitation


def test_observability_knowledge_documents_config_only_limitation():
    """The config-only limitation is JUDGMENT and must live in knowledge, not Python. Assert the
    observability knowledge file states it explicitly and documents the dotted keys + the gate."""
    from app.config import get_settings

    md = (get_settings().knowledge_dir / "observability/observability_tracing.md").read_text()
    lower = md.lower()
    # The section exists and states CONFIGURE-not-COLLECT.
    assert "tracing" in lower
    assert "config-only" in lower or "config only" in lower
    assert "configures" in lower and "collect" in lower
    # The agent cannot SHOW traces; the user views them in their own backend.
    assert "jaeger" in lower or "tempo" in lower
    # The exact dotted keys are documented.
    for key in (
        "tracing.enabled",
        "tracing.otlpEndpoint",
        "tracing.sampling.sampler",
        "tracing.sampling.samplerArg",
        "tracing.serviceNames",
        "tracing.vllm.collectDetailedTraces",
    ):
        assert key in md, f"{key} not documented in knowledge/observability_tracing.md"
    # It steers the agent through the determinism gate (plan/--dry-run with spec_path).
    assert "dry_run" in lower or "dry-run" in lower
    # And cites upstream truth (config-only is grounded, not invented).
    assert "observability.md" in md  # llm-d-benchmark/docs/observability.md
