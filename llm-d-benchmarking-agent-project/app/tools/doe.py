"""generate_doe_experiment — AUTHOR a Design-of-Experiments (DoE) experiment YAML.

The agent supplies the FACTORS to sweep (each: a name, the dotted config key it overrides,
and the list of levels) for the optional ``setup`` phase and the required ``run`` phase.
This tool cross-products those factors × levels into the full TREATMENTS matrix (pure
mechanism in ``app/validation/doe.py``), emits a valid experiment YAML into the session
workspace (never into the read-only repos), and validates it STRUCTURALLY against the
llm-d-benchmark experiment example format read LIVE from the repo on disk (we never vendor
a copy of that format).

Thin code / thick agent: WHICH factors and levels to sweep is the agent's judgment, grounded
in ``knowledge/sweep_playbook.md`` (e.g. "to find the optimal prefill/decode ratio, sweep the
prefill/decode replica split"; "elicit the token-length distribution and prefix-reuse ratio").
There is NO factor/level decision logic in this Python — the handler only mechanises the
cross-product, the YAML emission, and the structural validation.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from app.tools.context import ToolContext, ToolError
from app.validation.doe import DoEError, build_doe_experiment

# The runtime contract for a treatment row, derived from the repo's own example experiment
# files (see ``_reference_structure``): each runtime treatment is a mapping carrying a
# ``name`` plus one-or-more dotted-key overrides. We re-derive this from the repo at runtime
# rather than hardcoding it, so it can't drift from the upstream format.
_EXPERIMENTS_SUBDIR = ("workload", "experiments")


def _reference_structure(bench_repo: Path) -> dict[str, Any]:
    """Read the repo's experiment EXAMPLE files at runtime and derive the structural
    contract a generated experiment must satisfy. Returns the set of top-level keys seen
    and whether runtime treatment rows are name-bearing mappings. Reads repo truth; never
    vendors a copy. Returns ``{}`` (and the caller falls back to the static contract) when
    the repo/examples are absent."""
    exp_dir = bench_repo.joinpath(*_EXPERIMENTS_SUBDIR)
    if not exp_dir.is_dir():
        return {}
    top_keys: set[str] = set()
    treatment_is_named_mapping = False
    examples: list[str] = []
    for path in sorted(exp_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text())
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(data, dict):
            continue
        examples.append(path.name)
        top_keys.update(data.keys())
        rows = data.get("treatments")
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and isinstance(row.get("name"), str):
                    treatment_is_named_mapping = True
                    break
    if not examples:
        return {}
    return {
        "examples": examples,
        "top_keys": sorted(top_keys),
        "treatment_is_named_mapping": treatment_is_named_mapping,
    }


def validate_structure(document: dict[str, Any], reference: dict[str, Any]) -> list[str]:
    """Structurally validate a generated experiment document against the repo's experiment
    format. Mechanism only — checks SHAPE, not the wisdom of the factor choices.

    The contract (matching ``llmdbenchmark.experiment.parser.parse_experiment`` and the
    repo's example files):
      * top-level is a mapping;
      * it carries run treatments under ``treatments`` (or ``run``) as a non-empty list;
      * every treatment row is a mapping with a string ``name`` and >=1 override key;
      * if a ``setup`` block exists, its ``treatments`` follow the same row contract;
      * the document uses only keys the repo's own examples use (when a reference is given).
    """
    errors: list[str] = []
    if not isinstance(document, dict):
        return ["experiment document must be a YAML mapping"]

    run_rows = document.get("treatments")
    if run_rows is None and isinstance(document.get("run"), list):
        run_rows = document.get("run")
    if not isinstance(run_rows, list) or not run_rows:
        errors.append("experiment must have a non-empty `treatments` list (the run treatments)")
        run_rows = []

    def _check_rows(rows: list[Any], where: str) -> None:
        names: set[str] = set()
        for i, row in enumerate(rows):
            if not isinstance(row, dict):
                errors.append(f"{where}[{i}] must be a mapping")
                continue
            name = row.get("name")
            if not isinstance(name, str) or not name:
                errors.append(f"{where}[{i}] must have a non-empty string `name`")
                continue
            if name in names:
                errors.append(f"{where} has a duplicate treatment name {name!r}")
            names.add(name)
            overrides = {k: v for k, v in row.items() if k != "name"}
            if not overrides:
                errors.append(f"{where}[{i}] ({name!r}) has no override keys")

    _check_rows(run_rows, "treatments")

    setup = document.get("setup")
    if setup is not None:
        if not isinstance(setup, dict):
            errors.append("`setup` must be a mapping when present")
        else:
            setup_rows = setup.get("treatments")
            if not isinstance(setup_rows, list) or not setup_rows:
                errors.append("`setup.treatments` must be a non-empty list when `setup` is present")
            else:
                _check_rows(setup_rows, "setup.treatments")

    # When we could read the repo's examples, refuse any top-level key the upstream format
    # does not use (catches typos / format drift). Skipped if no reference was available.
    ref_keys = set(reference.get("top_keys", []))
    if ref_keys:
        for k in document:
            if k not in ref_keys:
                errors.append(
                    f"top-level key {k!r} is not in the repo's experiment format "
                    f"(allowed: {sorted(ref_keys)})"
                )
    return errors


def _safe_filename(target_filename: str) -> str:
    if "/" in target_filename or "\\" in target_filename or ".." in target_filename:
        raise ToolError("target_filename must be a bare *.yaml name (no path separators)")
    if not target_filename.endswith((".yaml", ".yml")):
        raise ToolError("target_filename must end in .yaml or .yml")
    return target_filename


async def generate_doe_experiment(
    ctx: ToolContext,
    *,
    name: str,
    run_factors: list[dict[str, Any]],
    setup_factors: list[dict[str, Any]] | None = None,
    run_constants: dict[str, Any] | None = None,
    setup_constants: dict[str, Any] | None = None,
    harness: str | None = None,
    profile: str | None = None,
    description: str | None = None,
    target_filename: str | None = None,
) -> dict[str, Any]:
    """Cross-product the agent-chosen factors × levels into a treatments matrix, write it as
    a valid experiment YAML in the workspace, and structurally validate it against the repo's
    experiment example format. Read-only w.r.t. the cluster/repos (it only writes into the
    session workspace), so it auto-runs."""
    fname = _safe_filename(target_filename or f"{name}.yaml")

    try:
        result = build_doe_experiment(
            name=name,
            setup_factors=setup_factors,
            run_factors=run_factors,
            setup_constants=setup_constants,
            run_constants=run_constants,
            harness=harness,
            profile=profile,
            description=description,
        )
    except DoEError as exc:
        # A structurally-invalid factor set is rejected (no file written) so the agent can
        # self-correct. This is the mechanism boundary, not a benchmarking decision.
        return {"generated": False, "reason": str(exc)}

    reference = _reference_structure(ctx.settings.bench_repo)
    errors = validate_structure(result.document, reference)
    if errors:
        return {"generated": False, "reason": "generated experiment failed structural validation",
                "errors": errors}

    ctx.workspace.mkdir(parents=True, exist_ok=True)
    dest = ctx.workspace / fname
    text = yaml.safe_dump(result.document, sort_keys=False)
    dest.write_text(text)

    # Re-parse the written text as a final validity gate (the file the agent will pass to
    # `execute_llmdbenchmark(subcommand="experiment", flags={experiments: <path>})`).
    try:
        reparsed = yaml.safe_load(text)
        parse_ok = isinstance(reparsed, dict)
    except yaml.YAMLError as exc:  # pragma: no cover - defensive
        return {"generated": False, "reason": f"emitted YAML did not re-parse: {exc}",
                "path": str(dest)}

    return {
        "generated": True,
        "path": str(dest),
        "experiment_name": name,
        "setup_treatments": result.setup_treatments,
        "run_treatments": result.run_treatments,
        "n_setup_treatments": len(result.setup_treatments),
        "n_run_treatments": len(result.run_treatments),
        "total_matrix": result.total_matrix,
        "valid": parse_ok and not errors,
        "validated_against_examples": reference.get("examples", []),
        "note": "Pass this path as flags.experiments to execute_llmdbenchmark "
                "(subcommand='experiment' for a full DoE, or 'run' for a run-parameter sweep "
                "against an already-stood-up stack). Always preview with flags.dry_run first.",
    }
