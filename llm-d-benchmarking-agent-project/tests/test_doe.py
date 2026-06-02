"""Hermetic tests for the DoE experiment-file generator (Phase 19).

Cover the pure cross-product mechanism (``app/validation/doe.py``) and the
``generate_doe_experiment`` tool (``app/tools/doe.py``): a 2-factor × (3,2)-level sweep
yields 6 correctly-named treatments; an empty/invalid factor set is rejected; the emitted
YAML parses and matches the repo's experiment structure. No network, no cluster — the only
on-disk dependency is the read-only sibling repo's experiment EXAMPLES (used as the
structural reference), and the structural validator degrades gracefully if absent.
"""
from __future__ import annotations

import yaml

from app.tools.doe import generate_doe_experiment, validate_structure
from app.tools.registry import dispatch, tool_definitions
from app.validation.doe import DoEError, build_doe_experiment

# ---------------------------------------------------------------------------
# Pure mechanism: build_doe_experiment (cross-product)
# ---------------------------------------------------------------------------


def test_cross_product_two_factors_three_by_two_is_six():
    """The acceptance case: 2 factors with 3 and 2 levels → 6 run treatments, full
    cross-product, each with the right fields."""
    result = build_doe_experiment(
        name="grid",
        run_factors=[
            {"name": "grp", "key": "data.shared_prefix.num_groups", "levels": [20, 40, 60]},
            {"name": "splen", "key": "data.shared_prefix.system_prompt_len", "levels": [1000, 5000]},
        ],
    )
    rows = result.run_treatments
    assert len(rows) == 6
    assert result.total_matrix == 6  # no setup factors → max(0,1) × 6

    # Every treatment carries a name + both dotted override keys with one chosen level each.
    for row in rows:
        assert isinstance(row["name"], str) and row["name"]
        assert set(row) == {"name", "data.shared_prefix.num_groups", "data.shared_prefix.system_prompt_len"}
        assert row["data.shared_prefix.num_groups"] in (20, 40, 60)
        assert row["data.shared_prefix.system_prompt_len"] in (1000, 5000)

    # The 6 (group, splen) value pairs are exactly the full cross-product (no dups, no gaps).
    pairs = {
        (r["data.shared_prefix.num_groups"], r["data.shared_prefix.system_prompt_len"]) for r in rows
    }
    assert pairs == {(g, s) for g in (20, 40, 60) for s in (1000, 5000)}

    # Treatment names are unique and deterministic.
    names = [r["name"] for r in rows]
    assert len(set(names)) == 6
    assert "grp20-splen1000" in names and "grp60-splen5000" in names


def test_setup_times_run_matrix_and_constants_merge():
    """setup × run matrix sizing + constants are surfaced in their parser/example-compatible
    locations: setup constants as a parser-consumed ``setup.constants`` mapping, run constants
    as the informational ``design.run.constants`` list (NEVER a top-level ``run`` key)."""
    result = build_doe_experiment(
        name="pd",
        setup_factors=[{"name": "dec", "key": "decode.replicas", "levels": [1, 2]}],
        run_factors=[{"name": "rate", "key": "rate", "levels": [10, 50, 100]}],
        setup_constants={"model.maxModelLen": 16000},
        run_constants={"data.shared_prefix.output_len": 256},
    )
    assert len(result.setup_treatments) == 2
    assert len(result.run_treatments) == 3
    assert result.total_matrix == 6  # 2 setup × 3 run

    doc = result.document
    # Setup constants stay a dotted-key mapping under `setup.constants` (the parser merges
    # them into every setup treatment's overrides).
    assert doc["setup"]["constants"] == {"model.maxModelLen": 16000}
    assert [t["name"] for t in doc["setup"]["treatments"]] == ["dec1", "dec2"]
    # Run constants live under `design.run.constants` as a list-of-{key,value}, exactly as the
    # repo's optimized-baseline.yaml / pd-disaggregation.yaml examples do. There must be NO
    # top-level `run` key — that is not part of the upstream format (regression guard).
    assert "run" not in doc
    assert doc["design"]["run"]["constants"] == [
        {"key": "data.shared_prefix.output_len", "value": 256}
    ]
    # Run treatments live under the canonical `treatments` key.
    assert len(doc["treatments"]) == 3
    # The document must use ONLY top-level keys the repo's own examples use.
    assert set(doc) <= {"design", "experiment", "setup", "treatments"}


def test_run_constants_document_passes_real_top_key_reference():
    """REGRESSION (Phase 19 review): a document carrying run constants must validate cleanly
    against a NON-EMPTY structural reference whose top_keys are the repo's real union
    ({design, experiment, setup, treatments}). This is the hermetic guard the prior suite
    lacked — with an empty reference the top-key check is skipped, masking a top-level `run`
    key leak. Here we feed the real reference, so any stray top-level key would be caught."""
    result = build_doe_experiment(
        name="rc",
        run_factors=[{"name": "c", "key": "max-concurrency", "levels": [8, 16]}],
        run_constants={"random-input-len": 10000, "random-output-len": 1000},
    )
    real_reference = {"top_keys": ["design", "experiment", "setup", "treatments"]}
    assert validate_structure(result.document, real_reference) == []
    # And explicitly: no forbidden top-level `run` key slipped in.
    assert "run" not in result.document


def test_metadata_recorded():
    result = build_doe_experiment(
        name="my-exp",
        run_factors=[{"name": "c", "key": "max-concurrency", "levels": [8, 16]}],
        harness="vllm-benchmark",
        profile="random_concurrent.yaml",
        description="load sweep",
    )
    meta = result.document["experiment"]
    assert meta["name"] == "my-exp"
    assert meta["harness"] == "vllm-benchmark"
    assert meta["profile"] == "random_concurrent.yaml"
    assert meta["description"] == "load sweep"


def test_duplicate_levels_are_deduped():
    """A repeated level must not produce a duplicate treatment (deduped on content)."""
    result = build_doe_experiment(
        name="dup",
        run_factors=[{"name": "c", "key": "rate", "levels": [10, 10, 20]}],
    )
    # 3 levels, but two are identical → 2 distinct treatments.
    assert len(result.run_treatments) == 2
    assert sorted(r["rate"] for r in result.run_treatments) == [10, 20]


def test_single_factor_run_only():
    result = build_doe_experiment(
        name="load",
        run_factors=[{"name": "c", "key": "max-concurrency", "levels": [8, 16, 32, 64]}],
    )
    assert "setup" not in result.document
    assert len(result.run_treatments) == 4
    assert result.total_matrix == 4


# ---------------------------------------------------------------------------
# Rejection of empty / invalid factor sets (mechanism boundary)
# ---------------------------------------------------------------------------


def test_no_run_factors_rejected():
    try:
        build_doe_experiment(name="empty", run_factors=[])
    except DoEError as exc:
        assert "RUN factor" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("empty run_factors should be rejected")


def test_empty_levels_rejected():
    try:
        build_doe_experiment(name="x", run_factors=[{"name": "c", "key": "a.b", "levels": []}])
    except DoEError as exc:
        assert "levels" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("empty levels should be rejected")


def test_bad_factor_name_rejected():
    try:
        build_doe_experiment(name="x", run_factors=[{"name": "bad name", "key": "a.b", "levels": [1]}])
    except DoEError as exc:
        assert "name" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("bad factor name should be rejected")


def test_bad_override_key_rejected():
    try:
        build_doe_experiment(name="x", run_factors=[{"name": "c", "key": "not a key!", "levels": [1]}])
    except DoEError as exc:
        assert "key" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("bad override key should be rejected")


def test_nested_level_rejected():
    try:
        build_doe_experiment(name="x", run_factors=[{"name": "c", "key": "a.b", "levels": [{"nested": 1}]}])
    except DoEError as exc:
        assert "scalar" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("nested level value should be rejected")


def test_duplicate_factor_key_rejected():
    try:
        build_doe_experiment(
            name="x",
            run_factors=[
                {"name": "a", "key": "rate", "levels": [1]},
                {"name": "b", "key": "rate", "levels": [2]},
            ],
        )
    except DoEError as exc:
        assert "duplicate" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("duplicate factor key should be rejected")


def test_bad_experiment_name_rejected():
    try:
        build_doe_experiment(name="bad/name", run_factors=[{"name": "c", "key": "a.b", "levels": [1]}])
    except DoEError as exc:
        assert "name" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("bad experiment name should be rejected")


# ---------------------------------------------------------------------------
# Structural validator (against the repo's experiment format)
# ---------------------------------------------------------------------------


def test_validate_structure_accepts_generated_document():
    result = build_doe_experiment(
        name="ok",
        setup_factors=[{"name": "tp", "key": "decode.parallelism.tensor", "levels": [1, 2]}],
        run_factors=[{"name": "g", "key": "data.num_groups", "levels": [40, 60]}],
    )
    # No reference (empty) → only the intrinsic shape contract is enforced.
    assert validate_structure(result.document, {}) == []


def test_validate_structure_rejects_missing_treatments():
    errors = validate_structure({"experiment": {"name": "x"}}, {})
    assert any("treatments" in e for e in errors)


def test_validate_structure_rejects_unnamed_treatment():
    doc = {"experiment": {"name": "x"}, "treatments": [{"rate": 10}]}
    errors = validate_structure(doc, {})
    assert any("name" in e for e in errors)


def test_validate_structure_rejects_treatment_without_overrides():
    doc = {"experiment": {"name": "x"}, "treatments": [{"name": "t1"}]}
    errors = validate_structure(doc, {})
    assert any("override" in e for e in errors)


def test_validate_structure_rejects_unknown_top_key_against_reference():
    # Use the repo's REAL top-key union (no `run` key — that is exactly why a top-level
    # `run:` would be rejected, see the run-constants regression tests above).
    doc = {"experiment": {"name": "x"}, "treatments": [{"name": "t1", "rate": 1}], "bogus": 1}
    reference = {"top_keys": ["design", "experiment", "setup", "treatments"]}
    errors = validate_structure(doc, reference)
    assert any("bogus" in e for e in errors)


def test_validate_structure_rejects_top_level_run_against_real_reference():
    """A bare top-level `run` key is NOT part of the upstream format and must be rejected
    against the real reference — this is the exact shape the old run-constants emission
    produced, which broke the tool against the populated repo."""
    doc = {
        "experiment": {"name": "x"},
        "treatments": [{"name": "t1", "rate": 1}],
        "run": {"constants": {"a": 1}},
    }
    reference = {"top_keys": ["design", "experiment", "setup", "treatments"]}
    errors = validate_structure(doc, reference)
    assert any("'run'" in e for e in errors)


# ---------------------------------------------------------------------------
# Tool: generate_doe_experiment (write into workspace, validate, return)
# ---------------------------------------------------------------------------


async def test_tool_writes_valid_experiment_yaml(tool_ctx):
    out = await generate_doe_experiment(
        tool_ctx,
        name="ratio-sweep",
        run_factors=[
            {"name": "grp", "key": "data.shared_prefix.num_groups", "levels": [40, 60]},
            {"name": "splen", "key": "data.shared_prefix.system_prompt_len", "levels": [1000, 5000, 8000]},
        ],
        setup_factors=[{"name": "dec", "key": "decode.replicas", "levels": [1, 2]}],
        harness="inference-perf",
    )
    assert out["generated"] is True
    assert out["n_run_treatments"] == 6 and out["n_setup_treatments"] == 2
    assert out["total_matrix"] == 12
    assert out["valid"] is True

    # The file landed inside the SESSION workspace (never the repos).
    from pathlib import Path

    path = Path(out["path"])
    assert path.is_file()
    assert tool_ctx.workspace in path.parents

    # The emitted YAML parses and matches the structure of the repo's own experiment files.
    doc = yaml.safe_load(path.read_text())
    assert doc["experiment"]["name"] == "ratio-sweep"
    assert doc["experiment"]["harness"] == "inference-perf"
    assert len(doc["setup"]["treatments"]) == 2
    assert len(doc["treatments"]) == 6
    for row in doc["treatments"]:
        assert "name" in row and len(row) >= 2


async def test_tool_validates_against_real_repo_examples(tool_ctx):
    """When the bench repo is present, the tool reports which example files it validated
    the generated structure against (read live, never vendored)."""
    if not tool_ctx.settings.bench_repo.is_dir():
        import pytest

        pytest.skip("bench repo not present")
    out = await generate_doe_experiment(
        tool_ctx,
        name="against-examples",
        run_factors=[{"name": "c", "key": "max-concurrency", "levels": [8, 16]}],
    )
    assert out["generated"] is True
    # At least one example experiment file in the repo was used as the structural reference.
    assert out["validated_against_examples"]
    assert all(e.endswith((".yaml", ".yml")) for e in out["validated_against_examples"])


async def test_tool_writes_run_constants_against_real_repo(tool_ctx):
    """REGRESSION (Phase 19 review): the documented ``run_constants`` arg must NOT make the
    tool reject its own output when the REAL populated bench repo is the structural reference.

    The old emission wrote a top-level ``run:`` key, which ``validate_structure`` rejects
    because the repo's examples never use it (top_keys = {design, experiment, setup,
    treatments}). With the populated repo present this previously returned generated=False and
    wrote NO file. It must now succeed and place run constants under ``design.run.constants``.
    """
    if not tool_ctx.settings.bench_repo.joinpath("workload", "experiments").is_dir():
        import pytest

        pytest.skip("bench repo experiment examples not present")

    out = await generate_doe_experiment(
        tool_ctx,
        name="rc-real",
        run_factors=[{"name": "c", "key": "max-concurrency", "levels": [8, 16]}],
        run_constants={"random-input-len": 10000, "random-output-len": 1000},
        harness="vllm-benchmark",
    )
    # Must succeed against the populated repo — this is the production/main integration path.
    assert out["generated"] is True, out
    assert out["valid"] is True
    # It validated against the real example files (non-empty reference exercised the top-key
    # check that the old `run:` key would have failed).
    assert out["validated_against_examples"]

    from pathlib import Path

    doc = yaml.safe_load(Path(out["path"]).read_text())
    assert "run" not in doc  # no forbidden top-level `run` key
    assert doc["design"]["run"]["constants"] == [
        {"key": "random-input-len", "value": 10000},
        {"key": "random-output-len", "value": 1000},
    ]
    assert set(doc) <= {"design", "experiment", "setup", "treatments"}


async def test_tool_rejects_empty_factor_set(tool_ctx):
    out = await generate_doe_experiment(tool_ctx, name="empty", run_factors=[])
    assert out["generated"] is False
    assert "RUN factor" in out["reason"]
    # No file was written for the rejected request.
    assert not list(tool_ctx.workspace.glob("*.yaml")) if tool_ctx.workspace.is_dir() else True


async def test_tool_rejects_invalid_factor(tool_ctx):
    out = await generate_doe_experiment(
        tool_ctx, name="bad", run_factors=[{"name": "c", "key": "a.b", "levels": []}]
    )
    assert out["generated"] is False
    assert "levels" in out["reason"]


async def test_tool_rejects_path_traversal_filename(tool_ctx):
    from app.tools.context import ToolError

    raised = False
    try:
        await generate_doe_experiment(
            tool_ctx,
            name="x",
            run_factors=[{"name": "c", "key": "a.b", "levels": [1, 2]}],
            target_filename="../escape.yaml",
        )
    except ToolError:
        raised = True
    assert raised


# ---------------------------------------------------------------------------
# Registry wiring + dispatch (the LLM-facing surface)
# ---------------------------------------------------------------------------


def test_tool_registered_and_exported():
    names = {d["name"] for d in tool_definitions()}
    assert "generate_doe_experiment" in names
    spec = next(d for d in tool_definitions() if d["name"] == "generate_doe_experiment")
    assert spec["description"]
    assert spec["input_schema"]["type"] == "object"
    # The required factor axis is declared in the schema.
    assert "run_factors" in spec["input_schema"]["properties"]


async def test_dispatch_validates_factor_schema(tool_ctx):
    # Missing the required run_factors → schema rejection before the handler runs.
    result = await dispatch(tool_ctx, "generate_doe_experiment", {"name": "x"})
    assert result.get("error") == "invalid arguments"


async def test_dispatch_end_to_end(tool_ctx):
    result = await dispatch(
        tool_ctx,
        "generate_doe_experiment",
        {
            "name": "e2e",
            "run_factors": [{"name": "c", "key": "max-concurrency", "levels": [8, 16, 32]}],
        },
    )
    assert result["generated"] is True
    assert result["n_run_treatments"] == 3
