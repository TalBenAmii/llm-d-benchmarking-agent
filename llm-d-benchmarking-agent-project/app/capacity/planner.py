"""Build a rendered ``plan_config`` for a spec and classify the capacity planner's verdict.

These are the *pure*, hermetic pieces of the capacity pre-flight (no subprocess, no
network, no cluster): resolving a spec's scenario file, deep-merging it over the repo's
defaults, applying agent overrides, and parsing the planner's flat diagnostic list into a
structured verdict. The subprocess that actually invokes the repo's planner lives in
``app/tools/capacity.py`` (it goes through the allowlisted runner). Keeping the two apart
is what lets the tests exercise the real classification logic without HuggingFace/GPU.

The diagnostic *markers* (``DEPLOYMENT WILL FAIL``, ``ERROR:``, ``WARNING:``) are the
benchmark repo's own contract — see ``llmdbenchmark/utilities/capacity_validator.py``. We
read them; we do not invent sizing rules.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Markers emitted by the repo's capacity_validator. A hard "will fail" line is the
# strongest signal; an ERROR-tagged line means deployment would halt when validation is
# enforced; WARNING lines are advisory (the sim path, gated by ignoreFailedValidation).
_FAIL_MARKER = "DEPLOYMENT WILL FAIL"
_ERROR_TAG = "ERROR:"
_WARNING_TAG = "WARNING:"

# Plan-config keys an override may touch. Restricting the surface keeps overrides honest:
# the agent expresses *what the user asked for* (a bigger model, longer context, a real
# GPU), not arbitrary helm-values surgery. Mechanism, not judgment.
_OVERRIDE_PATHS: dict[str, tuple[str, ...]] = {
    "model": ("model", "name"),
    "huggingface_id": ("model", "huggingfaceId"),
    "max_model_len": ("model", "maxModelLen"),
    "gpu_memory_utilization": ("model", "gpuMemoryUtilization"),
    "gpu_memory_gb": ("accelerator", "memory"),
    "accelerator_count": ("accelerator", "count"),
    "tensor_parallelism": ("decode", "parallelism", "tensor"),
    "data_parallelism": ("decode", "parallelism", "data"),
    "decode_replicas": ("decode", "replicas"),
    "prefill_replicas": ("prefill", "replicas"),
}


class CapacityError(RuntimeError):
    """A pre-flight could not be set up (e.g. spec/scenario/defaults not on disk)."""


@dataclass
class CapacityVerdict:
    """A structured reading of the planner's flat diagnostic list. Facts only — the
    remediation narrative belongs to the agent + ``knowledge/capacity.md``."""

    feasible: bool                       # no hard-fail / error line
    will_fail: bool                      # a "DEPLOYMENT WILL FAIL" line was present
    errors: list[str] = field(default_factory=list)     # ERROR:-tagged lines
    warnings: list[str] = field(default_factory=list)   # WARNING:-tagged lines
    info: list[str] = field(default_factory=list)       # everything else (sizing facts)
    diagnostics: list[str] = field(default_factory=list)  # the raw list, verbatim
    # Gated-model access pre-flight (Phase 62) — facts from the repo's OWN gating check.
    # gated: any served model is gated. authorized: True if your token can pull every gated
    # model, False if any cannot, None when gating is N/A or unknown. gated_reason: the
    # upstream detail text (never the token). All None/"" when no gating check ran.
    gated: bool | None = None
    authorized: bool | None = None
    gated_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "feasible": self.feasible,
            "will_fail": self.will_fail,
            "errors": self.errors,
            "warnings": self.warnings,
            "info": self.info,
            "diagnostics": self.diagnostics,
            "gated": self.gated,
            "authorized": self.authorized,
            "gated_reason": self.gated_reason,
        }


def classify_diagnostics(diagnostics: list[str]) -> CapacityVerdict:
    """Bucket the repo planner's flat string diagnostics by their own markers.

    Infeasible == any hard-fail OR ERROR:-tagged line. This is a faithful echo of the
    repo's own halt condition (``"ERROR:" in diag`` in step_03's sanity check), not a new
    policy of ours.
    """
    diags = [str(d) for d in (diagnostics or [])]
    errors: list[str] = []
    warnings: list[str] = []
    info: list[str] = []
    will_fail = False

    for line in diags:
        if _FAIL_MARKER in line:
            will_fail = True
        if _ERROR_TAG in line:
            errors.append(line)
        elif _WARNING_TAG in line:
            warnings.append(line)
        else:
            info.append(line)

    feasible = not (will_fail or errors)
    return CapacityVerdict(
        feasible=feasible,
        will_fail=will_fail,
        errors=errors,
        warnings=warnings,
        info=info,
        diagnostics=diags,
    )


def merge_gated_access(
    verdict: CapacityVerdict, gated_access: dict[str, Any] | None
) -> CapacityVerdict:
    """Copy the bridge's ``gated_access`` block onto the verdict — facts only, no policy.

    A pure field copy of the repo gating check's output (gated / authorized / reason).
    There is NO if/elif decision here: what to *say* for PUBLIC vs GATED+AUTHORIZED vs
    GATED+UNAUTHORIZED (and whether to offer Phase 30 secret-provisioning) is the agent's
    judgment, read from ``knowledge/capacity.md``. A ``None`` block (no model id, or no
    bridge gating field) leaves the defaulted None/"" fields unchanged.
    """
    if not isinstance(gated_access, dict):
        return verdict
    verdict.gated = gated_access.get("gated")
    verdict.authorized = gated_access.get("authorized")
    verdict.gated_reason = str(gated_access.get("reason", ""))
    return verdict


def resolve_scenario_file(bench_repo: Path, spec: str) -> Path:
    """Read the spec template on disk and return the scenario YAML it points at.

    Reads repo truth (the ``scenario_file.path`` line in ``config/specification/<spec>``)
    rather than hard-coding the ``specification`` -> ``scenarios`` naming convention.
    """
    spec_file = bench_repo / "config" / "specification" / f"{spec}.yaml.j2"
    if not spec_file.is_file():
        raise CapacityError(
            f"spec {spec!r} has no template at {spec_file} — list_catalog first to get a "
            "valid spec name"
        )
    text = spec_file.read_text()
    # The value may contain spaces (jinja, e.g. '{{ base_dir }}/config/...'); capture the
    # rest of the line, not just up to the first space.
    m = re.search(r"scenario_file:\s*\n\s*path:\s*(.+)", text)
    if not m:
        raise CapacityError(
            f"spec {spec!r} declares no scenario_file path; capacity pre-flight needs a "
            "scenario to read the model/accelerator/parallelism from"
        )
    raw = m.group(1).strip().strip('"').strip("'")
    # The path is jinja, e.g. '{{ base_dir }}/config/scenarios/cicd/kind.yaml'. base_dir
    # defaults to the repo root ('../' relative to config/), so strip the jinja prefix and
    # anchor the remainder at the repo root.
    rel = re.sub(r"^\{\{.*?\}\}/?", "", raw).lstrip("/")
    scenario = (bench_repo / rel).resolve()
    if not scenario.is_file():
        raise CapacityError(
            f"scenario file for spec {spec!r} not found at {scenario} (declared as {raw!r})"
        )
    return scenario


def _load_first_scenario(scenario_file: Path) -> dict[str, Any]:
    doc = yaml.safe_load(scenario_file.read_text()) or {}
    scenarios = doc.get("scenario")
    if not isinstance(scenarios, list) or not scenarios:
        raise CapacityError(
            f"scenario file {scenario_file} has no 'scenario:' list — cannot build a "
            "plan_config"
        )
    first = scenarios[0]
    if not isinstance(first, dict):
        raise CapacityError(f"scenario[0] in {scenario_file} is not a mapping")
    return first


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``over`` onto a deep copy of ``base`` (scenario over defaults).

    Mirrors how the standup composes a values file from defaults + the scenario overlay.
    Non-dict values (and lists) replace wholesale; nested dicts merge key-by-key.
    """
    out = copy.deepcopy(base)
    for key, val in (over or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out


def _set_path(cfg: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    cur = cfg
    for key in path[:-1]:
        nxt = cur.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[key] = nxt
        cur = nxt
    cur[path[-1]] = value


def apply_overrides(plan_config: dict[str, Any], overrides: dict[str, Any]) -> list[str]:
    """Apply the agent's conversation-derived overrides onto a plan_config in place.

    Returns a human-readable list of what changed (for transparency). Unknown override
    keys are rejected loudly so a typo can't silently no-op a feasibility check.
    """
    applied: list[str] = []
    for key, value in (overrides or {}).items():
        if value is None:
            continue
        path = _OVERRIDE_PATHS.get(key)
        if path is None:
            raise CapacityError(
                f"unknown capacity override {key!r}; valid overrides: "
                f"{sorted(_OVERRIDE_PATHS)}"
            )
        _set_path(plan_config, path, value)
        applied.append(f"{'.'.join(path)} = {value!r}")
    return applied


def plan_config_for_spec(
    bench_repo: Path,
    spec: str,
    *,
    overrides: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Render the plan_config for ``spec``: scenario merged over the repo defaults, with
    the agent's overrides applied. Returns ``(plan_config, applied_overrides)``.

    Reads only on-disk repo truth — never network. The result is the exact shape the
    repo's ``run_capacity_planner`` consumes.
    """
    defaults_file = bench_repo / "config" / "templates" / "values" / "defaults.yaml"
    if not defaults_file.is_file():
        raise CapacityError(
            f"benchmark repo defaults not found at {defaults_file}; clone/install the repo "
            "first"
        )
    defaults = yaml.safe_load(defaults_file.read_text()) or {}
    if not isinstance(defaults, dict):
        raise CapacityError(f"defaults file {defaults_file} did not parse to a mapping")

    scenario_file = resolve_scenario_file(bench_repo, spec)
    scenario = _load_first_scenario(scenario_file)

    plan_config = _deep_merge(defaults, scenario)
    applied = apply_overrides(plan_config, overrides or {})
    return plan_config, applied
