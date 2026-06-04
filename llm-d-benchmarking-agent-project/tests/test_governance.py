"""Phase 13 — allowlist governance: per-command timeouts + usage quotas (policy as DATA).

These tests assert the ACCEPTANCE criteria, all hermetically (no live cluster, no network,
no GPU, no long real sleeps — a tiny timeout kills a would-be-long command in well under a
second; quotas use a counter; the per-day window uses a fake clock):

  (a) a trivially-short fake command with a tiny ``timeout_s`` is KILLED and reported as a
      timeout;
  (b) the timeout value is honored FROM the YAML, not a Python constant (changing only the
      YAML number changes the deadline);
  (c) a command with ``per_session`` quota N is refused on call N+1 within a session;
  (d) a malformed allowlist RAISES at load.

Plus structural guarantees: the old ``app/tools/execute.py::_TIMEOUTS`` dict is gone, and the
real shipped allowlist sources llmdbenchmark subcommand timeouts purely from its YAML.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from app.config import get_settings
from app.security.allowlist import Allowlist, AllowlistError
from app.security.quota import QuotaCounter, QuotaExceeded
from app.security.runner import CommandRunner
from app.tools.context import QuotaError, ToolContext

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST_PATH = PROJECT_ROOT / "security" / "allowlist.yaml"


# A throwaway executable so the timeout/quota numbers under test come ONLY from the policy
# data we hand in — never from any production constant. argv[0] is the real interpreter so
# the runner (which uses shutil.which / the literal path) can actually launch it.
def _policy_with(*, timeout_s=None, quota=None, mode="read_only", with_positional=False):
    # ``with_positional`` adds one required, unconstrained positional so the timeout tests
    # can pass a fake script-FILE path that the real interpreter actually RUNS (executed as
    # ``[python, scriptpath]``). The path token carries no shell metacharacters. Validate-
    # only and quota tests use bare argv (no positional).
    entry = {"flat": True, "mode": mode}
    if with_positional:
        entry["positionals"] = [{}]
    if timeout_s is not None:
        entry["timeout_s"] = timeout_s
    if quota is not None:
        entry["quota"] = quota
    return Allowlist({"executables": {sys.executable: entry}})


def _sleep_argv(tmp_path: Path, seconds: float) -> list[str]:
    """A fake command: a script file that closes stdout then sleeps. Running it via a FILE
    path (not ``-c "..."``) keeps every argv token free of the shell metacharacters the
    allowlist screen rejects, so it can flow through the real ToolContext gate AND be
    executed by the real interpreter as ``[python, scriptpath]``. Closing stdout first
    mirrors the runner's whole-lifecycle-bound test: it proves the DEADLINE (not just stdout
    drain) is what kills the process."""
    script = tmp_path / f"sleep_{str(seconds).replace('.', '_')}.py"
    script.write_text(f"import os, time\nos.close(1)\ntime.sleep({seconds})\n")
    return [sys.executable, str(script)]


# ---- (d) malformed allowlist is rejected AT LOAD ---------------------------

@pytest.mark.parametrize("bad_entry, needle", [
    ({"flat": True, "timeout_s": -1}, "positive integer"),
    ({"flat": True, "timeout_s": 0}, "positive integer"),
    ({"flat": True, "timeout_s": True}, "positive integer"),     # bool is not an int here
    ({"flat": True, "timeout_s": 1.5}, "positive integer"),
    ({"flat": True, "timeout_s": "fast"}, "positive integer"),
    ({"flat": True, "quota": []}, "must be a mapping"),
    ({"flat": True, "quota": {}}, "at least one"),
    ({"flat": True, "quota": {"per_week": 3}}, "unknown key"),
    ({"flat": True, "quota": {"per_session": 0}}, "positive integer"),
    ({"flat": True, "quota": {"per_day": -2}}, "positive integer"),
])
def test_malformed_allowlist_rejected_at_load(bad_entry, needle):
    with pytest.raises(AllowlistError) as ei:
        Allowlist({"executables": {"foo": bad_entry}})
    assert needle in str(ei.value)


def test_malformed_subcommand_governance_rejected_at_load():
    policy = {"executables": {"tool": {"subcommands": {"go": {"mode": "mutating", "timeout_s": -5}}}}}
    with pytest.raises(AllowlistError) as ei:
        Allowlist(policy)
    assert "timeout_s" in str(ei.value) and "positive integer" in str(ei.value)


def test_well_formed_governance_loads_cleanly():
    al = _policy_with(timeout_s=42, quota={"per_session": 3, "per_day": 9})
    d = al.validate([sys.executable])
    assert d.allowed and d.timeout_s == 42
    assert d.quota_per_session == 3 and d.quota_per_day == 9 and d.quota_key == sys.executable


def test_real_shipped_allowlist_loads():
    # The actual policy file must pass the same startup schema validation.
    Allowlist.from_file(ALLOWLIST_PATH)


# ---- governance flows from YAML into the Decision --------------------------

def test_decision_carries_timeout_from_data():
    d = _policy_with(timeout_s=7).validate([sys.executable])
    assert d.timeout_s == 7
    assert _policy_with().validate([sys.executable]).timeout_s is None  # absent -> None


def test_subcommand_timeout_overrides_executable():
    policy = {"executables": {"tool": {
        "timeout_s": 100,
        "subcommands": {
            "slow": {"mode": "read_only", "timeout_s": 999},
            "inherit": {"mode": "read_only"},
        },
    }}}
    al = Allowlist(policy)
    assert al.validate(["tool", "slow"]).timeout_s == 999      # subcommand wins
    assert al.validate(["tool", "inherit"]).timeout_s == 100   # falls back to executable


def test_shipped_llmdbenchmark_timeouts_come_from_yaml(catalog):
    """The per-subcommand budgets that USED to live in _TIMEOUTS now ride on the Decision,
    sourced from the YAML alone."""
    al = Allowlist.from_file(ALLOWLIST_PATH)
    # `results` is now the git-like Results Store group (Phase 50): it REQUIRES a store-command
    # (mirroring upstream argparse `results_command` required=True), so it is exercised via
    # `results status` and carries the `results` entry's 600s budget on the Decision.
    expect = {
        "plan": ("plan", 300), "standup": ("standup", 3600), "smoketest": ("smoketest", 900),
        "run": ("run", 3600), "teardown": ("teardown", 900),
        "results": ("results status", 600), "experiment": ("experiment", 14400),
    }
    for sub, (tail, secs) in expect.items():
        argv = ["llmdbenchmark", "--spec", "cicd/kind", *tail.split()]
        d = al.validate(argv, catalog=catalog)
        assert d.allowed, (sub, d.reason)
        assert d.timeout_s == secs, f"{sub}: expected {secs}, got {d.timeout_s}"


# ---- (a)+(b) the timeout is ENFORCED, and it comes from the YAML number -----

async def test_tiny_yaml_timeout_kills_command_and_reports_timeout(tmp_path):
    """(a) A would-be-long fake command with a tiny ``timeout_s`` is killed and flagged as a
    timeout — driven through the real ToolContext.run_readonly path, deadline sourced from
    the Decision (i.e. from the policy), not a caller override."""
    al = _policy_with(timeout_s=1, with_positional=True)  # 1 second cap declared in DATA
    ctx = ToolContext(settings=get_settings(), allowlist=al,
                      runner=CommandRunner({}), workspace=tmp_path / "ws")
    start = time.monotonic()
    # No timeout= override: the deadline MUST come from the policy's timeout_s.
    res = await ctx.run_readonly(_sleep_argv(tmp_path, 30))
    elapsed = time.monotonic() - start
    assert res.timed_out is True
    assert elapsed < 5.0, f"the 1s YAML timeout was not enforced (took {elapsed:.1f}s)"


async def test_timeout_value_is_honored_from_yaml_not_a_constant(tmp_path):
    """(b) The deadline tracks the YAML number: a command that finishes in ~0.2s survives
    under a 5s policy timeout, but the SAME command is killed under a 1s policy timeout. The
    only thing that changed is the data, proving there is no Python constant in the path."""
    ctx_generous = ToolContext(
        settings=get_settings(), allowlist=_policy_with(timeout_s=5, with_positional=True),
        runner=CommandRunner({}), workspace=tmp_path / "a")
    res_ok = await ctx_generous.run_readonly(_sleep_argv(tmp_path, 0.2))
    assert res_ok.timed_out is False and res_ok.exit_code == 0

    ctx_tight = ToolContext(
        settings=get_settings(), allowlist=_policy_with(timeout_s=1, with_positional=True),
        runner=CommandRunner({}), workspace=tmp_path / "b")
    start = time.monotonic()
    res_killed = await ctx_tight.run_readonly(_sleep_argv(tmp_path, 30))
    assert res_killed.timed_out is True
    assert time.monotonic() - start < 5.0


async def test_caller_timeout_does_not_override_policy(tmp_path):
    """A declared policy timeout_s SUPERSEDES a caller fallback — there is one source of
    truth (the data). A long caller fallback cannot rescue a command past its YAML deadline."""
    ctx = ToolContext(
        settings=get_settings(), allowlist=_policy_with(timeout_s=1, with_positional=True),
        runner=CommandRunner({}), workspace=tmp_path / "ws")
    start = time.monotonic()
    res = await ctx.run_readonly(_sleep_argv(tmp_path, 30), timeout=60.0)  # fallback ignored
    assert res.timed_out is True
    assert time.monotonic() - start < 5.0


async def test_no_policy_timeout_uses_caller_fallback(tmp_path):
    """When the policy declares no timeout_s, the caller's fallback (here run_readonly's tiny
    probe default via an explicit value) applies — the runner's global default is the last
    resort, exercised structurally elsewhere."""
    ctx = ToolContext(
        settings=get_settings(), allowlist=_policy_with(with_positional=True),  # no timeout_s
        runner=CommandRunner({}), workspace=tmp_path / "ws")
    start = time.monotonic()
    res = await ctx.run_readonly(_sleep_argv(tmp_path, 30), timeout=1.0)
    assert res.timed_out is True
    assert time.monotonic() - start < 5.0


# ---- (c) per_session quota refuses the (N+1)-th call PRE-execution ----------

class _CountingRunner(CommandRunner):
    """Records how many times a command actually reached execution (i.e. was NOT refused)."""

    def __init__(self):
        super().__init__({})
        self.runs = 0

    async def execute(self, logical_argv, entry, *, on_line=None, timeout=None, cwd=None, extra_env=None):
        self.runs += 1
        from app.security.runner import RunResult
        return RunResult(exit_code=0, duration_s=0.0, real_argv=list(logical_argv), cwd=None)


async def test_per_session_quota_refuses_call_n_plus_1(tmp_path):
    runner = _CountingRunner()
    al = _policy_with(quota={"per_session": 3})
    ctx = ToolContext(settings=get_settings(), allowlist=al, runner=runner,
                      workspace=tmp_path / "ws")
    # The first N=3 calls run.
    for i in range(3):
        res = await ctx.run_readonly([sys.executable])
        assert res.exit_code == 0, f"call {i + 1} should have run"
    assert runner.runs == 3
    # Call N+1 is refused BEFORE execution.
    with pytest.raises(QuotaError) as ei:
        await ctx.run_readonly([sys.executable])
    err = ei.value
    assert err.window == "per_session" and err.cap == 3 and err.used == 3
    assert err.key == sys.executable
    assert runner.runs == 3, "the over-quota call must NOT have reached the runner"


async def test_quota_refusal_happens_before_approval_prompt(tmp_path):
    """A mutating command over its session quota is refused pre-exec WITHOUT prompting the
    user (the spec: refuse BEFORE execution; the gate never opens)."""
    runner = _CountingRunner()
    al = Allowlist({"executables": {sys.executable: {
        "flat": True, "mode": "mutating", "quota": {"per_session": 1}}}})
    approvals: list[dict] = []

    async def approve(kind, payload):
        approvals.append(payload)
        return True

    ctx = ToolContext(settings=get_settings(), allowlist=al, runner=runner,
                      workspace=tmp_path / "ws", request_approval=approve)
    await ctx.run_command([sys.executable])          # 1st: approved + runs
    assert len(approvals) == 1 and runner.runs == 1
    with pytest.raises(QuotaError):                  # 2nd: refused before the gate
        await ctx.run_command([sys.executable])
    assert len(approvals) == 1, "over-quota command must not reach the approval prompt"
    assert runner.runs == 1


async def test_command_without_quota_is_uncapped(tmp_path):
    runner = _CountingRunner()
    ctx = ToolContext(settings=get_settings(), allowlist=_policy_with(), runner=runner,
                      workspace=tmp_path / "ws")
    for _ in range(10):
        await ctx.run_readonly([sys.executable])
    assert runner.runs == 10  # no quota declared -> never refused


# ---- the quota COUNTER (mechanism) in isolation ----------------------------

def test_quota_counter_per_session():
    c = QuotaCounter()
    c.check("k", per_session=2, per_day=None)  # 0 used, ok
    c.record("k")
    c.check("k", per_session=2, per_day=None)  # 1 used, ok
    c.record("k")
    with pytest.raises(QuotaExceeded) as ei:    # 2 used -> next would exceed
        c.check("k", per_session=2, per_day=None)
    assert ei.value.window == "per_session" and ei.value.cap == 2


def test_quota_counter_per_day_rolls_with_clock():
    """A fake clock advances the day with no real sleep, exercising the per-day window."""
    day = {"v": "2026-06-02"}
    c = QuotaCounter(now=lambda: day["v"])
    c.record("k")
    c.record("k")
    with pytest.raises(QuotaExceeded) as ei:
        c.check("k", per_session=None, per_day=2)
    assert ei.value.window == "per_day"
    day["v"] = "2026-06-03"                      # next day -> window resets
    c.check("k", per_session=None, per_day=2)   # ok again


def test_quota_counter_is_per_key():
    c = QuotaCounter()
    c.record("a")
    c.record("a")
    c.check("b", per_session=1, per_day=None)   # different key unaffected
    with pytest.raises(QuotaExceeded):
        c.check("a", per_session=2, per_day=None)


# ---- structural: the Python timeout table is GONE --------------------------

def test_execute_timeouts_dict_removed():
    import app.tools.execute as execute_mod
    assert not hasattr(execute_mod, "_TIMEOUTS"), \
        "the hardcoded _TIMEOUTS dict must be gone — timeouts live in the allowlist YAML"
    src = Path(execute_mod.__file__).read_text()
    assert "_TIMEOUTS" not in src
