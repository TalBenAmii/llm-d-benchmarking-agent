"""QA-fix infra tests: the runner makes a timeout a first-class, distinguishable signal.

Findings real-1 00:15 / real-2 08:10: kubectl probes blew past the deadline and the tool
continued on incomplete data, silently treating an empty/absent result as success. The runner
already flagged ``timed_out`` + ``exit_code == -1``, but a caller reading only ``.output`` saw
an empty string indistinguishable from a clean empty success. The runner now (a) appends a
TIMEOUT_MARKER line to the captured output so even ``.output``-only readers can tell, and
(b) records the ``deadline_s`` it was bounded by so a timeout is fully self-describing.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import get_settings
from app.security.runner import TIMEOUT_MARKER, CommandRunner, RunResult, SimRunner


def _runner() -> CommandRunner:
    return CommandRunner(get_settings().repo_paths)


async def test_timeout_is_distinguishable_from_empty_success(tmp_path):
    """A command that exceeds its deadline → timed_out, exit_code -1, marker in output, deadline_s set."""
    runner = _runner()
    # `sleep 5` with a 0.3s deadline reliably times out without depending on a cluster.
    # `sleep` resolves via PATH (no allowlist needed at the runner layer — the runner trusts
    # a pre-validated argv; we exercise the timeout machinery directly).
    res = await runner.execute(["sleep", "5"], entry=None, timeout=0.3)

    assert res.timed_out is True
    # A timeout never exits 0 — the child is SIGKILLed (returncode is negative, e.g. -9), or -1
    # if it never reaped. The point is it is NOT a clean 0 exit.
    assert res.exit_code != 0
    assert res.deadline_s == 0.3
    # The marker makes the timeout visible to a caller that only inspects .output. Crucially,
    # this is NOT an empty string (which an empty-success would produce).
    assert TIMEOUT_MARKER in res.output
    assert res.output.strip() != ""


async def test_clean_run_has_no_marker_and_carries_deadline(tmp_path):
    """A normal fast command: not timed out, no marker, deadline recorded for self-description."""
    runner = _runner()
    res = await runner.execute(["true"], entry=None, timeout=10.0)

    assert res.timed_out is False
    assert res.exit_code == 0
    assert res.deadline_s == 10.0
    assert TIMEOUT_MARKER not in res.output


async def test_empty_success_is_truly_empty(tmp_path):
    """Empty-success (ran, produced nothing) stays an EMPTY output — the contrast the marker guards."""
    runner = _runner()
    # `true` prints nothing and exits 0.
    res = await runner.execute(["true"], entry=None, timeout=10.0)
    assert res.output == ""  # genuinely empty, no marker — distinguishable from the timeout case


async def test_sim_runner_carries_deadline(tmp_path):
    """SimRunner never times out but reports the deadline it was given for signature parity."""
    sim = SimRunner({})
    res = await sim.execute(["kubectl", "get", "pods"], entry=None, timeout=25.0)
    assert isinstance(res, RunResult)
    assert res.timed_out is False
    assert res.deadline_s == 25.0
    assert TIMEOUT_MARKER not in res.output
