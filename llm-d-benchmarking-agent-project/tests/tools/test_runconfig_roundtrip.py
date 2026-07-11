"""Phase 42 — run-config round-trip (--generate-config / -c).

Hermetic, no cluster / GPU / network. Covers the MECHANISM this phase adds — using the CLI's
OWN run-config reuse (in addition to the agent's in-workspace write_and_validate_config). The
generate-vs-reuse-vs-author JUDGMENT lives in knowledge/runconfig_roundtrip.md, not in Python:

  * build_argv emits --generate-config (GENERATE + exit) and -c <path> (REPLAY) on ``run`` ONLY
    (upstream defines both on the run subcommand alone); absent/None/empty => nothing;
  * the allowlist permits both on ``run``: --generate-config is a read_only_trigger (it
    generates-and-exits, deploys nothing, so it auto-runs), while a -c replay STAYS mutating
    (approval-gated) and is value-pinned to a *.yaml/*.yml config path (no `..`); the metachar
    screen still rejects an injection-laden config path;
  * the ExecuteInput schema accepts ``generate_config``/``run_config`` inside ``flags``;
  * the knowledge guide + tool descriptions point the agent at the judgment.

ACCEPTANCE: the agent can generate a run-config with the CLI (--generate-config) and replay it
via -c — both expressible through build_argv and both permitted by the allowlist.
"""
from __future__ import annotations

import pytest

from app.security.allowlist import MUTATING, READ_ONLY
from app.tools.run.execute import build_argv
from app.tools.schemas import ExecuteInput

# A workspace-relative run-config path of the shape --generate-config writes under --workspace.
RUN_CONFIG = "results/cicd-kind/run-config.yaml"


# ---------------------------------------------------------------------------
# build_argv — run-only emission of --generate-config and -c (PURE MECHANISM)
# ---------------------------------------------------------------------------


def test_generate_config_emits_flag_on_run():
    argv = build_argv("run", spec="cicd/kind", flags={"generate_config": True})
    assert "--generate-config" in argv


def test_run_config_emits_dash_c_with_path_on_run():
    argv = build_argv("run", flags={"run_config": RUN_CONFIG})
    assert "-c" in argv
    # -c is immediately followed by the exact path the agent chose (verbatim, no mutation).
    assert argv[argv.index("-c") + 1] == RUN_CONFIG


@pytest.mark.parametrize("subcommand", ["standup", "plan", "smoketest", "teardown", "experiment"])
def test_roundtrip_omitted_on_non_run(subcommand):
    # Upstream --generate-config and -c/--config are accepted ONLY on `run`; we never emit them
    # on any other subcommand, even if the agent mistakenly set the flags.
    argv = build_argv(
        subcommand, spec="cicd/kind",
        flags={"generate_config": True, "run_config": RUN_CONFIG},
    )
    assert "--generate-config" not in argv
    assert "-c" not in argv
    assert RUN_CONFIG not in argv


@pytest.mark.parametrize("subcommand", ["run", "standup", "plan"])
def test_roundtrip_unset_emits_nothing(subcommand):
    # No keys (or None/empty/False) => never inject --generate-config or -c.
    for flags in (
        {},
        {"generate_config": False, "run_config": None},
        {"generate_config": None, "run_config": ""},
    ):
        argv = build_argv(subcommand, spec="cicd/kind", flags=flags)
        assert "--generate-config" not in argv
        assert "-c" not in argv


def test_roundtrip_does_not_disturb_other_flags():
    # Generate alongside the normal run-shaping flags: the config is generated FROM these settings.
    argv = build_argv(
        "run", spec="cicd/kind", harness="inference-perf", workload="sanity_random.yaml",
        models="facebook/opt-125m",
        flags={"generate_config": True, "output": "local", "monitoring": True},
    )
    assert argv[:3] == ["llmdbenchmark", "--spec", "cicd/kind"]
    assert "--generate-config" in argv
    # the round-trip rides ALONGSIDE the existing flags, not in place of them
    assert "-l" in argv and "inference-perf" in argv
    assert "-w" in argv and "sanity_random.yaml" in argv
    assert "-m" in argv and "facebook/opt-125m" in argv
    assert "-r" in argv and "local" in argv
    assert "--monitoring" in argv


def test_replay_argv_is_minimal():
    # A replay can be as small as `run -p <ns> -c <path>`: harness/workload/model come from the
    # saved config, so they need not be re-specified.
    argv = build_argv("run", namespace="llmdbench", flags={"run_config": RUN_CONFIG})
    assert argv == ["llmdbenchmark", "run", "-p", "llmdbench", "-c", RUN_CONFIG]


def test_execute_schema_accepts_roundtrip_flags():
    m = ExecuteInput(
        subcommand="run", spec="cicd/kind",
        flags={"generate_config": True, "run_config": RUN_CONFIG},
    )
    assert m.flags == {"generate_config": True, "run_config": RUN_CONFIG}


# ---------------------------------------------------------------------------
# allowlist — both flags permitted on `run` (DATA); modes differ by intent
# ---------------------------------------------------------------------------


def _argv(*rest):
    return ["llmdbenchmark", "--spec", "cicd/kind", "run", *rest]


def test_allowlist_generate_config_auto_runs_read_only(allowlist, catalog):
    # --generate-config generates-and-exits (deploys nothing) => it downgrades the run to a
    # read-only AUTO-RUN, exactly like --dry-run / --list-endpoints.
    d = allowlist.validate(
        _argv("-p", "llmdbench", "-l", "inference-perf", "-w", "sanity_random.yaml",
              "--generate-config"),
        catalog=catalog,
    )
    assert d.allowed, f"--generate-config should be allowed on run: {d.reason}"
    assert d.mode == READ_ONLY


@pytest.mark.parametrize("flag", ["-c", "--config"])
def test_allowlist_replay_permitted_and_stays_mutating(allowlist, catalog, flag):
    # A -c/--config replay executes the load against an existing stack => it STAYS mutating
    # (approval-gated); it is NOT a read_only_trigger.
    d = allowlist.validate(_argv("-p", "llmdbench", flag, RUN_CONFIG), catalog=catalog)
    assert d.allowed, f"{flag} should be allowed on run: {d.reason}"
    assert d.mode == MUTATING


def test_allowlist_run_config_value_constraint_accepts_yaml_paths(allowlist, catalog):
    for path in (
        "run-config.yaml",
        "results/cicd-kind/run-config.yaml",
        "workspace/sess-123/run-config.yml",
    ):
        d = allowlist.validate(_argv("-p", "llmdbench", "-c", path), catalog=catalog)
        assert d.allowed, f"run-config path {path!r} should pass the value constraint: {d.reason}"


def test_allowlist_rejects_non_yaml_and_traversal_run_config(allowlist, catalog):
    for bad in (
        "run-config.txt",          # not a yaml
        "../../etc/passwd.yaml",   # directory-climbing escape
        "/etc/secret",             # no .yaml extension
    ):
        d = allowlist.validate(_argv("-p", "llmdbench", "-c", bad), catalog=catalog)
        assert not d.allowed, f"run-config path {bad!r} must be refused"


def test_allowlist_rejects_injection_laden_run_config(allowlist, catalog):
    # A metachar-laden config path is rejected by the blanket screen (defense in depth),
    # even though the *.yaml/no-`..` constraint would also reject it.
    d = allowlist.validate(
        _argv("-p", "llmdbench", "-c", "run-config.yaml;$(rm -rf /)"), catalog=catalog
    )
    assert not d.allowed


def test_roundtrip_in_one_session_acceptance(allowlist, catalog):
    # ACCEPTANCE end-to-end at the argv/allowlist boundary: generate (auto-run, read-only) THEN
    # replay (approval-gated, mutating) — both expressible and both permitted.
    gen = build_argv(
        "run", spec="cicd/kind", namespace="llmdbench", harness="inference-perf",
        workload="sanity_random.yaml", flags={"generate_config": True, "output": "local"},
    )
    gd = allowlist.validate(gen, catalog=catalog)
    assert gd.allowed and gd.mode == READ_ONLY

    replay = build_argv("run", namespace="llmdbench", flags={"run_config": RUN_CONFIG})
    rd = allowlist.validate(replay, catalog=catalog)
    assert rd.allowed and rd.mode == MUTATING


# ---------------------------------------------------------------------------
# the JUDGMENT is in knowledge, and the agent is pointed at it
# ---------------------------------------------------------------------------


def test_runconfig_roundtrip_knowledge_is_discoverable():
    from pathlib import Path

    kfile = Path(__file__).resolve().parent.parent.parent / "knowledge" / "run/runconfig_roundtrip.md"
    assert kfile.is_file(), "knowledge/runconfig_roundtrip.md must exist (auto-indexed by glob)"
    text = kfile.read_text()
    # It must actually carry the generate-vs-reuse-vs-author judgment, not just name the flags.
    assert "--generate-config" in text
    assert "-c" in text
    assert "write_and_validate_config" in text  # contrasts the in-workspace authoring path


def test_execute_schema_description_points_at_runconfig_knowledge():
    desc = ExecuteInput.model_fields["flags"].description or ""
    assert "generate_config" in desc
    assert "run_config" in desc
    assert "runconfig_roundtrip" in desc
