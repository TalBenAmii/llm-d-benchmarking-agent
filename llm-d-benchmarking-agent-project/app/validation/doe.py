"""Pure Design-of-Experiments (DoE) cross-product mechanism.

This module is PURE MECHANISM: given a set of *factors* (each a ``name``, a dotted
override ``key``, and a list of ``levels``) it computes the full cross-product of
*treatments* and emits the dict an experiment YAML expects. It contains ZERO judgment
about WHICH factors or levels to sweep — that is the agent's call, supplied as tool args
and grounded in ``knowledge/sweep_playbook.md``. There are no ``if/elif`` branches that
encode benchmarking decisions here; the only conditionals are structural validation of
the caller's request.

The emitted shape mirrors the llm-d-benchmark experiment format that
``llmdbenchmark.experiment.parser.parse_experiment`` consumes (verified at runtime
against the repo's own examples — see ``app/tools/run/doe.py``):

    experiment:
      name: <name>
      harness: <harness?>
      profile: <profile?>
    design:                # informational DoE metadata (mirrors the repo examples)
      run:
        constants:         # run-phase constants, list-of-{key,value} as the examples do
          - {key: <dotted.key>, value: <value>}
    setup:                 # present only when setup factors are given
      constants: {<dotted.key>: <value>, ...}   # parser-consumed: merged into each setup row
      treatments:
        - {name: <t>, <dotted.key>: <level>, ...}
    treatments:            # the RUN treatments (a.k.a. ``run:``)
      - {name: <t>, <dotted.key>: <level>, ...}

Run-phase constants are emitted under ``design.run.constants`` (the location the repo's
own ``optimized-baseline.yaml`` / ``pd-disaggregation.yaml`` examples use) — NOT as a
top-level ``run`` key, which is not part of the upstream format and would be rejected by
the structural validator. The parser does not consume run constants (they are held fixed
in the profile, not swept), so this placement is informational and example-compatible.

Each treatment row is the cross-product of one level per factor: N factors with
L1, L2, ... Ln levels yield ``L1 * L2 * ... * Ln`` treatments.
"""
from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from typing import Any

# A factor name / treatment name is constrained to a safe, file/label-friendly token so
# generated treatment names are stable identifiers (used as K8s-ish labels downstream).
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")
# A dotted override key: dot-separated identifier segments, e.g. ``decode.parallelism.tensor``.
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]*(\.[A-Za-z_][A-Za-z0-9_\-]*)*$")
# A purely-numeric dotted segment means the key INDEXES A LIST element (e.g. ``load.stages.0.rate``).
_LIST_INDEX_SEG_RE = re.compile(r"(?:^|\.)(\d+)(?:\.|$)")


def _list_index_reason(key: Any) -> str:
    """Extra rejection detail when a rejected key indexes a LIST element via a numeric segment.

    Upstream ``apply_overrides`` (llmdbenchmark/utilities/profile_renderer.py) walks DICTS ONLY: a
    numeric segment isn't a dict key, so the path never matches and the override never applies. The
    current upstream RETURNS such keys as ``unmatched_keys`` for the caller to warn on (they were
    silently dropped under the old API), but either way the override is a runtime no-op. Naming that
    here stops a caller from hand-editing YAML around a rejection that is actually protecting them
    from a no-op run."""
    if not isinstance(key, str):
        return ""
    m = _LIST_INDEX_SEG_RE.search(key)
    if not m:
        return ""
    return (f" — segment {m.group(1)!r} indexes a LIST element, which upstream apply_overrides "
            "cannot apply (it walks dicts only, so a list-indexed override never lands — upstream "
            "returns it as an unmatched key and it no-ops at runtime); use a dict-keyed path, or "
            "vary this via a different profile/workload rather than a list index")


class DoEError(ValueError):
    """Raised when a factor/level request is structurally invalid (mechanism-level, not
    a judgment call). The message is safe to surface to the agent so it can self-correct."""


@dataclass(frozen=True)
class Factor:
    """One swept parameter: a human ``name``, the dotted config ``key`` the level overrides,
    and the list of ``levels`` to sweep. Mechanism only — no opinion on what to sweep."""

    name: str
    key: str
    levels: list[Any]


@dataclass
class DoEResult:
    document: dict[str, Any]
    setup_treatments: list[dict[str, Any]] = field(default_factory=list)
    run_treatments: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total_matrix(self) -> int:
        s = max(len(self.setup_treatments), 1)
        r = max(len(self.run_treatments), 1)
        return s * r


def _coerce_factor(raw: Any, *, phase: str, index: int) -> Factor:
    """Validate ONE factor request into a :class:`Factor`. Structural checks only."""
    if not isinstance(raw, dict):
        raise DoEError(f"{phase}.factors[{index}] must be a mapping with name/key/levels")
    name = raw.get("name")
    key = raw.get("key")
    levels = raw.get("levels")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise DoEError(
            f"{phase}.factors[{index}].name must be a token matching {_NAME_RE.pattern} "
            f"(got {name!r})"
        )
    if not isinstance(key, str) or not _KEY_RE.match(key):
        raise DoEError(
            f"{phase}.factors[{index}].key must be a dotted override key like "
            f"'decode.parallelism.tensor' (got {key!r}){_list_index_reason(key)}"
        )
    if not isinstance(levels, list) or len(levels) == 0:
        raise DoEError(f"{phase}.factors[{index}].levels must be a non-empty list (got {levels!r})")
    # Levels must be scalars (the value placed under the dotted key in a treatment row).
    for lv in levels:
        if isinstance(lv, (dict, list)):
            raise DoEError(
                f"{phase}.factors[{index}].levels must be scalars (str/int/float/bool), "
                f"not nested containers (got {lv!r})"
            )
    return Factor(name=name, key=key, levels=list(levels))


def _coerce_factors(raw_factors: Any, *, phase: str) -> list[Factor]:
    if raw_factors is None:
        return []
    if not isinstance(raw_factors, list):
        raise DoEError(f"{phase}.factors must be a list of factor mappings")
    factors = [_coerce_factor(f, phase=phase, index=i) for i, f in enumerate(raw_factors)]
    seen_names: set[str] = set()
    seen_keys: set[str] = set()
    for f in factors:
        if f.name in seen_names:
            raise DoEError(f"{phase}.factors has a duplicate factor name {f.name!r}")
        if f.key in seen_keys:
            raise DoEError(f"{phase}.factors has a duplicate override key {f.key!r}")
        seen_names.add(f.name)
        seen_keys.add(f.key)
    return factors


def _level_token(value: Any) -> str:
    """A short, stable, name-safe token for a level value, used to build a treatment name.
    Pure formatting — booleans render as on/off, others are stringified and sanitized."""
    token = ("on" if value else "off") if isinstance(value, bool) else str(value)
    token = re.sub(r"[^A-Za-z0-9]+", "-", token).strip("-")
    return token or "x"


def _treatment_name(factors: list[Factor], combo: tuple[Any, ...]) -> str:
    """Deterministic treatment name from the chosen level of each factor, e.g.
    ``tp2-rep4``. Single-factor sweeps drop the factor prefix when the level token is
    already descriptive enough on its own (keeps the common case clean)."""
    parts = [f"{f.name}{_level_token(v)}" if len(f.levels) else f.name
             for f, v in zip(factors, combo, strict=True)]
    return "-".join(parts)


def _expand(factors: list[Factor]) -> list[dict[str, Any]]:
    """Full cartesian product → list of treatment rows. Each row is a mapping of
    ``{name, <dotted.key>: <level>, ...}``. Deduped on the full (key→value) content."""
    if not factors:
        return []
    rows: list[dict[str, Any]] = []
    seen_payloads: set[tuple[tuple[str, Any], ...]] = set()
    seen_names: set[str] = set()
    for combo in itertools.product(*(f.levels for f in factors)):
        payload = {f.key: v for f, v in zip(factors, combo, strict=True)}
        sig = tuple(sorted((k, _hashable(v)) for k, v in payload.items()))
        if sig in seen_payloads:
            continue  # identical treatment content (e.g. caller passed a repeated level)
        seen_payloads.add(sig)
        name = _treatment_name(factors, combo)
        name = _dedupe_name(name, seen_names)
        seen_names.add(name)
        rows.append({"name": name, **payload})
    return rows


def _hashable(value: Any) -> Any:
    """A signature key for ONE level value, used to dedupe treatment payloads.

    The value is paired with its concrete TYPE name so DISTINCT levels that merely compare
    equal in Python are kept apart. Without the type tag, ``1`` (int), ``1.0`` (float) and
    ``True`` (bool) all collide in the dedupe set (``1 == 1.0 == True`` with equal hashes), so
    a factor like ``levels=[1, True]`` or ``levels=[1, 1.0]`` would silently drop a genuinely
    distinct treatment. A true repeat (same type AND value, e.g. ``[10, 10]``) still dedupes,
    since both the type tag and the value match."""
    base = value if isinstance(value, (str, int, float, bool)) or value is None else str(value)
    return (type(base).__name__, base)


def _dedupe_name(name: str, taken: set[str]) -> str:
    if name not in taken:
        return name
    i = 2
    while f"{name}-{i}" in taken:
        i += 1
    return f"{name}-{i}"


def _constants_map(raw: Any, *, phase: str) -> dict[str, Any]:
    """Constants merged into every treatment of a phase. Accepts the agent's mapping of
    dotted-key → value; validates the keys structurally only (no judgment on values)."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise DoEError(f"{phase}.constants must be a mapping of dotted-key → value")
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not _KEY_RE.match(k):
            raise DoEError(
                f"{phase}.constants key {k!r} must be a dotted override key{_list_index_reason(k)}"
            )
        out[k] = v
    return out


def build_doe_experiment(
    *,
    name: str,
    setup_factors: Any = None,
    run_factors: Any = None,
    setup_constants: Any = None,
    run_constants: Any = None,
    harness: str | None = None,
    profile: str | None = None,
    description: str | None = None,
) -> DoEResult:
    """Expand agent-chosen factors × levels into a full treatments matrix and assemble the
    experiment document. PURE: deterministic, no I/O, no factor/level judgment.

    At least one RUN factor is required (an experiment with no run treatments has nothing
    to measure). SETUP factors are optional — when given, each setup treatment triggers its
    own standup/teardown (a full DoE); when absent, all run treatments share one stack.
    """
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise DoEError(f"experiment name must be a token matching {_NAME_RE.pattern} (got {name!r})")

    setup = _coerce_factors(setup_factors, phase="setup")
    run = _coerce_factors(run_factors, phase="run")
    if not run:
        raise DoEError("at least one RUN factor (with >=1 level) is required — "
                       "an experiment must vary something to measure")

    setup_consts = _constants_map(setup_constants, phase="setup")
    run_consts = _constants_map(run_constants, phase="run")

    setup_rows = _expand(setup)
    run_rows = _expand(run)

    experiment_meta: dict[str, Any] = {"name": name}
    if harness is not None:
        experiment_meta["harness"] = harness
    if profile is not None:
        experiment_meta["profile"] = profile
    if description is not None:
        experiment_meta["description"] = description

    document: dict[str, Any] = {"experiment": experiment_meta}

    # ``design`` is informational DoE metadata that mirrors the repo's example files.
    # Run-phase constants live here (under ``design.run.constants``, list-of-{key,value})
    # because the upstream format has NO top-level ``run`` key — emitting one would fail
    # structural validation against the repo's own experiment examples. ``design`` IS a
    # top-level key the examples use, so this placement is example-/parser-compatible.
    if run_consts:
        document["design"] = {
            "run": {"constants": [{"key": k, "value": v} for k, v in run_consts.items()]}
        }

    if setup_rows:
        setup_block: dict[str, Any] = {}
        if setup_consts:
            # ``setup.constants`` (a dotted-key → value mapping) IS consumed by the parser:
            # it is merged into every setup treatment's overrides. Kept as a mapping for that
            # reason (distinct from the informational list form used for run constants).
            setup_block["constants"] = setup_consts
        setup_block["treatments"] = setup_rows
        document["setup"] = setup_block

    # Run treatments live under the ``treatments`` key (the parser also accepts ``run`` as a
    # list); ``treatments`` is the canonical form used by the repo's own example files.
    document["treatments"] = run_rows

    return DoEResult(document=document, setup_treatments=setup_rows, run_treatments=run_rows)
