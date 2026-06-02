"""Phase 20 — Well-lit-path advisor.

Hermetic tests for ``knowledge/welllit_path_advisor.yaml``: it parses, has an entry per
archetype with the required fields, every referenced scenario/guide/workload identifier is
well-formed and (where applicable) present in the FROZEN catalog snapshot, and the file is
loaded into the agent context (read_knowledge + inlined into the system prompt). No network,
no live cluster, no repo dependency for the identifier checks — they run against the snapshot.

The JUDGMENT (which scenario fits which workload) lives entirely in the YAML; these tests
only assert the data's shape and that it cannot drift from the catalog — there is no
scenario-selection logic in Python to test.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from app.agent.prompt import CORE_KNOWLEDGE, build_system_prompt
from app.config import get_settings
from app.tools import probe
from tests.flows.catalog_snapshot import frozen_catalog

ADVISOR_NAME = "welllit_path_advisor.yaml"

# The archetypes the ACCEPTANCE criteria require coverage of (workload shapes).
REQUIRED_ARCHETYPES = {
    "chat_prefix_heavy",      # chat / prefix-heavy
    "long_context_rag",       # long-context / RAG
    "high_throughput_batch",  # throughput / batch
    "code_completion",        # code
    "agentic",                # agentic
    "default_sanity",         # default / sanity (local)
}

# Required fields on every well-lit-path entry.
REQUIRED_FIELDS = {
    "archetype",
    "title",
    "scenario",
    "deploy_path",
    "signals",
    "rationale",
    "benchmark_workloads",
}

# The signals that must be expressed (the spec calls these out explicitly).
REQUIRED_SIGNALS = {"prefix_reuse_ratio", "context_length", "concurrency", "slo_emphasis"}

VALID_DEPLOY_PATHS = {"kind-local", "gpu-only"}

# A catalog spec identifier is one of: a bare leaf (e.g. ``cicd/kind``) — i.e. one or more
# ``/``-separated segments of [a-z0-9._-]. This is the "well-formed" check (no spaces, no
# traversal, no uppercase/garbage) applied even before checking snapshot membership.
_SPEC_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*$")
_WORKLOAD_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*\.yaml$")


def _advisor_path() -> Path:
    return get_settings().knowledge_dir / ADVISOR_NAME


def _load_advisor() -> dict:
    return yaml.safe_load(_advisor_path().read_text())


def _entries() -> list[dict]:
    return _load_advisor()["well_lit_paths"]


# ---- structure / parsing -------------------------------------------------------------


def test_advisor_file_exists_and_parses():
    path = _advisor_path()
    assert path.is_file(), f"{ADVISOR_NAME} must exist in the knowledge dir"
    data = _load_advisor()
    assert isinstance(data, dict)
    assert isinstance(data.get("well_lit_paths"), list) and data["well_lit_paths"], (
        "advisor must define a non-empty `well_lit_paths` list"
    )


def test_covers_every_required_archetype():
    archetypes = {e["archetype"] for e in _entries()}
    missing = REQUIRED_ARCHETYPES - archetypes
    assert not missing, f"advisor is missing required archetypes: {sorted(missing)}"


def test_every_entry_has_required_fields():
    for e in _entries():
        missing = REQUIRED_FIELDS - set(e)
        assert not missing, f"entry {e.get('archetype')!r} missing fields: {sorted(missing)}"
        # rationale must be substantive prose, not a placeholder.
        assert isinstance(e["rationale"], str) and len(e["rationale"].split()) >= 12, (
            f"entry {e['archetype']!r} needs a substantive rationale"
        )
        # benchmark_workloads must be a non-empty list.
        assert isinstance(e["benchmark_workloads"], list) and e["benchmark_workloads"], (
            f"entry {e['archetype']!r} needs at least one benchmark workload"
        )


def test_every_entry_expresses_the_required_signals():
    for e in _entries():
        signals = e["signals"]
        assert isinstance(signals, dict), f"entry {e['archetype']!r} signals must be a mapping"
        missing = REQUIRED_SIGNALS - set(signals)
        assert not missing, (
            f"entry {e['archetype']!r} missing selection signals: {sorted(missing)}"
        )


def test_deploy_path_values_are_valid():
    for e in _entries():
        assert e["deploy_path"] in VALID_DEPLOY_PATHS, (
            f"entry {e['archetype']!r} has unknown deploy_path {e['deploy_path']!r}"
        )


def test_archetypes_are_unique():
    archetypes = [e["archetype"] for e in _entries()]
    assert len(archetypes) == len(set(archetypes)), "archetype keys must be unique"


# ---- identifier validity + catalog-snapshot membership -------------------------------


def test_scenarios_are_well_formed_and_in_catalog_snapshot():
    """Every recommended scenario (+ also_consider) is a well-formed identifier and present
    in the frozen catalog snapshot — so the advisor cannot recommend a name the CLI would
    reject. (Guides absent from the kind catalog are marked deploy_path: gpu-only, but a
    `gpu-only` scenario is STILL a real benchmark-repo spec, so it must be in the snapshot.)
    """
    specs = set(frozen_catalog()["specs"])
    for e in _entries():
        scenario = e["scenario"]
        assert _SPEC_RE.match(scenario), f"scenario {scenario!r} is not a well-formed identifier"
        assert scenario in specs, (
            f"scenario {scenario!r} ({e['archetype']}) is not in the catalog snapshot"
        )
        for alt in e.get("also_consider", []):
            assert _SPEC_RE.match(alt), f"also_consider {alt!r} is not well-formed"
            assert alt in specs, f"also_consider {alt!r} ({e['archetype']}) not in snapshot"


def test_benchmark_workloads_are_well_formed_and_in_catalog_snapshot():
    workloads = set(frozen_catalog()["workloads"])
    for e in _entries():
        for w in e["benchmark_workloads"]:
            assert _WORKLOAD_RE.match(w), f"workload {w!r} ({e['archetype']}) is not well-formed"
            assert w in workloads, f"workload {w!r} ({e['archetype']}) not in catalog snapshot"


def test_guides_are_well_formed_llm_d_paths():
    """Where an entry names an llm-d well-lit-path `guide`, it must be a well-formed
    `llm-d/guides/<name>` identifier (deploy-path pointer; not a benchmark spec)."""
    for e in _entries():
        guide = e.get("guide")
        if guide is None:
            continue
        assert guide.startswith("llm-d/guides/"), (
            f"guide {guide!r} ({e['archetype']}) must be an llm-d/guides/<name> path"
        )
        leaf = guide.split("/")[-1]
        assert _SPEC_RE.match(leaf), f"guide leaf {leaf!r} is not well-formed"


def test_gpu_only_entries_are_marked_as_deploy_path_guidance():
    """A GPU-only well-lit path must NOT claim to be locally runnable; exactly one archetype
    (the default/sanity local one) is kind-local. This is what lets the agent honestly tell
    the user a GPU guide is deploy-path guidance, not a thing it can stand up on kind."""
    by_arch = {e["archetype"]: e for e in _entries()}
    assert by_arch["default_sanity"]["deploy_path"] == "kind-local"
    assert by_arch["default_sanity"]["scenario"] == "cicd/kind"
    # The GPU well-lit paths are gpu-only.
    for arch in ("chat_prefix_heavy", "long_context_rag", "high_throughput_batch"):
        assert by_arch[arch]["deploy_path"] == "gpu-only"


# ---- loader / agent-context wiring ---------------------------------------------------


def test_loader_serves_the_advisor_via_read_knowledge(tool_ctx):
    out = probe.read_knowledge(tool_ctx, name="welllit_path_advisor")
    assert "error" not in out
    assert out["name"] == ADVISOR_NAME
    assert "well_lit_paths" in out["content"]


def test_advisor_is_inlined_into_the_system_prompt(tool_ctx):
    # The advisor is a CORE knowledge file -> inlined verbatim into every system prompt.
    assert ADVISOR_NAME in CORE_KNOWLEDGE
    prompt = build_system_prompt(tool_ctx)
    assert f"# Knowledge: {ADVISOR_NAME}" in prompt
    body = (tool_ctx.settings.knowledge_dir / ADVISOR_NAME).read_text()
    # A representative slice of the advisor body must be present verbatim.
    assert "well_lit_paths" in prompt
    assert "guides/precise-prefix-cache-routing" in prompt
    # Sanity: the inlined section carries the real file content.
    assert body.splitlines()[0].strip() in prompt


def test_deploy_path_playbook_references_the_advisor():
    playbook = (get_settings().knowledge_dir / "deploy_path_playbook.md").read_text()
    assert "welllit_path_advisor" in playbook, (
        "deploy_path_playbook.md must reference the advisor (per the Phase 20 spec)"
    )


# ---- the live catalog must not drift from the snapshot (when the repo IS present) ----


def test_advisor_identifiers_match_live_catalog_when_present(tool_ctx):
    """If the real bench repo is on disk, every advisor scenario must ALSO exist in the LIVE
    catalog — so the snapshot-based checks above can't pass on stale data. Skips hermetically
    when the repo is absent (CI with empty gitlinks), exactly like the flow snapshot guard."""
    if not tool_ctx.settings.bench_repo.is_dir():
        pytest.skip("bench repo not present — snapshot checks already cover identifier validity")
    live_specs = set(tool_ctx.catalog(refresh=True).get("specs", []))
    if not live_specs:
        pytest.skip("live catalog empty (repo present but unpopulated)")
    for e in _entries():
        assert e["scenario"] in live_specs, (
            f"advisor scenario {e['scenario']!r} ({e['archetype']}) is NOT in the LIVE catalog "
            f"— the advisor has drifted from the repo"
        )
        for alt in e.get("also_consider", []):
            assert alt in live_specs, f"also_consider {alt!r} not in LIVE catalog"
