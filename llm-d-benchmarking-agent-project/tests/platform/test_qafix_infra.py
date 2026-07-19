# Merged QA-fix infra tests (policy + agent loop + command runner).
# Concatenated from three sources; each section is preserved below under its own
# `# ── <original file> ──` banner with the original module docstring kept verbatim
# as a comment block.
from __future__ import annotations

import json
import os
import signal
from pathlib import Path

import pytest

from app.agent.engine import SdkNativeEngine
from app.config import get_settings
from app.security.policy import READ_ONLY, CommandPolicy
from app.security.runner import TIMEOUT_MARKER, CommandRunner, RunResult, SimRunner
from tests._helpers import _session
from tests._sdk_fake import FakeTransport, assistant, result, text, tool_use

# ── test_qafix_infra_policy.py ──
# """QA-fix infra tests: narrow policy entries for multi-cluster context recovery.
#
# Finding real-1 07:20: with multiple kind clusters present the active kubectl context drifted to
# a sibling cluster and the agent could not recover because `kind export kubeconfig` and
# `kubectl config use-context` were both blocked. Both are read-only-ish (they only rewrite a
# local kubeconfig / its active-context pointer — no cluster mutation), so they auto-run.
#
# Also asserts the WSL2-realistic read-only kubectl deadline (real-1 00:15 / real-2 08:10): the
# read-only kubectl probe subcommands now declare `timeout_s: 25` (DATA) so the runner stops
# flooding the log with 12s timeouts on a slow-but-reachable apiserver.
# """
PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CATALOG = {"specs": [], "harnesses": [], "workloads": []}


@pytest.fixture(scope="module")
def policy() -> CommandPolicy:
    # Loading also runs the governance + positional schema validators — a malformed edit would
    # raise here, so this fixture doubles as a "yaml still loads" guard.
    return CommandPolicy.from_file(PROJECT_ROOT / "security" / "command_policy.yaml")


def _v(policy, argv):
    return policy.validate(argv, catalog=_CATALOG)


# ---- kind export kubeconfig -------------------------------------------------
def test_kind_export_kubeconfig_by_name(policy):
    d = _v(policy, ["kind", "export", "kubeconfig", "--name", "ralph-real-1-x3"])
    assert d.allowed and d.mode == READ_ONLY


def test_kind_export_kubeconfig_with_kubeconfig_path(policy):
    d = _v(policy, ["kind", "export", "kubeconfig", "--name", "foo", "--kubeconfig", "/tmp/kc"])
    assert d.allowed and d.mode == READ_ONLY


def test_kind_export_rejects_unknown_positional(policy):
    # Only `kubeconfig` is a valid first positional for `export`.
    d = _v(policy, ["kind", "export", "logs"])
    assert not d.allowed


def test_kind_export_rejects_traversing_kubeconfig(policy):
    # kubeconfig_path forbids `..` traversal.
    d = _v(policy, ["kind", "export", "kubeconfig", "--kubeconfig", "../../etc/passwd"])
    assert not d.allowed


# ---- kubectl config use-context ---------------------------------------------
def test_kubectl_use_context(policy):
    d = _v(policy, ["kubectl", "config", "use-context", "kind-ralph-real-1-x3"])
    assert d.allowed and d.mode == READ_ONLY


def test_kubectl_existing_config_verbs_still_work(policy):
    for verb in ("current-context", "view", "get-contexts"):
        d = _v(policy, ["kubectl", "config", verb])
        assert d.allowed and d.mode == READ_ONLY, verb


def test_kubectl_config_rejects_unknown_verb(policy):
    d = _v(policy, ["kubectl", "config", "set-credentials", "bad"])
    assert not d.allowed


def test_kubectl_use_context_rejects_dangerous_context(policy):
    # The metacharacter screen still rejects shell-dangerous tokens.
    d = _v(policy, ["kubectl", "config", "use-context", "foo;rm -rf /"])
    assert not d.allowed


# ---- WSL2-realistic read-only kubectl deadline ------------------------------
@pytest.mark.parametrize("argv", [
    ["kubectl", "config", "current-context"],
    ["kubectl", "cluster-info"],
    ["kubectl", "version", "--client"],
    ["kubectl", "get", "pods", "-n", "llm-d", "-o", "json"],
    ["kubectl", "top", "nodes"],
])
def test_readonly_kubectl_probes_have_wsl2_deadline(policy, argv):
    d = _v(policy, argv)
    assert d.allowed and d.mode == READ_ONLY
    # The YAML timeout_s (which OVERRIDES the probe tool's 12s caller timeout) is the WSL2-realistic 25s.
    assert d.timeout_s == 25, argv


def test_mutating_kubectl_keeps_default_deadline(policy):
    # apply/delete intentionally do NOT get the read-only probe deadline — they keep their own
    # (unset → runner global default) so a long apply isn't artificially capped.
    d = _v(policy, ["kubectl", "apply", "-f", "job.yaml"])
    assert d.allowed and d.mode != READ_ONLY
    assert d.timeout_s is None


# ── test_qafix_infra_loop.py ──
# """QA-fix infra tests: the engine's abandoned-turn guard.
#
# Finding sim-1 00:40: after the WebSocket client disconnected, the turn kept calling tools
# and ran to completion ~89s later, burning API tokens with no recipient. The engine polls an
# optional ``should_continue()`` predicate at stream-message boundaries and INTERRUPTS the CLI
# when it reports the turn is abandoned — never mid-tool — then declines any steer follow-ups.
# """
class _RecordingTransport(FakeTransport):
    """Records the control-request subtypes the SDK writes, so a test can observe the
    engine's abandoned-turn ``interrupt``."""

    def __init__(self, script):
        super().__init__(script)
        self.control_subtypes: list[str] = []

    async def write(self, data: str) -> None:
        message = json.loads(data)
        if message.get("type") == "control_request":
            self.control_subtypes.append(message["request"]["subtype"])
        await super().write(data)


def _abandon_engine(script):
    fakes: list[_RecordingTransport] = []

    def factory():
        fake = _RecordingTransport(script)
        fakes.append(fake)
        return fake

    return SdkNativeEngine(transport_factory=factory), fakes


async def test_abandoned_turn_interrupts_the_stream(tmp_path):
    """should_continue() == False on entry → the engine interrupts the CLI at the first
    stream message and the turn still ends with a clean terminal `done`."""
    events: list[tuple[str, dict]] = []

    async def emit(t, p):
        events.append((t, p))

    async def request_approval(kind, payload):
        raise AssertionError("no approval should be requested on an abandoned turn")

    session = _session(tmp_path)
    session.catalog_injected = True
    engine, fakes = _abandon_engine([[assistant(text("working")), result()]])
    await engine.run_turn(
        session, "hello", emit=emit, request_approval=request_approval,
        should_continue=lambda: False,
    )

    assert "interrupt" in fakes[0].control_subtypes, "abandoned turn was never interrupted"
    assert events[-1][0] == "done"


async def test_abandoned_after_one_step_interrupts_before_more_work(tmp_path):
    """Disconnect partway through: allow the first tool step, then report abandoned → the
    engine interrupts and refuses steer follow-ups (the turn winds down instead of stepping on)."""
    if not get_settings().bench_repo.is_dir():
        pytest.skip("repo not present")

    events: list[tuple[str, dict]] = []
    alive = {"v": True}

    async def emit(t, p):
        events.append((t, p))
        # Simulate the recipient dropping right after the first tool result lands.
        if t == "tool_result":
            alive["v"] = False

    async def request_approval(kind, payload):
        return True

    session = _session(tmp_path)
    session.catalog_injected = True
    engine, fakes = _abandon_engine([[
        assistant(text("step1"),
                  tool_use("c1", "mcp__benchtools__list_catalog", {"kinds": ["harnesses"]})),
        assistant(text("wrapping up")),
        result(),
    ]])
    await engine.run_turn(
        session, "go", emit=emit, request_approval=request_approval,
        should_continue=lambda: alive["v"],
    )

    # The first tool actually ran, then the guard interrupted the turn.
    assert [p["name"] for (t, p) in events if t == "tool_call"] == ["list_catalog"]
    assert "interrupt" in fakes[0].control_subtypes, "abandoned turn was never interrupted"
    assert events[-1][0] == "done"


async def test_default_no_guard_runs_to_completion(tmp_path):
    """Backward compatibility: with should_continue=None the engine never interrupts."""
    events: list[tuple[str, dict]] = []

    async def emit(t, p):
        events.append((t, p))

    async def request_approval(kind, payload):
        return True

    session = _session(tmp_path)
    session.catalog_injected = True
    engine, fakes = _abandon_engine([[assistant(text("all done, no tools")), result()]])
    await engine.run_turn(session, "hi", emit=emit, request_approval=request_approval)

    assert "interrupt" not in fakes[0].control_subtypes
    assert events[-1][0] == "done"


# ── test_qafix_infra_runner.py ──
# """QA-fix infra tests: the runner makes a timeout a first-class, distinguishable signal.
#
# Findings real-1 00:15 / real-2 08:10: kubectl probes blew past the deadline and the tool
# continued on incomplete data, silently treating an empty/absent result as success. The runner
# already flagged ``timed_out`` + ``exit_code == -1``, but a caller reading only ``.output`` saw
# an empty string indistinguishable from a clean empty success. The runner now (a) appends a
# TIMEOUT_MARKER line to the captured output so even ``.output``-only readers can tell, and
# (b) records the ``deadline_s`` it was bounded by so a timeout is fully self-describing.
# """
def _runner() -> CommandRunner:
    return CommandRunner(get_settings().repo_paths)


async def test_timeout_is_distinguishable_from_empty_success(tmp_path):
    """A command that exceeds its deadline → timed_out, exit_code -1, marker in output, deadline_s set."""
    runner = _runner()
    # `sleep 5` with a 0.3s deadline reliably times out without depending on a cluster.
    # `sleep` resolves via PATH (no policy needed at the runner layer — the runner trusts
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
