"""convert_guide_to_scenario — the agent's WORKSPACE-ONLY variant of upstream's
``skills/convert-guide`` (Phase 53).

Upstream's ``/convert-guide`` reads an llm-d deployment guide (Helm values / kustomize
patches), maps the config to ``LLMDBENCH_*`` environment variables, and CANONICALLY writes
``scenarios/guides/ai.<name>.sh`` (+ an optional ``experiments/ai.<name>.yaml``) INTO the
read-only benchmark repo. This tool does the same emission but writes ONLY into the session
workspace — the sibling repos stay read-only.

Thin-code / thick-agent split (the whole point of this module):

* JUDGMENT — WHICH ``LLMDBENCH_*`` vars a guide maps to, the "standard practices"
  (``DECODE_MODEL_COMMAND=custom``, the ``REPLACE_ENV_*`` placeholders, the preprocess
  command, the ``ai.`` prefix, the default inference-perf / sanity_random.yaml harness) — is
  DATA in ``knowledge/convert_guide.md`` (mirroring upstream ``skills/convert-guide/references/
  mappings.md`` + ``templates.md``). The LLM reads the guide itself (via the read-only
  ``read_repo_doc`` / ``run_shell 'git clone …'`` / its own file reads), resolves the mapping
  using that knowledge, and supplies the already-resolved ``env`` map + chosen harness/profile/
  scenario name to this tool. There is NO mapping ``if/elif`` in this Python.

* MECHANISM — this module only EMITS + VALIDATES:
  1. ``ai.<name>.sh`` — a deterministic shell file of ``export LLMDBENCH_KEY=value`` lines
     (sorted, shell-quoted, each with its optional ``# SOURCE:`` provenance comment), the
     upstream-shaped artifact.
  2. a VALIDATABLE companion ``ai.<name>.yaml`` scenario + ``ai.<name>.spec.yaml`` — by
     REUSING the Phase-45 mechanism in ``app/tools/config_artifact.py`` so the REQUIRED
     "validate via plan/--dry-run" determinism gate actually has a YAML ``--spec`` to target
     (a bare ``.sh`` is not consumable by the allowlisted gate, which takes a YAML ``--spec``
     whose ``scenario_file.path`` is a YAML). The ``.sh`` is the upstream-shaped artifact; the
     YAML+spec is its gate-able twin.

Hard rule: every output path is confined to ``ctx.workspace`` exactly like
``config_artifact.author_scenario`` (workspace mkdir, bare-filename screen rejecting ``/``,
``..``, enforcing the ``ai.<name>.sh`` / ``ai.<name>.yaml`` names). No allowlist change is
needed — file writes are not commands; the agent previews the authored YAML via the EXISTING,
already-allowlisted ``execute_llmdbenchmark(subcommand="plan", spec=<spec_path>,
flags={"dry_run": True})`` route.
"""
from __future__ import annotations

import re
import shlex
from typing import Any

from app.tools.config_artifact import author_scenario
from app.tools.context import ToolContext, ToolError

# A scenario/guide name token: letters/digits and the three separators upstream's ``ai.`` file
# names tolerate. No path separators, no ``..`` — the emitted file names are derived from it.
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# Every var the converter emits is a deploy-time knob the benchmark framework reads. Upstream's
# scenario .sh files set ONLY ``LLMDBENCH_*`` exports; we hold the agent to that so a stray,
# non-namespaced (and therefore inert) export can't be smuggled into the emitted file.
_ENV_PREFIX = "LLMDBENCH_"

# The harness/profile knobs upstream records in the scenario .sh (see knowledge/convert_guide.md
# "Benchmark framework defaults"). Defaulted here as MECHANISM only — the agent overrides them
# per the guide/user; the DEFAULT VALUES themselves are the upstream documented defaults.
_HARNESS_VAR = "LLMDBENCH_HARNESS_NAME"
_PROFILE_VAR = "LLMDBENCH_HARNESS_EXPERIMENT_PROFILE"
_DEFAULT_HARNESS = "inference-perf"
_DEFAULT_PROFILE = "sanity_random.yaml"


def _validate_name(name: str) -> str:
    """Screen the guide/scenario name token. Pure mechanism — rejects anything that could
    escape the workspace or break the ``ai.<name>.*`` file naming."""
    token = (name or "").strip()
    if not token or not _NAME_RE.match(token):
        raise ToolError(
            "name must be a bare token of letters/digits/_/-/. only (it becomes "
            "ai.<name>.sh / ai.<name>.yaml in the workspace) — no path separators or '..'"
        )
    return token


def _emit_export_lines(
    env: dict[str, str], sources: dict[str, str] | None
) -> list[str]:
    """Emit the ``export LLMDBENCH_KEY=value`` body for the scenario .sh, sorted and
    shell-quoted, each optionally preceded by its ``# SOURCE:`` provenance comment.

    Pure mechanism: the agent already resolved WHICH keys/values belong here (per
    knowledge/convert_guide.md); we only render them deterministically. Values are quoted with
    ``shlex.quote`` so a value carrying spaces/quotes/metacharacters cannot break the line or
    inject shell — the emitted file is a faithful, safe ``export`` of exactly what was supplied.
    """
    src = sources or {}
    lines: list[str] = []
    for key in sorted(env):
        if not key.startswith(_ENV_PREFIX):
            raise ToolError(
                f"env key {key!r} must be a benchmark variable (start with {_ENV_PREFIX!r}); "
                "convert-guide scenario files set only LLMDBENCH_* exports"
            )
        if not isinstance(env[key], str):
            raise ToolError(f"env value for {key!r} must be a string (got {type(env[key]).__name__})")
        comment = src.get(key)
        if isinstance(comment, str) and comment.strip():
            # Keep the provenance comment to a single line (upstream's # SOURCE: trace).
            one_line = " ".join(comment.split())
            lines.append(f"# SOURCE: {one_line}")
        lines.append(f"export {key}={shlex.quote(env[key])}")
    return lines


def _build_scenario_sh(
    *,
    name: str,
    env: dict[str, str],
    sources: dict[str, str] | None,
    source_ref: str | None,
) -> str:
    """Assemble the full ``ai.<name>.sh`` text: a provenance header naming the guide source +
    the agent's resolved ``export LLMDBENCH_*`` lines. Pure mechanism; deterministic."""
    header = [
        f"# {name} — scenario converted from an llm-d guide",
        "# Generated by the llm-d-benchmarking-agent guide converter (workspace-only).",
        "# The LLMDBENCH_* -> guide-value mapping is JUDGMENT: read_knowledge('convert_guide').",
    ]
    if source_ref:
        header.insert(1, f"# Source guide: {source_ref}")
    body = _emit_export_lines(env, sources)
    return "\n".join(header + ["", *body]) + "\n"


def _scenario_twin_content(
    name: str, scenario: dict[str, Any] | None
) -> dict[str, Any]:
    """Build the ``content`` for the VALIDATABLE companion YAML scenario twin, in the exact
    shape ``config_artifact.author_scenario`` expects (a ``name`` + >=1 dotted-path knob).

    When the agent supplies an explicit ``scenario`` override map, it is used verbatim (with
    the scenario item ``name`` forced to this guide's name). Otherwise we derive a MINIMAL,
    structurally-valid twin carrying the scenario name under the repo-known ``model.shortName``
    knob — enough to render + gate via plan/--dry-run. This is naming mechanism, not benchmark
    judgment: the rich per-knob choices live in the ``.sh`` (the upstream artifact) and, when
    the agent wants them gated, in the ``scenario`` override it passes here."""
    if scenario:
        content = dict(scenario)
        content["name"] = name  # the twin's scenario item is THIS guide
        return content
    # Minimal but real: a repo-known knob so validate_scenario_structure passes and the spec
    # renders. ``model.shortName`` is a top-level ``model`` knob the repo's examples use.
    return {"name": name, "model.shortName": name}


async def convert_guide_to_scenario(
    ctx: ToolContext,
    *,
    name: str,
    env: dict[str, str],
    sources: dict[str, str] | None = None,
    scenario: dict[str, Any] | None = None,
    harness: str | None = None,
    profile: str | None = None,
    source_ref: str | None = None,
) -> dict[str, Any]:
    """Author an ``ai.<name>.sh`` scenario file (upstream shape) PLUS a validatable
    ``ai.<name>.yaml`` scenario twin + ``ai.<name>.spec.yaml`` companion spec into the session
    WORKSPACE — never the read-only repo — from a guide the agent has already mapped to
    ``LLMDBENCH_*`` vars using ``knowledge/convert_guide.md``.

    Returns the four output paths plus the gate hint. The agent MUST gate the YAML twin via
    ``execute_llmdbenchmark(subcommand="plan", spec=<spec_path>, flags={"dry_run": True})``
    before any standup.
    """
    token = _validate_name(name)
    if not env:
        raise ToolError(
            "env must be a non-empty map of resolved LLMDBENCH_* -> value entries "
            "(derive it from the guide using read_knowledge('convert_guide'))"
        )

    # Record the chosen harness/profile into the .sh as the upstream-documented knobs, defaulting
    # to inference-perf / sanity_random.yaml. We DON'T clobber an explicit guide value: only fill
    # the var if the agent didn't already supply it in env.
    sh_env = dict(env)
    sh_env.setdefault(_HARNESS_VAR, harness or _DEFAULT_HARNESS)
    sh_env.setdefault(_PROFILE_VAR, profile or _DEFAULT_PROFILE)

    sh_text = _build_scenario_sh(
        name=token, env=sh_env, sources=sources, source_ref=source_ref
    )

    # Confine every write to the workspace — same discipline as config_artifact.author_scenario.
    ctx.workspace.mkdir(parents=True, exist_ok=True)
    sh_name = f"ai.{token}.sh"
    sh_dest = ctx.workspace / sh_name
    # Defensive: the validated token cannot contain a separator, but assert the resolved path
    # stays inside the workspace before writing (belt-and-suspenders against the read-only repo).
    if ctx.workspace not in sh_dest.resolve().parents and sh_dest.resolve().parent != ctx.workspace.resolve():
        raise ToolError("refusing to write the scenario .sh outside the session workspace")
    sh_dest.write_text(sh_text)

    # Author the VALIDATABLE twin (ai.<name>.yaml) + its companion spec by REUSING the Phase-45
    # mechanism. author_scenario writes ONLY into ctx.workspace and SHAPE-validates against the
    # repo's live scenario examples; it returns the workspace-confined spec_path for the gate.
    yaml_name = f"ai.{token}.yaml"
    twin = author_scenario(
        ctx,
        target_filename=yaml_name,
        content=_scenario_twin_content(token, scenario),
    )
    if not twin.get("valid", False):
        # The validatable twin failed structural validation — surface it so the agent can
        # self-correct its `scenario` override; the .sh is still emitted (upstream artifact).
        return {
            "scenario_sh_path": str(sh_dest),
            "valid": False,
            "errors": twin.get("errors", []),
            "validated_against_examples": twin.get("validated_against_examples", []),
            "note": "the upstream-shaped scenario .sh was written, but the validatable YAML "
                    "twin failed structural validation (see errors). Fix the `scenario` "
                    "override (read_knowledge('convert_guide')) and re-run before any standup.",
        }

    return {
        "scenario_sh_path": str(sh_dest),
        "scenario_yaml_path": twin["path"],
        "spec_path": twin["spec_path"],
        "scenario_name": token,
        "harness": sh_env[_HARNESS_VAR],
        "profile": sh_env[_PROFILE_VAR],
        "env_vars": sorted(sh_env),
        "valid": True,
        "errors": [],
        "validated_against_examples": twin.get("validated_against_examples", []),
        "note": "ALL FOUR outputs are in the session workspace (the repos stay read-only). "
                "ai.<name>.sh is the upstream-shaped scenario; ai.<name>.yaml + .spec.yaml are "
                "its gate-able twin. GATE the twin before any standup: "
                "execute_llmdbenchmark(subcommand='plan', spec='" + twin["spec_path"] + "', "
                "flags={'dry_run': True}). The LLMDBENCH_* mapping JUDGMENT is "
                "read_knowledge('convert_guide').",
    }
