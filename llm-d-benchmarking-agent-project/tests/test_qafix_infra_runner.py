"""QA-fix infra tests: the runner makes a timeout a first-class, distinguishable signal.

Findings real-1 00:15 / real-2 08:10: kubectl probes blew past the deadline and the tool
continued on incomplete data, silently treating an empty/absent result as success. The runner
already flagged ``timed_out`` + ``exit_code == -1``, but a caller reading only ``.output`` saw
an empty string indistinguishable from a clean empty success. The runner now (a) appends a
TIMEOUT_MARKER line to the captured output so even ``.output``-only readers can tell, and
(b) records the ``deadline_s`` it was bounded by so a timeout is fully self-describing.
"""
from __future__ import annotations

import os
import signal

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


async def test_huge_single_line_does_not_crash_the_runner(tmp_path):
    """A subprocess that emits one line longer than asyncio's 64KiB StreamReader limit with no
    trailing newline (a real shape: ``kubectl get … -o json`` of a big object on one line, a
    base64 blob, a minified log line) must NOT crash the runner.

    Before the fix the pump did ``async for raw in proc.stdout`` (== ``readline()``), which
    raises ``ValueError: Separator is not found, and chunk exceed the limit`` once a single
    un-terminated line passes 65536 bytes. That ValueError is in NEITHER the TimeoutError nor
    the CancelledError handler, so it escapes ``execute()`` as a raw exception — degrading a
    benign giant line into an opaque tool crash — AND skips ``_kill_process_group``, leaking
    the child.
    """
    runner = _runner()
    big = 200_000  # comfortably over the 65_536 limit, and over _MAX_CAPTURE_CHARS too
    prog = f"import sys; sys.stdout.write('A' * {big}); sys.stdout.flush()"
    # `python3` resolves via PATH; the runner trusts a pre-validated argv at this layer.
    res = await runner.execute(["python3", "-c", prog], entry=None, timeout=30.0)

    assert isinstance(res, RunResult)
    assert res.timed_out is False
    assert res.exit_code == 0
    # The giant line is captured (tail-bounded), proving the pump read past the 64KiB limit
    # instead of throwing it away with a ValueError.
    assert "A" in res.output
    assert len(res.output) > 65_536


async def test_huge_line_then_alive_child_is_reaped_not_leaked(tmp_path):
    """The leak half of the bug: a child that prints a >64KiB un-terminated line and then STAYS
    ALIVE must still be reaped (its process group SIGKILLed), never orphaned.

    Before the fix the over-limit ValueError escaped before any kill ran, leaving the child
    (and its session/process group) running while the server kept serving.
    """
    runner = _runner()
    big = 200_000
    # Print the huge line, then block forever on stdin so the child outlives the pump unless
    # the runner kills it. (`sys.stdin.read()` never returns — the parent keeps the pipe open.)
    prog = (
        f"import sys; sys.stdout.write('A' * {big}); sys.stdout.flush(); "
        "sys.stdin.read()"
    )
    res = await runner.execute(["python3", "-c", prog], entry=None, timeout=30.0)

    assert isinstance(res, RunResult)
    # The child was reaped: a real exit code (a clean exit or a kill signal), never the -1
    # "never reaped" sentinel, and the runner returned a clean result rather than raising.
    # The runner doesn't surface the pid, so probe the result shape + assert no orphan remains.
    # `pgrep`-style check: the unique marker string in argv must no longer match a live process.
    import subprocess

    alive = subprocess.run(
        ["pgrep", "-f", f"'A' \\* {big}"],
        capture_output=True,
        text=True,
    ).stdout.split()
    # Belt-and-suspenders cleanup if the assert below is going to fail.
    for p in alive:
        with __import__("contextlib").suppress(Exception):
            os.killpg(os.getpgid(int(p)), signal.SIGKILL)
    assert not alive, f"runner leaked the child process group: {alive}"
    assert res.exit_code != -1
