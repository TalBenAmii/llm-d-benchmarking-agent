"""Phase 36 — First-class collect-only / skip-execution mode (``-z`` / ``flags['skip']``).

Hermetic: no cluster / GPU / network. Covers the acceptance criterion — the agent can
re-collect/re-analyze the results of a prior ``run`` WITHOUT re-running the load — across the
three layers it spans:

  * build_argv emits ``-z`` (pure MECHANISM) only when ``flags['skip']`` is truthy, after the
    subcommand, without disturbing the other run args (workspace anchoring, -l/-w/-r);
  * the allowlist permits ``-z``/``--skip`` ONLY under ``run`` (upstream defines them on run
    alone) and classifies a skip-run as READ_ONLY so it AUTO-RUNS (no approval) — it collects
    existing results, it does not load/mutate; the boolean-flag value-abuse screen still bites;
  * the WHEN-to-collect-only JUDGMENT lives in knowledge/collect_only.md (present, discoverable
    by read_knowledge, and points back at the run-only collect-only flow) — not in Python.
"""
from __future__ import annotations

from pathlib import Path

from app.security.allowlist import MUTATING, READ_ONLY
from app.tools.execute import build_argv
from app.tools.probe import read_knowledge
from app.tools.schemas import ExecuteInput
from tests._helpers import _argv

KNOWLEDGE_DIR = Path(__file__).resolve().parents[1] / "knowledge"

# ---------------------------------------------------------------------------
# build_argv — collect-only emission (PURE MECHANISM)
# ---------------------------------------------------------------------------


def test_skip_emits_short_z_on_run():
    argv = build_argv("run", spec="cicd/kind", flags={"skip": True})
    assert "-z" in argv
    # -z is a BARE boolean flag — nothing is consumed after it as a value.
    assert "--skip" not in argv  # we emit the short form (the -m precedent)


def test_skip_falsey_or_absent_emits_nothing():
    # No skip / skip falsey => a normal run; we never inject -z the agent didn't set.
    for flags in ({}, {"skip": False}, {"skip": None}):
        argv = build_argv("run", spec="cicd/kind", flags=flags)
        assert "-z" not in argv


def test_skip_after_subcommand_and_does_not_disturb_other_run_args():
    # -z follows the subcommand; the run still carries -l/-w/-r etc. unperturbed.
    argv = build_argv(
        "run", spec="cicd/kind", harness="inference-perf", workload="sanity_random.yaml",
        flags={"skip": True, "output": "local"},
    )
    assert argv[:3] == ["llmdbenchmark", "--spec", "cicd/kind"]
    assert argv.index("-z") > argv.index("run")
    assert "-l" in argv and "inference-perf" in argv
    assert "-w" in argv and "sanity_random.yaml" in argv
    assert "-r" in argv and "local" in argv


def test_execute_schema_accepts_skip_flag():
    m = ExecuteInput(subcommand="run", spec="cicd/kind", flags={"skip": True})
    assert m.flags == {"skip": True}


# ---------------------------------------------------------------------------
# allowlist — -z/--skip permitted on `run` ONLY, and a skip-run AUTO-RUNS (read-only)
# ---------------------------------------------------------------------------


def _run(*rest):
    # A concrete, otherwise-complete run argv (harness + workload), plus the extra flags.
    return _argv("run", "-l", "inference-perf", "-w", "sanity_random.yaml", *rest)


def test_allowlist_permits_skip_short_and_long_on_run(allowlist, catalog):
    for flag in ("-z", "--skip"):
        d = allowlist.validate(_run(flag), catalog=catalog)
        assert d.allowed, f"{flag} should be allowed on run: {d.reason}"


def test_skip_run_is_read_only_and_auto_runs(allowlist, catalog):
    # The acceptance: a skip-run is collect-only — it reads existing results, it does NOT load
    # or mutate the cluster, so it is classified READ_ONLY and needs no approval (auto-runs),
    # exactly like --list-endpoints / --dry-run downgrade a run.
    d = allowlist.validate(_run("-z"), catalog=catalog)
    assert d.allowed
    assert d.mode == READ_ONLY
    assert d.requires_approval is False
    # The same downgrade holds for the long spelling.
    assert allowlist.validate(_run("--skip"), catalog=catalog).mode == READ_ONLY


def test_skip_only_triggers_the_read_only_downgrade_under_run(allowlist, catalog):
    # Upstream defines -z/--skip on `run` ALONE, so the read_only_trigger is configured under
    # `run` only. The allowlist is permissive on UNKNOWN flags (accepted once the executable +
    # subcommand are allowlisted, metachar-screened) — BUT an unknown flag never downgrades the
    # mode. So a stray -z on a MUTATING standup does NOT collect-only/auto-run it: it stays
    # MUTATING and keeps its approval gate. The collect-only auto-run is scoped to `run`.
    assert allowlist.validate(_argv("standup", "-z"), catalog=catalog).mode == MUTATING
    assert allowlist.validate(_argv("standup", "-z"), catalog=catalog).requires_approval is True
    # plan is already a read-only preview regardless of -z (so -z neither helps nor harms there);
    # experiment is mutating and a stray -z must not downgrade it.
    d_exp = allowlist.validate(_argv("experiment", "-e", "workspace/exp.yaml", "-z"), catalog=catalog)
    assert d_exp.mode == MUTATING and d_exp.requires_approval is True


def test_skip_value_abuse_is_screened(allowlist, catalog):
    # -z is a bare boolean; a metachar-laden trailing token is still rejected by the screen.
    assert not allowlist.validate(_run("-z", "a;rm -rf /"), catalog=catalog).allowed


# ---------------------------------------------------------------------------
# acceptance — re-collect WITHOUT re-running, end to end at the argv level
# ---------------------------------------------------------------------------


def test_recollect_argv_is_a_complete_allowed_readonly_run(allowlist, catalog):
    """The agent re-collects a prior run's results without re-running the load: the SAME run
    argv plus -z. It builds, the allowlist permits it, and it is read-only (auto-runs)."""
    argv = build_argv(
        "run", spec="cicd/kind", harness="inference-perf", workload="sanity_random.yaml",
        flags={"skip": True, "output": "local"},
    )
    assert "-z" in argv
    d = allowlist.validate(argv, catalog=catalog)
    assert d.allowed and d.mode == READ_ONLY and d.requires_approval is False


# ---------------------------------------------------------------------------
# knowledge — the WHEN-to-collect-only JUDGMENT is a discoverable knowledge file, not Python
# ---------------------------------------------------------------------------


def test_collect_only_knowledge_exists_and_describes_the_run_only_flow():
    guide = KNOWLEDGE_DIR / "collect_only.md"
    assert guide.is_file(), "knowledge/collect_only.md must hold the WHEN-to-collect-only judgment"
    text = guide.read_text()
    # It names the mechanism it governs and the run-only constraint + the re-collect intent.
    assert "-z" in text and "skip" in text
    assert "run" in text
    assert "existing results" in text.lower()


def test_collect_only_knowledge_is_loadable_via_read_knowledge(tool_ctx):
    # The judgment doc is auto-discovered by the on-demand knowledge index, so read_knowledge
    # can load it by basename (no prompt.py change is needed for a new knowledge/*.md).
    res = read_knowledge(tool_ctx, name="collect_only")
    assert res.get("topic") == "collect_only"
    assert "error" not in res
    assert "-z" in res["content"]
