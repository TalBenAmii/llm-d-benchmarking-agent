"""write_and_validate_config — materialize a generated config artifact into the session
workspace and validate it before any execution.

Two artifact modes:

* ``workload`` / ``run_config`` (MVP) — the quickstart uses stock profiles, so this is
  intentionally minimal: it writes the artifact into the workspace (never into the repos)
  and does a structural YAML check.

* ``scenario`` (Phase 45) — AUTHOR finer per-knob vLLM/scheduling/storage scenario edits
  beyond the parallelism/memory knobs that capacity + DoE already cover. The agent supplies
  ``content`` as a set of per-knob OVERRIDES keyed by the upstream scenario field paths
  (dotted, e.g. ``vllmCommon.flags.enforceEager``, ``vllmCommon.kvTransfer.enabled``,
  ``schedulerName``, ``routing.servicePort``, ``decode.schedulerName``). Python only
  MECHANISES the deep-merge of those overrides onto a minimal ``scenario: [ {name, ...} ]``
  skeleton, validates the knob SHAPE against the repo's own scenario examples (read LIVE so
  it can't drift), and writes into ``ctx.workspace`` only — never the read-only spec.
  WHICH knobs to set is JUDGMENT and lives in ``knowledge/vllm_overrides.md`` — there is no
  knob-selection logic in this Python. The ``tracing.*`` family (OpenTelemetry tracing config;
  Phase 54) is also a supported dotted override — the benchmark CONFIGURES tracing on the
  modelservice pods, it never COLLECTS traces; see ``knowledge/observability.md``.

  Alongside the scenario, it ALSO authors a companion ``--spec`` SPECIFICATION file into the
  same workspace (``<name>.spec.yaml``) whose ``scenario_file.path`` points at the authored
  scenario and whose ``values_file``/``template_dir`` point at the read-only repo's stock
  ``defaults.yaml`` + ``jinja`` dir. THAT is the concrete plumbing into the determinism gate:
  the returned ``spec_path`` is fed straight to ``execute_llmdbenchmark(spec=<spec_path>, …)``
  — the CLI's ``resolve_specification_file`` accepts an exact file path, and the allowlist
  permits a workspace-confined ``*.spec.yaml`` (constraint ``spec_workspace_path``), so the
  just-authored scenario has a real, allowlisted route into plan/--dry-run.

The authored scenario is then previewed via the CLI's own determinism gate using that spec:
``execute_llmdbenchmark(subcommand="plan"/"run", spec=<spec_path>, flags.dry_run=True)``.
"""
from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

import yaml

from app.tools.context import ToolContext, ToolError

_ARTIFACT_TYPES = {"workload", "run_config", "scenario"}

# Where the repo keeps its scenario EXAMPLE files. Each file is a mapping with a top-level
# ``scenario`` key whose value is a non-empty list of name-bearing knob mappings. We read
# these LIVE to derive the structural reference rather than vendoring a copy of the format.
_SCENARIOS_SUBDIR = ("config", "scenarios")

# The repo's stock VALUES file and TEMPLATE dir — the other two halves the CLI's
# ``--spec`` needs alongside our authored ``scenario_file``. We point the companion spec at
# the read-only repo's copies (never vendored) so plan/--dry-run renders exactly as upstream
# would, with only the scenario swapped for the authored one.
_VALUES_FILE_SUBPATH = ("config", "templates", "values", "defaults.yaml")
_TEMPLATE_DIR_SUBPATH = ("config", "templates", "jinja")

# Suffix for the companion specification file authored beside a scenario. A bare ``*.spec.yaml``
# under the session workspace is what the allowlist's ``spec_workspace_path`` constraint admits
# as a ``--spec`` value (in addition to live-catalog names), so this file is the allowlisted
# route the authored scenario takes into the CLI's determinism gate.
_SPEC_SUFFIX = ".spec.yaml"

# Top-level scenario-item keys the upstream modelservice jinja renders behind a
# ``{% if <key> is defined %}`` guard but the repo's scenario EXAMPLE files NEVER set, so
# ``_scenario_reference`` cannot discover them from the examples. They are nonetheless VALID
# scenario knobs: ``config/templates/jinja/13_ms-values.yaml.j2`` renders ``tracing`` (under
# ``{% if tracing is defined and tracing.enabled is defined %}``), and
# ``render_plans.py`` deep_merge(defaults, scenario_item)s a scenario item onto
# ``defaults.yaml``, so a top-level ``tracing`` key on a scenario item merges through even
# though no example/default sets it. We union these into the reference's ``knob_keys`` so the
# validator ACCEPTS them — without weakening the typo-screen for every other key. These are
# soft-optional because the examples omit them; do NOT special-case any value here (the
# generic dotted deep-merge in ``_build_scenario_document`` already authors ``tracing.*``).
_SOFT_OPTIONAL_KNOBS = {"tracing"}

# Intrinsic shape contract used when the repo / its scenario examples are absent (so the
# tool degrades gracefully to the format invariant rather than failing). A scenario item is
# a mapping carrying a non-empty string ``name`` plus >=1 override knob.


def _scenario_reference(bench_repo: Path) -> dict[str, Any]:
    """Read the repo's scenario EXAMPLE files at runtime and derive the structural contract
    an authored scenario must satisfy: the union of top-level scenario-item knob keys the
    repo's own examples actually use, plus the example file names (for provenance). Reads
    repo truth; never vendors a copy. Returns ``{}`` (and the caller falls back to the
    intrinsic shape contract) when the repo / its scenario examples are absent.

    The repo's scenario examples are STATIC between runs, but the underlying rglob+parse runs on
    every authored scenario, so we memoize on the scenarios-dir path string. Both fields are sorted
    lists; the two callers treat the result READ-ONLY (validate_scenario_structure copies knob_keys
    into a fresh set; author_scenario only reads examples), so sharing one cached dict is safe."""
    return _scenario_reference_cached(str(bench_repo.joinpath(*_SCENARIOS_SUBDIR)))


@cache
def _scenario_reference_cached(scenarios_dir: str) -> dict[str, Any]:
    """Memoized core of ``_scenario_reference``, keyed by the scenarios-dir path string. See the
    wrapper's docstring for the read-only-sharing contract that makes caching the dict safe."""
    scen_dir = Path(scenarios_dir)
    if not scen_dir.is_dir():
        return {}
    knob_keys: set[str] = set()
    examples: list[str] = []
    for path in sorted(scen_dir.rglob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text())
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(data, dict):
            continue
        rows = data.get("scenario")
        if not isinstance(rows, list):
            continue
        named_row = False
        for row in rows:
            if isinstance(row, dict) and isinstance(row.get("name"), str):
                knob_keys.update(row.keys())
                named_row = True
        if named_row:
            examples.append(str(path.relative_to(scen_dir)).removesuffix(".yaml"))
    if not examples:
        return {}
    # Union in the soft-optional knobs (e.g. ``tracing``) the jinja renders but the examples
    # omit, so the validator accepts an authored tracing block even though no example yields it.
    return {"examples": sorted(examples), "knob_keys": sorted(knob_keys | _SOFT_OPTIONAL_KNOBS)}


def validate_scenario_structure(document: dict[str, Any], reference: dict[str, Any]) -> list[str]:
    """Structurally validate an authored scenario document against the repo's scenario
    format. MECHANISM only — checks SHAPE, not the wisdom of the knob choices.

    The intrinsic contract (matching the repo's example files):
      * top-level is a mapping carrying ``scenario`` as a non-empty list;
      * every scenario item is a mapping with a non-empty string ``name`` and >=1 knob key;
      * scenario-item names are unique;
      * each item uses only knob keys the repo's own examples use — when a non-empty
        reference is available (catches typos / format drift). Skipped if no reference.
    """
    errors: list[str] = []
    if not isinstance(document, dict):
        return ["scenario document must be a YAML mapping"]

    rows = document.get("scenario")
    if not isinstance(rows, list) or not rows:
        return ["scenario document must have a non-empty `scenario` list"]

    ref_keys = set(reference.get("knob_keys", []))
    names: set[str] = set()
    for i, row in enumerate(rows):
        where = f"scenario[{i}]"
        if not isinstance(row, dict):
            errors.append(f"{where} must be a mapping")
            continue
        name = row.get("name")
        if not isinstance(name, str) or not name:
            errors.append(f"{where} must have a non-empty string `name`")
        else:
            if name in names:
                errors.append(f"scenario has a duplicate item name {name!r}")
            names.add(name)
        knobs = {k: v for k, v in row.items() if k != "name"}
        if not knobs:
            errors.append(f"{where} ({name!r}) sets no override knobs")
        # When we could read the repo's examples, refuse any top-level scenario-item key the
        # upstream format does not use. Skipped if no reference was available.
        if ref_keys:
            for k in row:
                if k not in ref_keys:
                    errors.append(
                        f"{where} key {k!r} is not in the repo's scenario format "
                        f"(allowed: {sorted(ref_keys)})"
                    )
    return errors


def _deep_set(target: dict[str, Any], dotted_key: str, value: Any) -> None:
    """Set ``value`` at the dotted path ``dotted_key`` inside ``target``, creating nested
    mappings as needed. Pure mechanism — the agent chose the path and value; we only place
    it. A path segment that collides with an existing non-mapping is overwritten with a new
    mapping (the latest override wins), so the merge is deterministic."""
    parts = dotted_key.split(".")
    node = target
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def _build_scenario_document(name: str, content: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge the per-knob OVERRIDES in ``content`` onto a minimal scenario skeleton.

    ``content`` keys are dotted upstream field paths (e.g. ``vllmCommon.flags.enforceEager``,
    ``routing.servicePort``); a bare ``name`` in ``content`` is folded into the item name.
    Deterministic: keys are applied in sorted order so the emitted YAML is stable. Pure
    mechanism — no knob is special-cased; we only place what the agent supplied."""
    item: dict[str, Any] = {"name": name}
    for key in sorted(content):
        if key == "name":
            continue
        _deep_set(item, key, content[key])
    return {"scenario": [item]}


def _spec_filename(scenario_filename: str) -> str:
    """The companion spec file name for a scenario file: ``<stem>.spec.yaml`` (stripping a
    trailing ``.yaml``/``.yml``). Pure naming mechanism."""
    stem = scenario_filename.removesuffix(".yaml").removesuffix(".yml")
    return f"{stem}{_SPEC_SUFFIX}"


def _build_spec_document(bench_repo: Path, scenario_path: Path) -> dict[str, Any]:
    """Build the companion ``--spec`` SPECIFICATION document that wires the CLI to the
    authored scenario. It carries the three paths the CLI's RenderSpecification requires:

      * ``values_file.path``   — the repo's stock ``defaults.yaml`` (read-only repo),
      * ``template_dir.path``  — the repo's stock ``jinja`` dir (read-only repo),
      * ``scenario_file.path`` — the AUTHORED scenario in the session workspace.

    All paths are ABSOLUTE so the document is self-contained (no Jinja ``base_dir`` needed —
    a plain-YAML spec renders to itself) and ``RenderSpecification._precheck`` can verify each
    one exists. ``base_dir`` is set to the repo root for completeness. Pure mechanism — no
    knob judgment here; the scenario the spec points at carries the agent's choices."""
    return {
        "base_dir": str(bench_repo),
        "values_file": {"path": str(bench_repo.joinpath(*_VALUES_FILE_SUBPATH))},
        "template_dir": {"path": str(bench_repo.joinpath(*_TEMPLATE_DIR_SUBPATH))},
        "scenario_file": {"path": str(scenario_path)},
    }


def author_scenario(
    ctx: ToolContext, *, target_filename: str, content: dict[str, Any]
) -> dict[str, Any]:
    """Author a per-knob scenario override file into the session workspace and validate its
    SHAPE against the repo's scenario examples (read live). Never writes into the repos."""
    name = content.get("name")
    if not isinstance(name, str) or not name:
        raise ToolError(
            "scenario content must include a non-empty string 'name' (the scenario item name)"
        )
    overrides = {k: v for k, v in content.items() if k != "name"}
    if not overrides:
        raise ToolError(
            "scenario content must set at least one override knob besides 'name' "
            "(see knowledge/vllm_overrides.md for which knobs to set)"
        )

    document = _build_scenario_document(name, content)
    reference = _scenario_reference(ctx.settings.bench_repo)
    errors = validate_scenario_structure(document, reference)

    ctx.workspace.mkdir(parents=True, exist_ok=True)
    dest = ctx.workspace / target_filename
    text = yaml.safe_dump(document, sort_keys=False)

    if errors:
        # A structurally-invalid override set is rejected (no file written) so the agent can
        # self-correct. Mechanism boundary, not a benchmarking decision.
        return {
            "artifact_type": "scenario",
            "valid": False,
            "errors": errors,
            "validated_against_examples": reference.get("examples", []),
        }

    dest.write_text(text)
    # Re-parse the written text as a final validity gate (the file the agent will preview).
    try:
        reparsed = yaml.safe_load(text)
        parse_ok = isinstance(reparsed, dict)
    except yaml.YAMLError as exc:  # pragma: no cover - defensive
        return {"artifact_type": "scenario", "valid": False,
                "errors": [f"emitted YAML did not re-parse: {exc}"], "path": str(dest)}

    # Author the companion --spec file beside the scenario so the authored artifact has a real,
    # allowlisted route into the CLI's determinism gate. It points scenario_file at THIS file
    # and values_file/template_dir at the read-only repo's stock copies. Workspace-only write.
    spec_doc = _build_spec_document(ctx.settings.bench_repo, dest)
    spec_dest = ctx.workspace / _spec_filename(target_filename)
    spec_dest.write_text(yaml.safe_dump(spec_doc, sort_keys=False))

    return {
        "path": str(dest),
        "spec_path": str(spec_dest),
        "artifact_type": "scenario",
        "valid": parse_ok,
        "errors": [],
        "scenario_name": name,
        "knobs_set": sorted(overrides),
        "validated_against_examples": reference.get("examples", []),
        "note": "GATE this authored scenario on the CLI's determinism check before any "
                "mutation: pass spec_path as the `spec` argument — "
                "execute_llmdbenchmark(subcommand='plan', spec='" + str(spec_dest) + "', "
                "flags={'dry_run': True}). A clean plan/--dry-run is the acceptance gate. "
                "WHICH knobs to set is judgment — read_knowledge('vllm_overrides').",
    }


async def write_and_validate_config(
    ctx: ToolContext,
    *,
    artifact_type: str,
    target_filename: str,
    content: dict[str, Any],
) -> dict[str, Any]:
    if artifact_type not in _ARTIFACT_TYPES:
        raise ToolError(f"artifact_type must be one of {sorted(_ARTIFACT_TYPES)}")
    # Constrain the filename to the workspace — no path traversal, no repo writes.
    if "/" in target_filename or ".." in target_filename or not target_filename.endswith((".yaml", ".yml")):
        raise ToolError("target_filename must be a bare *.yaml name (no path separators)")

    if artifact_type == "scenario":
        return author_scenario(ctx, target_filename=target_filename, content=content)

    ctx.workspace.mkdir(parents=True, exist_ok=True)
    dest = ctx.workspace / target_filename
    text = yaml.safe_dump(content, sort_keys=False)
    dest.write_text(text)

    # Structural re-parse as a minimal validity gate.
    try:
        yaml.safe_load(text)
        valid, errors = True, []
    except yaml.YAMLError as exc:  # pragma: no cover - defensive
        valid, errors = False, [str(exc)]

    return {
        "path": str(dest),
        "artifact_type": artifact_type,
        "valid": valid,
        "errors": errors,
        "note": "structural check only in MVP; deep validation via CLI --dry-run is deferred",
    }
