"""Phase 38 — the CLI's OWN per-phase timeout flags (``--*-timeout``).

Hermetic: no cluster / GPU / network / real run. Covers the MECHANISM this phase adds
(the WHEN/WHAT-to-set + two-layer reconcile JUDGMENT lives in knowledge/phase_timeouts.md,
not in Python):

  * build_argv emits each per-phase timeout flag as ``--<name>-timeout <seconds>`` ONLY on the
    upstream-accepting subcommand(s) — an out-of-place key emits nothing — and passes the int
    value through verbatim (no if/elif on the value);
  * the allowlist permits each ``--*-timeout`` flag (value-pinned to a positive integer) under
    exactly those subcommands, and refuses a non-integer / injection-laden value;
  * the per-phase flag does NOT change a command's mutating classification (standup/run/teardown
    stay approval-gated), and the runner deadline (allowlist ``timeout_s``) still bounds the whole
    process and stays the OUTER ceiling the per-phase value must stay below (the reconcile);
  * the ExecuteInput schema accepts the timeout keys inside ``flags``;
  * the knowledge guide + tool description point the agent at the judgment.
"""
from __future__ import annotations

import pytest

from app.security.allowlist import MUTATING, READ_ONLY
from app.tools.run.execute import _PHASE_TIMEOUT_FLAGS, build_argv
from app.tools.schemas import ExecuteInput
from tests._helpers import _argv

# (flags-key, CLI flag, accepting subcommands) — the authoritative mapping this phase introduces.
# Verified against llm-d-benchmark/llmdbenchmark/interface/{standup,run,experiment,teardown}.py.
_EXPECTED = [
    ("standalone_deploy_timeout", "--standalone-deploy-timeout", ("standup",)),
    ("gateway_deploy_timeout", "--gateway-deploy-timeout", ("standup",)),
    ("modelservice_deploy_timeout", "--modelservice-deploy-timeout", ("standup",)),
    ("kustomize_deploy_timeout", "--kustomize-deploy-timeout", ("standup",)),
    ("pvc_bind_timeout", "--pvc-bind-timeout", ("standup",)),
    ("wait_timeout", "--wait-timeout", ("run", "experiment")),
    ("data_access_timeout", "--data-access-timeout", ("run", "experiment")),
    ("fma_teardown_timeout", "--fma-teardown-timeout", ("teardown",)),
]

_ALL_SUBCOMMANDS = ["plan", "standup", "smoketest", "run", "teardown", "results", "experiment"]


def test_table_matches_expected_mapping():
    # The static mechanism table is exactly the verified upstream mapping (no drift, no extras).
    assert {k: v for k, (cli, sub) in _PHASE_TIMEOUT_FLAGS.items() for v in [(cli, sub)]} == {
        k: (cli, sub) for k, cli, sub in _EXPECTED
    }


# ---------------------------------------------------------------------------
# build_argv — subcommand-aware per-phase timeout emission (PURE MECHANISM)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key,cli_flag,accepts", _EXPECTED)
def test_timeout_emitted_on_accepting_subcommands(key, cli_flag, accepts):
    for subcommand in accepts:
        argv = build_argv(subcommand, spec="cicd/kind", flags={key: 1800})
        assert cli_flag in argv, f"{cli_flag} must be emitted on {subcommand}"
        # the flag is immediately followed by the exact int value, stringified verbatim
        assert argv[argv.index(cli_flag) + 1] == "1800"


@pytest.mark.parametrize("key,cli_flag,accepts", _EXPECTED)
def test_timeout_omitted_on_non_accepting_subcommands(key, cli_flag, accepts):
    # A timeout key set on a subcommand upstream does NOT accept emits nothing (silently dropped).
    for subcommand in _ALL_SUBCOMMANDS:
        if subcommand in accepts:
            continue
        argv = build_argv(subcommand, spec="cicd/kind", flags={key: 1800})
        assert cli_flag not in argv, f"{cli_flag} must NOT be emitted on {subcommand}"


@pytest.mark.parametrize("key,cli_flag,accepts", _EXPECTED)
def test_timeout_unset_emits_nothing(key, cli_flag, accepts):
    # No key / None => nothing emitted (the CLI's own default stands). 0 is a meaningful value
    # for wait_timeout (= don't wait), so it must still emit when explicitly set to 0.
    for subcommand in accepts:
        for flags in ({}, {key: None}):
            argv = build_argv(subcommand, spec="cicd/kind", flags=flags)
            assert cli_flag not in argv


def test_wait_timeout_zero_is_emitted():
    # wait_timeout=0 means "do not wait" upstream — it is a real value, not an omission, so it
    # MUST emit (the `is not None` guard, not a truthiness guard).
    argv = build_argv("run", spec="cicd/kind", flags={"wait_timeout": 0})
    assert "--wait-timeout" in argv
    assert argv[argv.index("--wait-timeout") + 1] == "0"


def test_multiple_standup_timeouts_compose():
    argv = build_argv(
        "standup",
        spec="cicd/kind",
        flags={
            "pvc_bind_timeout": 600,
            "modelservice_deploy_timeout": 1200,
            "monitoring": True,
        },
    )
    assert argv[argv.index("--pvc-bind-timeout") + 1] == "600"
    assert argv[argv.index("--modelservice-deploy-timeout") + 1] == "1200"
    # rides ALONGSIDE existing flags, not in place of them
    assert "--monitoring" in argv


def test_timeout_does_not_disturb_other_flags():
    argv = build_argv(
        "run", spec="cicd/kind", harness="vllm-benchmark", workload="sanity_random.yaml",
        flags={"wait_timeout": 5400, "data_access_timeout": 300, "output": "local"},
    )
    assert argv[:3] == ["llmdbenchmark", "--spec", "cicd/kind"]
    assert argv[argv.index("--wait-timeout") + 1] == "5400"
    assert argv[argv.index("--data-access-timeout") + 1] == "300"
    assert "-l" in argv and "vllm-benchmark" in argv
    assert "-r" in argv and "local" in argv


def test_execute_schema_accepts_timeout_flags():
    m = ExecuteInput(
        subcommand="standup",
        spec="cicd/kind",
        flags={"pvc_bind_timeout": 600, "modelservice_deploy_timeout": 1200},
    )
    assert m.flags == {"pvc_bind_timeout": 600, "modelservice_deploy_timeout": 1200}


# ---------------------------------------------------------------------------
# allowlist — each --*-timeout permitted (value-pinned positive int) on its subcommand(s) (DATA)
# ---------------------------------------------------------------------------


# Minimal required positional args per subcommand so the validate() is realistic.
_REQUIRED = {
    "standup": (),
    "run": ("-l", "vllm-benchmark", "-w", "sanity_random.yaml"),
    "experiment": ("-e", "exp.yaml"),
    "teardown": (),
}


@pytest.mark.parametrize("key,cli_flag,accepts", _EXPECTED)
def test_allowlist_permits_timeout_on_accepting_subcommands(allowlist, catalog, key, cli_flag, accepts):
    for subcommand in accepts:
        d = allowlist.validate(
            _argv(subcommand, *_REQUIRED[subcommand], cli_flag, "1800"), catalog=catalog
        )
        assert d.allowed, f"{cli_flag} should be allowed on {subcommand}: {d.reason}"
        # a real mutating command stays mutating (approval-gated) — the timeout flag is orthogonal
        assert d.mode == MUTATING


@pytest.mark.parametrize("key,cli_flag,accepts", _EXPECTED)
def test_allowlist_value_pins_timeout_where_declared(allowlist, catalog, key, cli_flag, accepts):
    # Where the flagspec is DECLARED (the accepting subcommand), the positive_int value
    # constraint is enforced — that is the security property of adding the flagspec. Note the
    # allowlist's documented policy is permissive about UNKNOWN flags elsewhere (they pass but
    # their VALUE is unchecked, and mode is never downgraded); build_argv is what guarantees the
    # flag is only ever EMITTED on the accepting subcommand (see the build_argv tests above), so
    # an unconstrained-elsewhere value can never originate from the agent's own argv builder.
    for subcommand in accepts:
        # a valid positive int is accepted...
        ok = allowlist.validate(
            _argv(subcommand, *_REQUIRED[subcommand], cli_flag, "1800"), catalog=catalog
        )
        assert ok.allowed, f"{cli_flag}=1800 should be allowed on {subcommand}: {ok.reason}"
        # ...but a non-integer value is REFUSED by the declared positive_int constraint here.
        bad = allowlist.validate(
            _argv(subcommand, *_REQUIRED[subcommand], cli_flag, "notanumber"), catalog=catalog
        )
        assert not bad.allowed, f"{cli_flag}=notanumber must be refused on {subcommand}"


@pytest.mark.parametrize("key,cli_flag,accepts", _EXPECTED)
def test_allowlist_rejects_non_integer_timeout_value(allowlist, catalog, key, cli_flag, accepts):
    subcommand = accepts[0]
    for bad in ("notanumber", "-5", "30s", "12.5"):
        d = allowlist.validate(
            _argv(subcommand, *_REQUIRED[subcommand], cli_flag, bad), catalog=catalog
        )
        assert not d.allowed, f"{cli_flag} {bad!r} must fail the positive_int constraint"


def test_allowlist_rejects_injection_laden_timeout_value(allowlist, catalog):
    # Defense in depth: a metachar-laden value is rejected by the blanket screen even before the
    # positive_int constraint.
    d = allowlist.validate(
        _argv("standup", "--pvc-bind-timeout", "600$(rm -rf /)"), catalog=catalog
    )
    assert not d.allowed


def test_timeout_flag_keeps_read_only_preview(allowlist, catalog):
    # --dry-run still downgrades a timeout-bearing standup to a read-only preview: the per-phase
    # timeout is orthogonal to the mode classification.
    d = allowlist.validate(
        _argv("standup", "--pvc-bind-timeout", "600", "--dry-run"), catalog=catalog
    )
    assert d.allowed and d.mode == READ_ONLY


# ---------------------------------------------------------------------------
# the RECONCILE: the per-phase timeout is a DEEPER bound; the runner deadline still applies and
# stays the OUTER ceiling the per-phase value must remain below (two layers do not fight)
# ---------------------------------------------------------------------------

# The runner deadline is sourced from the allowlist `timeout_s` per subcommand (Phase 13). These
# are the documented ceilings the knowledge guide tells the agent to stay below.
_RUNNER_CEILING = {"standup": 3600, "run": 3600, "teardown": 900, "experiment": 14400}


@pytest.mark.parametrize("subcommand,ceiling", _RUNNER_CEILING.items())
def test_runner_deadline_still_bounds_the_process(allowlist, catalog, subcommand, ceiling):
    # The Decision carries the runner deadline (timeout_s) regardless of any per-phase CLI flag —
    # the outer asyncio.wait_for ceiling is UNCHANGED by Phase 38. Pick a timeout flag valid here.
    cli_flag = next(c for _, c, acc in _EXPECTED if subcommand in acc)
    # set the per-phase timeout to a sane DEEPER value (below the ceiling), as the guide prescribes
    inner = ceiling - 600
    d = allowlist.validate(
        _argv(subcommand, *_REQUIRED[subcommand], cli_flag, str(inner)), catalog=catalog
    )
    assert d.allowed, d.reason
    # the outer runner deadline is the subcommand's policy ceiling, untouched by the inner flag
    assert d.timeout_s == ceiling
    # and the reconcile the agent is told to honour holds: inner per-phase bound < outer ceiling
    assert inner < d.timeout_s


# ---------------------------------------------------------------------------
# the JUDGMENT is in knowledge, and the agent is pointed at it
# ---------------------------------------------------------------------------


def test_phase_timeouts_knowledge_is_discoverable():
    from pathlib import Path

    kfile = Path(__file__).resolve().parent.parent / "knowledge" / "run/phase_timeouts.md"
    assert kfile.is_file(), "knowledge/phase_timeouts.md must exist (auto-indexed by prompt glob)"
    text = kfile.read_text().lower()
    # It must carry the actual reconcile judgment, not just name the flags.
    assert "below" in text and "ceiling" in text
    assert "runner" in text and "timeout_s" in text
    # every CLI flag it governs is documented
    for _, cli_flag, _ in _EXPECTED:
        assert cli_flag in text, f"{cli_flag} should be documented in the guide"


def test_execute_schema_description_points_at_phase_timeouts_knowledge():
    desc = ExecuteInput.model_fields["flags"].description or ""
    assert "knowledge/phase_timeouts.md" in desc
    assert "below the runner" in desc
