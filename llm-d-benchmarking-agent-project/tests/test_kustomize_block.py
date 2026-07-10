"""Phase 46 — Kustomize deploy config block (``kustomize.*``).

Hermetic, no cluster / GPU / network. Covers the phase's ACCEPTANCE:

  * the agent can AUTHOR a kustomize-method scenario (a ``kustomize.*`` block with a guide +
    patches) via ``write_and_validate_config(artifact_type="scenario", …)`` using DOTTED
    ``kustomize.*`` keys, it deep-merges into the upstream block shape, SHAPE-validates against
    the repo's own scenario examples (``kustomize`` IS a real top-level scenario knob key), lands
    in the SESSION workspace (never the read-only repo), and has a real route into the
    determinism gate via the companion ``*.spec.yaml`` (plan/--dry-run) — the determinism gate;
  * ``-t kustomize`` stays ALLOWLISTED (the bare method was already permitted — Phase 46 must not
    regress it);
  * ``--llmd-repo-path`` is now a KNOWN, path-constrained standup flag, threaded by
    ``build_argv`` from ``flags["repo_path"]`` — pointing the kustomize method at a local llm-d
    clone — and the path value is pinned (no ``..`` traversal, no shell metacharacters);
  * the which-guide/overlay/patches JUDGMENT lives in knowledge/deploy_path_playbook.md.

The repos stay read-only; the only on-disk dependency is the read-only sibling bench repo, and
the structural validator degrades gracefully when it is absent.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.security.allowlist import MUTATING, READ_ONLY
from app.tools.setup.config_artifact import (
    _build_scenario_document,
    _scenario_reference,
    validate_scenario_structure,
    write_and_validate_config,
)
from app.tools.run.execute import build_argv
from app.tools.schemas import ExecuteInput

KNOWLEDGE_DIR = Path(__file__).resolve().parents[1] / "knowledge"

# A representative kustomize block: a guide + a strategic-merge patch (the ACCEPTANCE shape).
KUSTOMIZE_CONTENT = {
    "name": "ob-kustomize",
    "kustomize.enabled": True,
    "kustomize.guideName": "optimized-baseline",
    "kustomize.repoRef": "main",
    "kustomize.patches": [
        {"patch": "apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: decode}\nspec: {replicas: 4}\n"}
    ],
}


# ---------------------------------------------------------------------------
# Authoring: dotted kustomize.* overrides deep-merge into the upstream block shape
# ---------------------------------------------------------------------------


def test_dotted_kustomize_keys_build_the_nested_block():
    doc = _build_scenario_document("ob-kustomize", KUSTOMIZE_CONTENT)
    item = doc["scenario"][0]
    assert item["name"] == "ob-kustomize"
    block = item["kustomize"]
    # The dotted paths became a real nested `kustomize:` mapping (not literal dotted keys).
    assert block["enabled"] is True
    assert block["guideName"] == "optimized-baseline"
    assert block["repoRef"] == "main"
    # patches is preserved as a LIST of {patch: ...} mappings (the upstream shape).
    assert isinstance(block["patches"], list)
    assert block["patches"][0]["patch"].startswith("apiVersion: apps/v1")
    # No literal dotted key leaked into the item.
    assert not any("." in k for k in item)
    assert not any("." in k for k in block)


def test_kustomize_is_a_real_top_level_scenario_knob_key(tool_ctx):
    """`kustomize` must be a knob key the repo's OWN scenario examples use, so the dotted
    `kustomize.*` overrides pass SHAPE validation (no config_artifact change was needed)."""
    if not tool_ctx.settings.bench_repo.joinpath("config", "scenarios").is_dir():
        pytest.skip("bench repo scenarios not present")
    ref = _scenario_reference(tool_ctx.settings.bench_repo)
    assert "kustomize" in ref["knob_keys"], (
        f"kustomize missing from live repo scenario knob keys: {sorted(ref['knob_keys'])}"
    )


def test_authored_kustomize_block_validates_against_example_shape():
    """The authored kustomize block validates against the repo's example shape (a non-empty
    reference that includes `kustomize`)."""
    doc = _build_scenario_document("ob-kustomize", KUSTOMIZE_CONTENT)
    reference = {"knob_keys": ["name", "kustomize", "model", "modelservice", "decode"]}
    assert validate_scenario_structure(doc, reference) == []


# ---------------------------------------------------------------------------
# The tool: author into the workspace + validate (the ACCEPTANCE path)
# ---------------------------------------------------------------------------


async def test_author_kustomize_scenario_writes_validated_file_into_workspace(tool_ctx):
    """ACCEPTANCE: author a kustomize-method scenario (guide + patches); it lands in the session
    workspace (never the repo), validates against the repo example shape, and reports the knobs."""
    out = await write_and_validate_config(
        tool_ctx,
        artifact_type="scenario",
        target_filename="kustomize-ob.yaml",
        content=KUSTOMIZE_CONTENT,
    )
    assert out["artifact_type"] == "scenario"
    assert out["valid"] is True
    assert out["errors"] == []
    assert out["scenario_name"] == "ob-kustomize"
    assert "kustomize.guideName" in out["knobs_set"]
    assert "kustomize.patches" in out["knobs_set"]

    path = Path(out["path"])
    assert path.is_file()
    assert tool_ctx.workspace in path.parents
    assert tool_ctx.settings.bench_repo not in path.parents  # NOT written into the read-only repo

    # The emitted YAML parses and carries the kustomize block with its patches.
    doc = yaml.safe_load(path.read_text())
    block = doc["scenario"][0]["kustomize"]
    assert block["enabled"] is True
    assert block["guideName"] == "optimized-baseline"
    assert block["patches"][0]["patch"].startswith("apiVersion: apps/v1")

    # A companion *.spec.yaml was authored beside it — the route into plan/--dry-run.
    spec_path = Path(out["spec_path"])
    assert spec_path.is_file()
    assert spec_path.name == "kustomize-ob.spec.yaml"
    assert tool_ctx.workspace in spec_path.parents
    assert "dry_run" in out["note"]


async def test_author_kustomize_validates_against_real_repo_examples(tool_ctx):
    """Against the LIVE repo, the kustomize block validates and reports the example files used."""
    if not tool_ctx.settings.bench_repo.joinpath("config", "scenarios").is_dir():
        pytest.skip("bench repo scenarios not present")
    out = await write_and_validate_config(
        tool_ctx,
        artifact_type="scenario",
        target_filename="kustomize-live.yaml",
        content=KUSTOMIZE_CONTENT,
    )
    assert out["valid"] is True
    assert out["validated_against_examples"]


async def test_authored_kustomize_spec_passes_plan_dry_run_allowlist(tool_ctx, catalog):
    """The companion spec path must be an ACCEPTED `--spec` value so a kustomize-method scenario
    can be GATED through plan/--dry-run — the determinism gate — without a live cluster."""
    out = await write_and_validate_config(
        tool_ctx,
        artifact_type="scenario",
        target_filename="kustomize-gated.yaml",
        content=KUSTOMIZE_CONTENT,
    )
    spec_path = out["spec_path"]
    argv = ["llmdbenchmark", "--spec", spec_path, "plan", "-p", "test-ns", "--dry-run"]
    decision = tool_ctx.allowlist.validate(argv, catalog=catalog)
    assert decision.allowed, decision.reason
    assert decision.mode == READ_ONLY  # --dry-run is a read_only_trigger


# ---------------------------------------------------------------------------
# build_argv — kustomize method + --llmd-repo-path emission (PURE MECHANISM)
# ---------------------------------------------------------------------------


def test_repo_path_emits_llmd_repo_path_flag():
    argv = build_argv("standup", spec="guides/optimized-baseline",
                       flags={"methods": "kustomize", "repo_path": "/home/me/llm-d"})
    assert "-t" in argv and argv[argv.index("-t") + 1] == "kustomize"
    assert "--llmd-repo-path" in argv
    assert argv[argv.index("--llmd-repo-path") + 1] == "/home/me/llm-d"


def test_repo_path_unset_emits_no_flag():
    # No repo_path => nothing emitted (upstream clones llm-d into workspace/).
    argv = build_argv("standup", spec="guides/optimized-baseline", flags={"methods": "kustomize"})
    assert "--llmd-repo-path" not in argv
    # And a bare kustomize standup with no flags at all emits no repo-path flag either.
    assert "--llmd-repo-path" not in build_argv("standup", spec="guides/optimized-baseline")


def test_repo_path_field_accepted_in_flags_schema():
    # repo_path rides in the free-form flags dict (like methods/output), not a new top-level field.
    m = ExecuteInput(subcommand="standup", spec="guides/optimized-baseline",
                     flags={"methods": "kustomize", "repo_path": "/home/me/llm-d"})
    assert m.flags is not None and m.flags["repo_path"] == "/home/me/llm-d"


# ---------------------------------------------------------------------------
# allowlist — -t kustomize STAYS allowlisted; --llmd-repo-path is known + value-pinned
# ---------------------------------------------------------------------------


def _argv(subcommand, *rest):
    return ["llmdbenchmark", "--spec", "guides/optimized-baseline", subcommand, *rest]


def test_kustomize_method_stays_allowlisted(allowlist, catalog):
    # The phase must NOT regress the bare method: `-t kustomize` is permitted on standup, and
    # standup stays mutating with it (a --dry-run still downgrades to a read-only preview).
    d = allowlist.validate(_argv("standup", "-t", "kustomize"), catalog=catalog)
    assert d.allowed, d.reason
    assert d.mode == MUTATING
    assert allowlist.validate(
        _argv("standup", "-t", "kustomize", "--dry-run"), catalog=catalog
    ).mode == READ_ONLY


def test_llmd_repo_path_is_allowlisted_and_value_pinned(allowlist, catalog):
    # The known flag is permitted with a local path, and its value is pinned: a '..' traversal
    # and a shell-metachar injection are BOTH refused (the regex + the metacharacter screen).
    ok = allowlist.validate(
        _argv("standup", "-t", "kustomize", "--llmd-repo-path", "/home/me/llm-d"), catalog=catalog
    )
    assert ok.allowed, ok.reason
    assert not allowlist.validate(
        _argv("standup", "--llmd-repo-path", "../../etc/passwd"), catalog=catalog
    ).allowed
    assert not allowlist.validate(
        _argv("standup", "--llmd-repo-path", "/x; rm -rf /"), catalog=catalog
    ).allowed


def test_repo_path_value_constraint_present():
    """The DATA constraint exists and forbids '..' traversal (so a widening is a reviewed YAML
    edit, never a Python change)."""
    import re

    from app.security.allowlist import Allowlist
    al = Allowlist.from_file(Path(__file__).resolve().parents[1] / "security" / "allowlist.yaml")
    constraints = al._value_constraints
    assert "repo_path" in constraints
    rx = constraints["repo_path"]["regex"]
    assert re.match(rx, "/home/me/llm-d")
    assert not re.match(rx, "../escape")


# ---------------------------------------------------------------------------
# knowledge — the which-guide/overlay/patches JUDGMENT is a knowledge file, not Python
# ---------------------------------------------------------------------------


def test_deploy_path_playbook_documents_the_kustomize_block():
    guide = KNOWLEDGE_DIR / "deploy/deploy_path_playbook.md"
    assert guide.is_file()
    text = guide.read_text()
    # The block's knob family and the --llmd-repo-path thread are documented as judgment.
    assert "kustomize.enabled" in text
    assert "kustomize.guideName" in text
    assert "kustomize.patches" in text
    assert "--llmd-repo-path" in text
    # And it grounds the choice in the upstream authoritative doc.
    assert "docs/kustomize.md" in text


def test_schema_steers_to_kustomize_authoring_and_knowledge():
    from app.tools.registry import tool_definitions

    spec = next(d for d in tool_definitions() if d["name"] == "write_and_validate_config")
    desc = spec["description"]
    schema_desc = spec["input_schema"]["properties"]["content"]["description"]
    # The content knob list now includes the kustomize.* family and points at the playbook.
    assert "kustomize.guideName" in schema_desc
    assert "deploy_path_playbook" in schema_desc or "deploy_path_playbook" in desc
