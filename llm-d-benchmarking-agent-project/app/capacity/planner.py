"""Build a rendered ``plan_config`` for a spec and classify the capacity planner's verdict.

These are the *pure*, hermetic pieces of the capacity pre-flight (no subprocess, no
network, no cluster): resolving a spec's scenario file, deep-merging it over the repo's
defaults, applying agent overrides, and parsing the planner's flat diagnostic list into a
structured verdict. The subprocess that actually invokes the repo's planner lives in
``app/tools/setup/capacity.py`` (it goes through the allowlisted runner). Keeping the two apart
is what lets the tests exercise the real classification logic without HuggingFace/GPU.

The diagnostic *markers* (``DEPLOYMENT WILL FAIL``, ``ERROR:``, ``WARNING:``) are the
benchmark repo's own contract — see ``llmdbenchmark/utilities/capacity_validator.py``. We
read them; we do not invent sizing rules.
"""
from __future__ import annotations

import copy
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Markers emitted by the repo's capacity_validator. A hard "will fail" line is the
# strongest signal; an ERROR-tagged line means deployment would halt when validation is
# enforced; WARNING lines are advisory (the sim path, gated by ignoreFailedValidation).
_FAIL_MARKER = "DEPLOYMENT WILL FAIL"
_ERROR_TAG = "ERROR:"
_WARNING_TAG = "WARNING:"

# A GPU-COUNT shortfall: the spec requests TP x PP x DP GPUs per replica but the pod has
# fewer (capacity_validator.py:139-144, "... <N> GPUs are required per replica"). This is a
# hard won't-deploy — under enforce=True it is tagged ERROR: (so already infeasible here). But
# under the DEFAULT enforce=False/ignoreFailedValidation path the SAME line is only a WARNING:,
# AND the planner then keeps sizing as if the GPUs existed, emitting the FIT KV-cache markers —
# making an under-provisioned deployment read as feasible:true (a BUG-030-class false fit). We
# treat this line as a hard-fail signal so the default path matches the enforced verdict. The
# benign sibling "...requested per pod. Some GPUs will be idle." (over-provisioned, deploys
# fine) uses a different string, so it is NOT caught.
_GPU_SHORTFALL_MARKER = "gpus are required per replica"

# Substrings the upstream planner emits when it BYPASSES the VRAM/KV-cache sizing for a
# method instead of evaluating it. When sizing is bypassed there is NO fit/won't-fit signal,
# so a clean (no-ERROR) run does NOT mean "it fits" — it means "feasibility was not
# evaluated". We detect these to downgrade the verdict to inconclusive (feasible=None)
# rather than letting an un-sized run read as feasible:true (real-2 #2: a 405B model
# "feasible" because every method was skipped). Faithful to the repo's own log strings:
#   * run_capacity_planner: "<method> is disabled or has 0 replicas -- skipping"
#   * validate_vllm_params: "...Skipping GPU memory checks." (accelerator.memory unknown)
#   * validate_vllm_params: "...skipping memory checks." (model architecture unavailable)
_REPLICA_SKIP_MARKER = "0 replicas -- skipping"
_GPU_SKIP_MARKERS = ("skipping gpu memory checks", "skipping memory checks")
# The upstream sizing computation THREW for a method (e.g. a HuggingFace config fetch failed) —
# under ignoreFailedValidation this surfaces only as a WARNING, but NO fit verdict was produced, so
# feasibility was not evaluated (same effect as a skip). capacity_validator.py:305.
_SIZE_FAILED_MARKER = "cannot estimate model memory or kv cache"
# The deployment method is `fma` (fast model actuation): run_capacity_planner returns EARLY with
# NO sizing at all — "Deployment method is fma -- skipping vLLM capacity validation"
# (capacity_validator.py:419). Like the skip/threw markers, this means feasibility was NOT
# evaluated, so a clean run must downgrade to inconclusive rather than read as feasible:true (the
# same un-evaluated-reads-feasible class as BUG-030/035, but the FMA early-return path). The
# `examples/fma` spec sets fma.enabled:true, so this is reachable from a real catalog spec.
_FMA_SKIP_MARKER = "skipping vllm capacity validation"
# The lines that prove a method's KV-cache/VRAM arithmetic actually ran to a FIT verdict
# (capacity_validator.py:280-302, the fits branch). The "...available GPU memory" line is ONLY the
# GPU-COUNT summary, emitted BEFORE sizing and independent of whether it ran — keying sizing-proof
# on it let an un-sized run (sizing threw → only a WARNING) read as feasible:true (BUG-030). A
# won't-fit verdict is carried by _FAIL_MARKER instead, so these mark only the fits path.
_SIZED_MARKERS = (
    "allocatable kv cache memory",
    "per-request kv cache",
    "max concurrent requests",
)

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

    feasible: bool | None                # True=fits / False=won't fit / None=NOT evaluated
    will_fail: bool                      # a "DEPLOYMENT WILL FAIL" line was present
    errors: list[str] = field(default_factory=list)     # ERROR:-tagged lines
    warnings: list[str] = field(default_factory=list)   # WARNING:-tagged lines
    info: list[str] = field(default_factory=list)       # everything else (sizing facts)
    diagnostics: list[str] = field(default_factory=list)  # the raw list, verbatim
    # Whether the VRAM/KV-cache sizing was actually EVALUATED (vs bypassed for every method
    # because a method had 0 replicas / the GPU memory or model architecture was unknown).
    # When False, ``feasible`` is None (inconclusive) and ``inconclusive_reason`` says why —
    # a clean run with sizing skipped must NOT read as feasible:true (real-2 #2).
    sizing_evaluated: bool = True
    inconclusive_reason: str = ""
    # Gated-model access pre-flight (Phase 62) — facts from the repo's OWN gating check.
    # gated: any served model is gated. authorized: True if your token can pull every gated
    # model, False if any cannot, None when gating is N/A or unknown. gated_reason: the
    # upstream detail text (never the token). All None/"" when no gating check ran.
    gated: bool | None = None
    authorized: bool | None = None
    gated_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


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
    any_sized = False          # a method's VRAM/KV-cache arithmetic actually ran
    replica_skip = False       # >=1 method skipped for 0 replicas / disabled
    gpu_skip = False           # sizing bypassed (unknown GPU memory / model architecture)
    fma_skip = False           # deployment method is fma -> capacity validation skipped entirely

    for line in diags:
        low = line.lower()
        if _FAIL_MARKER in line or _GPU_SHORTFALL_MARKER in low:
            # A "DEPLOYMENT WILL FAIL" line, or a GPU-COUNT shortfall (more GPUs/replica
            # required than the pod has) — both are hard won't-deploy conditions, even when
            # the upstream only tags them WARNING under ignoreFailedValidation.
            will_fail = True
        if any(m in low for m in _SIZED_MARKERS):
            any_sized = True
        if _REPLICA_SKIP_MARKER in line:
            replica_skip = True
        if any(m in low for m in _GPU_SKIP_MARKERS) or _SIZE_FAILED_MARKER in low:
            # Sizing was bypassed (skipped) OR threw (cannot-estimate) — either way no fit verdict.
            gpu_skip = True
        if _FMA_SKIP_MARKER in low:
            # fma deployment method: the planner returns early with NO sizing at all, so
            # feasibility was NOT evaluated (BUG-030/035 class, the fma early-return path).
            fma_skip = True
        if _ERROR_TAG in line:
            errors.append(line)
        elif _WARNING_TAG in line:
            warnings.append(line)
        else:
            info.append(line)

    hard_infeasible = will_fail or bool(errors)
    # Sizing is treated as bypassed ONLY on POSITIVE evidence that the planner emitted a skip
    # line / a sizing exception — never merely from the absence of sizing facts (an empty or
    # info/warning-only diagnostic list keeps the prior feasible reading). Bypass signals, all
    # faithful to the planner's own log strings:
    #   * a 0-replica / disabled method skip ("<method> ... 0 replicas -- skipping"); or
    #   * the VRAM/KV-cache memory-fit check being skipped ("...skipping (gpu) memory checks.")
    #     because the accelerator memory or model architecture was unknown; or
    #   * the sizing computation THROWING ("Cannot estimate model memory or KV cache ...") — under
    #     ignoreFailedValidation that's only a WARNING, so without this it would read as feasible; or
    #   * the deployment method being `fma`, which returns early skipping ALL capacity validation
    #     ("...skipping vLLM capacity validation") — no sizing ran, so it is NOT a fit verdict.
    # `any_sized` keys on the FIT-path KV-cache lines (_SIZED_MARKERS), NOT the "...available GPU
    # memory" GPU-COUNT summary (which is emitted before sizing and is NOT a fit verdict — BUG-030).
    # When a method was skipped for 0 replicas yet ANOTHER method DID size, that other method
    # carries the verdict, so a replica skip alone (with sizing elsewhere) is not a bypass.
    # A hard ERROR / will-fail is authoritative and wins below; we only downgrade an otherwise
    # CLEAN run to inconclusive (real-2 #2: a clean run with sizing bypassed must not read as
    # feasible:true).
    sizing_bypassed = fma_skip or gpu_skip or (replica_skip and not any_sized)
    sizing_evaluated = not sizing_bypassed
    inconclusive_reason = ""
    if hard_infeasible:
        feasible: bool | None = False
    elif sizing_bypassed:
        feasible = None
        if fma_skip:
            inconclusive_reason = (
                "Capacity validation skipped — the deployment method is fma (fast model "
                "actuation), which the planner does NOT size, so feasibility was NOT evaluated. "
                "The KV-cache/VRAM pre-flight does not apply to an fma deployment."
            )
        elif replica_skip and not any_sized:
            inconclusive_reason = (
                "VRAM sizing skipped (spec has 0 decode/prefill replicas) — feasibility NOT "
                "evaluated. Set decode_replicas/prefill_replicas (>=1) so the planner sizes "
                "the deployment, then re-check."
            )
        else:
            inconclusive_reason = (
                "VRAM sizing skipped (accelerator memory or model architecture could not be "
                "determined) — feasibility NOT evaluated. Supply gpu_memory_gb and a model the "
                "planner can fetch a config for, then re-check."
            )
    else:
        feasible = True

    return CapacityVerdict(
        feasible=feasible,
        will_fail=will_fail,
        errors=errors,
        warnings=warnings,
        info=info,
        diagnostics=diags,
        sizing_evaluated=sizing_evaluated,
        inconclusive_reason=inconclusive_reason,
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

    Mirrors how the standup composes a values file from defaults + the scenario overlay —
    specifically the repo's OWN ``RenderSpecification.deep_merge`` (parser/render_plans.py),
    whose documented contract is: "``None`` values in the override dict are skipped (YAML keys
    with no value do not clobber defaults)". We must match it byte-for-byte, because the
    plan_config we hand the planner has to be the one the real standup would build.

    A bare YAML key with no value (``decode:`` with every sub-key commented out, as in the
    examples/gpu scenario) parses to ``None``. Faithfully skipping it keeps the rich default
    ``decode`` block (``replicas: 1`` and friends) instead of replacing it with ``None`` —
    which is what was wiping the section and causing both the ``'NoneType'.get()`` crash in
    the upstream planner AND the spurious "decode … 0 replicas -- skipping" that bypassed VRAM
    sizing. Non-dict, non-None values (and lists) replace wholesale; nested dicts merge
    key-by-key.
    """
    out = copy.deepcopy(base)
    for key, val in (over or {}).items():
        if val is None:
            continue  # YAML key with no value -- don't clobber the default (upstream contract)
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
        # The agent's `model` override means "serve THIS model" — but the scenario's
        # `model.name` AND `model.huggingfaceId` both start out as the spec default (e.g.
        # facebook/opt-125m), and BOTH the upstream sizing path (capacity_validator reads
        # `model.huggingfaceId or model.name`) and the gated-access check PREFER
        # `huggingfaceId`. Setting only `model.name` left the planner sizing + gating the
        # SPEC DEFAULT model, not the override (real-2 #2: "Model facebook/opt-125m is not
        # gated -- ..." for an override of Llama-3.1-405B). So when `model` is overridden
        # (and the caller didn't ALSO pass an explicit `huggingface_id`), keep `huggingfaceId`
        # in lockstep so the model actually EVALUATED is the one the user asked for. This is
        # an INTERNAL consistency sync of the same value, not a distinct user override, so it
        # is folded into the one `model.name` transparency line rather than listed twice.
        applied.append(f"{'.'.join(path)} = {value!r}")
        if key == "model" and "huggingface_id" not in (overrides or {}):
            hf_path = _OVERRIDE_PATHS["huggingface_id"]
            _set_path(plan_config, hf_path, value)
            applied.append(f"{'.'.join(hf_path)} = {value!r}")
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
