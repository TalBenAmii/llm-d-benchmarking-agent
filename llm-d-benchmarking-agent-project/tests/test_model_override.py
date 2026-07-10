"""Phase 28 — First-class model override (``-m`` / ``ExecuteInput.models``).

Hermetic, no cluster / GPU / network. Covers the three acceptance criteria:

  * a standup (and plan/run/experiment) can run with an explicit ``-m`` model NOT pinned by
    the spec — ``build_argv`` emits it as pure mechanism, the allowlist permits + value-pins it;
  * the capacity pre-flight sees the SAME model — the standup ``models`` id and the
    ``check_capacity(overrides={'model': …})`` id land on the IDENTICAL plan_config path
    (``model.name``), so the pre-flight validates exactly what the standup will deploy;
  * catalog grounding still validates the name — the override is constrained by the
    ``model_id`` value rule + the metacharacter screen (an enumerable on-disk models catalog
    does not exist; HF-config/gated validity is the capacity pre-flight's job, not Python's).

The WHICH-model judgment lives in knowledge/model_override.md (asserted present + linked).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.capacity.planner import _OVERRIDE_PATHS, apply_overrides
from app.security.allowlist import MUTATING, READ_ONLY
from app.tools.run.execute import build_argv
from app.tools.schemas import ExecuteInput
from tests._helpers import _argv

MODEL = "meta-llama/Llama-3.1-8B"  # a model NOT pinned by cicd/kind (which serves opt-125m)

# ---------------------------------------------------------------------------
# build_argv — model override emission (PURE MECHANISM)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("subcommand", ["standup", "plan", "run", "experiment"])
def test_models_emits_short_m_flag_on_every_subcommand(subcommand):
    argv = build_argv(subcommand, spec="cicd/kind", models=MODEL)
    assert "-m" in argv
    # -m is immediately followed by the exact id (a value flag, not a bare boolean).
    assert argv[argv.index("-m") + 1] == MODEL


@pytest.mark.parametrize("subcommand", ["standup", "plan", "run", "experiment"])
def test_models_unset_emits_no_flag(subcommand):
    # No override => the spec's default model stands; we never inject -m the agent didn't set.
    argv_none = build_argv(subcommand, spec="cicd/kind", models=None)
    argv_default = build_argv(subcommand, spec="cicd/kind")
    for argv in (argv_none, argv_default):
        assert "-m" not in argv


def test_models_after_subcommand_and_does_not_disturb_other_args():
    # Global flags (--spec) precede the subcommand; -m follows it, alongside -l/-w/-r.
    argv = build_argv(
        "run", spec="cicd/kind", harness="inference-perf", workload="sanity_random.yaml",
        models=MODEL, flags={"output": "local"},
    )
    assert argv[:3] == ["llmdbenchmark", "--spec", "cicd/kind"]
    assert argv.index("-m") > argv.index("run")
    assert "-l" in argv and "inference-perf" in argv
    assert "-w" in argv and "sanity_random.yaml" in argv
    assert "-r" in argv and "local" in argv
    assert argv[argv.index("-m") + 1] == MODEL


def test_execute_schema_accepts_top_level_models_field():
    m = ExecuteInput(subcommand="standup", spec="cicd/kind", models=MODEL)
    assert m.models == MODEL
    # It is a TOP-LEVEL field (parallel to harness/workload), NOT buried in flags.
    assert m.flags is None


def test_execute_schema_models_defaults_to_none():
    assert ExecuteInput(subcommand="standup", spec="cicd/kind").models is None


# ---------------------------------------------------------------------------
# allowlist — the override is permitted AND its value is pinned (DATA)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "subcommand,short_long,extra",
    [
        ("standup", "--models", []),
        ("plan", "--models", []),
        ("run", "--model", ["-l", "inference-perf", "-w", "sanity_random.yaml"]),
        ("experiment", "--models", ["-e", "workspace/exp.yaml"]),
    ],
)
def test_allowlist_permits_model_override_short_and_long(allowlist, catalog, subcommand, short_long, extra):
    # Both the short -m (emitted by build_argv) and the subcommand's upstream long spelling
    # are allowlisted with a value constraint.
    for flag in ("-m", short_long):
        d = allowlist.validate(_argv(subcommand, *extra, flag, MODEL), catalog=catalog)
        assert d.allowed, f"{flag} should be allowed on {subcommand}: {d.reason}"


def test_model_override_does_not_change_mode_classification(allowlist, catalog):
    # standup stays mutating with -m; plan stays a read-only preview with -m.
    assert allowlist.validate(_argv("standup", "-m", MODEL), catalog=catalog).mode == MUTATING
    assert allowlist.validate(_argv("plan", "-m", MODEL), catalog=catalog).mode == READ_ONLY
    # --dry-run still downgrades a model-override standup to a read-only preview.
    d = allowlist.validate(_argv("standup", "-m", MODEL, "--dry-run"), catalog=catalog)
    assert d.allowed and d.mode == READ_ONLY


def test_model_id_value_constraint_is_pinned(allowlist, catalog):
    # A valid HF id passes the model_id regex; a metachar-laden injection value is REFUSED
    # (the value constraint + the defense-in-depth metacharacter screen both bite).
    assert allowlist.validate(_argv("standup", "-m", "facebook/opt-125m"), catalog=catalog).allowed
    assert not allowlist.validate(
        _argv("standup", "-m", "x; rm -rf /"), catalog=catalog
    ).allowed
    assert not allowlist.validate(
        _argv("standup", "--models", "$(curl evil)"), catalog=catalog
    ).allowed


# ---------------------------------------------------------------------------
# capacity mirror — the pre-flight sees the IDENTICAL model the standup deploys
# ---------------------------------------------------------------------------


def test_capacity_override_targets_same_config_path_as_standup_model():
    # The standup will emit `-m <MODEL>`; the agent must pass the SAME id to
    # check_capacity(overrides={'model': MODEL}). Both must change the model the planner sizes.
    # `model` is a known capacity override key (no schema change was needed for Phase 28).
    assert "model" in _OVERRIDE_PATHS
    plan_config: dict = {"model": {"name": "facebook/opt-125m"}}  # the spec's stock default
    standup_model = build_argv("standup", spec="cicd/kind", models=MODEL)
    emitted = standup_model[standup_model.index("-m") + 1]
    applied = apply_overrides(plan_config, {"model": emitted})
    # The pre-flight now sizes the SAME model the standup will deploy — not the spec default.
    assert plan_config["model"]["name"] == MODEL == emitted
    # A `model` override also syncs `model.huggingfaceId` (real-2 07:15 fix): upstream sizing and
    # the gated-model check prefer `huggingfaceId`, so without this they evaluated the spec default
    # (facebook/opt-125m) instead of the override. Both transparency entries are emitted.
    assert f"model.name = {MODEL!r}" in applied
    assert plan_config["model"]["huggingfaceId"] == MODEL


def test_capacity_override_path_is_model_name():
    # Pin the exact path so a future refactor can't silently desync the standup id from the
    # pre-flight id (they must share model.name).
    assert _OVERRIDE_PATHS["model"] == ("model", "name")


# ---------------------------------------------------------------------------
# knowledge — the which-model JUDGMENT is a knowledge file, not Python
# ---------------------------------------------------------------------------

KNOWLEDGE_DIR = Path(__file__).resolve().parents[1] / "knowledge"


def test_model_override_knowledge_exists_and_pairs_with_capacity():
    guide = KNOWLEDGE_DIR / "run/model_override.md"
    assert guide.is_file(), "knowledge/model_override.md must hold the which-model judgment"
    text = guide.read_text()
    # The non-negotiable rule: the same id goes to the capacity pre-flight.
    assert "check_capacity" in text and "overrides={'model'" in text
    # And capacity.md points back at the override guide so the lockstep rule is discoverable.
    cap = (KNOWLEDGE_DIR / "deploy/capacity.md").read_text()
    assert "model_override.md" in cap and "ExecuteInput.models" in cap
