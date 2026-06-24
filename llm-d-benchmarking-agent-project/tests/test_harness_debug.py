"""Phase 37 — Harness debug mode (``-d``/``--debug``, ``sleep infinity`` / ``flags['debug']``).

Hermetic: no cluster / GPU / network. Covers the acceptance criterion — the agent can launch a
debug harness pod (approval-gated) and explain how to exec into it WITHOUT driving the
interactive shell itself — across the layers it spans:

  * build_argv emits a bare ``-d`` (pure MECHANISM) only when ``flags['debug']`` is truthy AND
    the subcommand is ``run``/``experiment``; it is NEVER emitted on teardown (upstream ``-d`` on
    teardown is ``--deep`` — a DESTRUCTIVE wipe), nor on standup/plan/smoketest; it does not
    disturb the other run args;
  * the allowlist permits ``-d``/``--debug`` under ``run`` AND ``experiment`` only, as a PLAIN
    boolean flag (NO read_only_trigger) — a debug launch creates a REAL pod, so it stays MUTATING
    and approval-gated (unlike collect-only ``-z``); it is deliberately NOT permitted on teardown;
    the boolean-flag value-abuse screen still bites; and there is NO kubectl/oc ``exec`` subcommand
    (the in-pod exec boundary is structurally enforced);
  * the WHEN-to-debug JUDGMENT and the no-drive in-pod boundary live in knowledge/harness_debug.md
    (present, discoverable by read_knowledge, names the run/experiment-only + NOT-teardown rule and
    the manual-exec boundary) — not in Python.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from app.security.allowlist import MUTATING, READ_ONLY
from app.tools.execute import build_argv
from app.tools.probe import read_knowledge
from app.tools.schemas import ExecuteInput
from tests._helpers import _argv

KNOWLEDGE_DIR = Path(__file__).resolve().parents[1] / "knowledge"
ALLOWLIST_PATH = Path(__file__).resolve().parents[1] / "security" / "allowlist.yaml"

# ---------------------------------------------------------------------------
# build_argv — debug emission (PURE MECHANISM), subcommand-guarded
# ---------------------------------------------------------------------------


def test_debug_emits_bare_short_d_on_run_and_experiment():
    for sub in ("run", "experiment"):
        argv = build_argv(sub, spec="cicd/kind", flags={"debug": True})
        assert "-d" in argv, f"{sub} should carry -d"
        # -d is a BARE boolean flag — nothing is consumed after it as a value.
        assert "--debug" not in argv  # we emit the short form (the -m/-z precedent)


def test_debug_NOT_emitted_on_teardown_because_there_d_means_deep():
    # CRITICAL: upstream `-d` on teardown is `--deep` (a DESTRUCTIVE full-namespace wipe), NOT
    # --debug. Emitting -d on a teardown would silently turn a debug request into a deep teardown,
    # so the guard must drop it entirely.
    argv = build_argv("teardown", spec="cicd/kind", flags={"debug": True})
    assert "-d" not in argv
    assert "--debug" not in argv


def test_debug_NOT_emitted_on_other_subcommands():
    # Upstream defines --debug on run/experiment ALONE; anywhere else it is dropped.
    for sub in ("standup", "smoketest", "plan", "results"):
        argv = build_argv(sub, spec="cicd/kind", flags={"debug": True})
        assert "-d" not in argv, f"{sub} must not carry -d"


def test_debug_falsey_or_absent_emits_nothing():
    # No debug / debug falsey => a normal run; we never inject -d the agent didn't set.
    for flags in ({}, {"debug": False}, {"debug": None}):
        argv = build_argv("run", spec="cicd/kind", flags=flags)
        assert "-d" not in argv


def test_debug_after_subcommand_and_does_not_disturb_other_run_args():
    # -d follows the subcommand; the run still carries -l/-w/-r etc. unperturbed.
    argv = build_argv(
        "run", spec="cicd/kind", harness="inference-perf", workload="sanity_random.yaml",
        flags={"debug": True, "output": "local"},
    )
    assert argv[:3] == ["llmdbenchmark", "--spec", "cicd/kind"]
    assert argv.index("-d") > argv.index("run")
    assert "-l" in argv and "inference-perf" in argv
    assert "-w" in argv and "sanity_random.yaml" in argv
    assert "-r" in argv and "local" in argv


def test_debug_does_not_collide_with_deep_short_form_intent():
    # The agent passing debug=True never produces a teardown's -d; and a teardown's own flags are
    # unaffected (no -d sneaks in via debug). Belt-and-suspenders on the destructive-collision risk.
    teardown = build_argv("teardown", spec="cicd/kind", namespace="llm-d", flags={"debug": True})
    assert "-d" not in teardown


def test_execute_schema_accepts_debug_flag():
    m = ExecuteInput(subcommand="run", spec="cicd/kind", flags={"debug": True})
    assert m.flags == {"debug": True}


# ---------------------------------------------------------------------------
# allowlist — -d/--debug permitted on run+experiment ONLY, and a debug launch is MUTATING
# ---------------------------------------------------------------------------


def _run(*rest):
    # A concrete, otherwise-complete run argv (harness + workload), plus the extra flags.
    return _argv("run", "-l", "inference-perf", "-w", "sanity_random.yaml", *rest)


def test_allowlist_permits_debug_short_and_long_on_run(allowlist, catalog):
    for flag in ("-d", "--debug"):
        d = allowlist.validate(_run(flag), catalog=catalog)
        assert d.allowed, f"{flag} should be allowed on run: {d.reason}"


def test_allowlist_permits_debug_short_and_long_on_experiment(allowlist, catalog):
    for flag in ("-d", "--debug"):
        argv = _argv("experiment", "-e", "workspace/exp.yaml", flag)
        d = allowlist.validate(argv, catalog=catalog)
        assert d.allowed, f"{flag} should be allowed on experiment: {d.reason}"


def test_debug_run_is_mutating_and_requires_approval(allowlist, catalog):
    # The acceptance: a debug launch creates REAL harness pods (they sleep), so it MUTATES the
    # cluster — it is NOT a read-only/collect-only flag. So a debug run stays MUTATING and keeps
    # its approval gate (unlike -z/--skip, which downgrades a run to read-only/auto-run).
    for flag in ("-d", "--debug"):
        d = allowlist.validate(_run(flag), catalog=catalog)
        assert d.allowed
        assert d.mode == MUTATING
        assert d.requires_approval is True


def test_debug_experiment_is_mutating_and_requires_approval(allowlist, catalog):
    d = allowlist.validate(_argv("experiment", "-e", "workspace/exp.yaml", "-d"), catalog=catalog)
    assert d.allowed
    assert d.mode == MUTATING
    assert d.requires_approval is True


def test_debug_run_is_not_a_read_only_trigger(allowlist, catalog):
    # Explicitly: -d must NOT downgrade the mode the way -z/--list-endpoints/--dry-run do.
    assert allowlist.validate(_run("-d"), catalog=catalog).mode != READ_ONLY


def test_debug_value_abuse_is_screened(allowlist, catalog):
    # -d is a bare boolean; a metachar-laden trailing token is still rejected by the screen.
    assert not allowlist.validate(_run("-d", "a;rm -rf /"), catalog=catalog).allowed


def test_no_kubectl_or_oc_exec_subcommand_is_allowlisted():
    # The interactive in-pod exec boundary is STRUCTURALLY enforced: there is no `exec` subcommand
    # under kubectl or oc in the allowlist, so the agent literally cannot drive an interactive
    # shell — it can only EXPLAIN how the user does it.
    data = yaml.safe_load(ALLOWLIST_PATH.read_text())
    for exe in ("kubectl", "oc"):
        subs = data["executables"][exe].get("subcommands", {})
        assert "exec" not in subs, f"{exe} must NOT have an `exec` subcommand"


def test_allowlist_debug_flags_have_no_read_only_trigger_on_run_and_experiment():
    # DATA assertion: -d/--debug are declared as PLAIN boolean flags (no read_only_trigger, no
    # takes_value) under run AND experiment, and exist under NEITHER teardown nor standup.
    data = yaml.safe_load(ALLOWLIST_PATH.read_text())
    subs = data["executables"]["llmdbenchmark"]["subcommands"]
    for sub in ("run", "experiment"):
        flags = subs[sub]["flags"]
        for f in ("-d", "--debug"):
            assert f in flags, f"{f} must be allowlisted under {sub}"
            spec = flags[f] or {}
            assert not spec.get("read_only_trigger"), f"{f} on {sub} must NOT be a read_only_trigger"
            assert not spec.get("takes_value"), f"{f} on {sub} must be a bare boolean"
    # Deliberately absent on teardown (there -d is --deep) and on standup.
    for sub in ("teardown", "standup"):
        flags = subs[sub].get("flags") or {}
        assert "-d" not in flags and "--debug" not in flags, f"-d/--debug must NOT be under {sub}"


# ---------------------------------------------------------------------------
# acceptance — launch a debug pod (approval-gated) end to end at the argv level
# ---------------------------------------------------------------------------


def test_debug_launch_argv_is_a_complete_allowed_mutating_run(allowlist, catalog):
    """The agent launches a debug harness pod: a normal run argv plus -d. It builds, the allowlist
    permits it, and it is MUTATING (approval-gated) — a real pod is created (it just sleeps)."""
    argv = build_argv(
        "run", spec="cicd/kind", harness="inference-perf", workload="sanity_random.yaml",
        flags={"debug": True, "output": "local"},
    )
    assert "-d" in argv
    d = allowlist.validate(argv, catalog=catalog)
    assert d.allowed and d.mode == MUTATING and d.requires_approval is True


# ---------------------------------------------------------------------------
# knowledge — the WHEN + the no-drive boundary live in a discoverable knowledge file, not Python
# ---------------------------------------------------------------------------


def test_harness_debug_knowledge_exists_and_describes_the_boundaries():
    guide = KNOWLEDGE_DIR / "harness_debug.md"
    assert guide.is_file(), "knowledge/harness_debug.md must hold the WHEN + no-drive judgment"
    text = guide.read_text()
    lower = text.lower()
    # Names the mechanism and what it does.
    assert "-d" in text and "--debug" in text
    assert "sleep infinity" in lower
    # The run/experiment-only + NOT-teardown (--deep) rule.
    assert "run" in lower and "experiment" in lower and "teardown" in lower
    assert "--deep" in text
    # The hard boundary: explain the exec, never drive the interactive shell.
    assert "exec" in lower
    assert "manual" in lower or "user" in lower
    assert "never" in lower or "do not" in lower


def test_harness_debug_knowledge_is_loadable_via_read_knowledge(tool_ctx):
    # The judgment doc is auto-discovered by the on-demand knowledge index, so read_knowledge can
    # load it by basename (no prompt.py change is needed for a new knowledge/*.md).
    res = read_knowledge(tool_ctx, name="harness_debug")
    assert res.get("topic") == "harness_debug"
    assert "error" not in res
    assert "-d" in res["content"]
    assert "sleep infinity" in res["content"].lower()
