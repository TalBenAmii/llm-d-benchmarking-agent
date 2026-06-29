"""Hermetic flow harness.

Drives the **real** ``AgentLoop`` (real tool dispatch, real allowlist, real approval
gating) but with two substitutions that make a whole deploy+benchmark flow observable
without an API key, Docker, kind, the upstream repos, or any real side effect:

  * :class:`CaptureRunner` — a ``CommandRunner`` that RECORDS the logical argv of every
    command instead of spawning a subprocess. It bypasses path resolution (no real venv /
    repos needed) and returns synthetic success, so the loop runs to completion and we can
    inspect exactly which commands the agent would have run.
  * a seeded **frozen catalog** (see ``catalog_snapshot``) so the allowlist's
    ``ref_catalog`` checks and the ``SessionPlan`` validator behave as they do in prod.

The same machinery powers three callers:
  * the deterministic gating tests (a *scripted* provider plays a golden transcript),
  * the opt-in live eval (a *real* provider drives from natural-language input), and
  * the local ``scripts/validate_flows.py`` CLI.

Nothing here mutates the host. Read-only probes ``shutil.which`` for tools; the harness
patches that to a flow-declared set so probe behaviour is identical on every machine.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

from app.agent.loop import AgentLoop
from app.agent.session import Session
from app.config import BENCH_REPO_NAME, GUIDE_REPO_NAME, Settings
from app.llm.provider import AssistantTurn, LLMProvider, ProviderTurn, open_provider_turn
from app.security.allowlist import MUTATING, READ_ONLY, Allowlist
from app.security.runner import CommandRunner, RunResult
from app.tools.catalog import catalog_for_allowlist
from app.tools.context import ToolContext
from app.tools.registry import _group_of
from app.tools.shell import classify_shell_command

from .catalog_snapshot import frozen_catalog

# Executables whose invocation IS the flow (vs. read-only environment probes like
# docker/kubectl/kind, which are host-dependent and validated only loosely). Exact,
# ordered matching of "the right commands" is done over these.
SIGNIFICANT_EXES = frozenset({"llmdbenchmark", "install.sh", "git", "helm"})


# ---- scripted provider (golden transcript) ----------------------------------

class ScriptedProvider(LLMProvider):
    """Replays a fixed list of AssistantTurns — the 'golden transcript' for a flow."""

    def __init__(self, turns: list[AssistantTurn]):
        self._turns = list(turns)
        self.i = 0

    async def chat(self, *, system, messages, tools, cache_key=None) -> AssistantTurn:
        if self.i >= len(self._turns):
            # The transcript is exhausted: end the turn cleanly.
            return AssistantTurn(text="", tool_calls=[])
        turn = self._turns[self.i]
        self.i += 1
        return turn


# ---- per-call fail-fast watchdog --------------------------------------------
# The LIVE eval drives a REAL model over a slow network / CLI subprocess. A single hung or
# provider-overloaded call would otherwise stall a flow until the test-level backstop
# (pytest.mark.timeout(300)) fires — wasting minutes per stuck flow and making "is it still
# running?" impossible to tell from a wedged one. Instead we give EACH LLM call its own
# deadline: a breach raises TimeoutError, which the agent loop already catches as a provider
# error (app/agent/loop.py::_run_step) and turns into a clean ``error`` event + an early,
# orderly stop — so the flow fails FAST (and scores as a failure) and the suite moves on, with
# the 300s mark left only as a last-resort backstop. The wrapper PRESERVES the inner provider's
# amortized ``open_turn`` (the Claude Agent SDK's one warm CLI subprocess per turn), so the only
# behavior it adds is the deadline. Tunable via ``LLM_EVAL_CALL_TIMEOUT`` (seconds; <=0 disables).

_DEFAULT_LLM_CALL_TIMEOUT_S = 90.0


def _resolve_call_timeout(call_timeout: float | None) -> float:
    """Resolve the per-LLM-call deadline in seconds. An explicit ``call_timeout`` arg wins; else
    the ``LLM_EVAL_CALL_TIMEOUT`` env var (a bad value is ignored); else the default. A value
    ``<= 0`` disables the watchdog (the unbounded call relies on the 300s test-level backstop)."""
    if call_timeout is not None:
        return call_timeout
    env = os.getenv("LLM_EVAL_CALL_TIMEOUT")
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    return _DEFAULT_LLM_CALL_TIMEOUT_S


# Markers that uniquely identify the claude-agent-sdk's BUNDLED CLI subprocess (the one a live
# flow spawns), so a force-kill matches ONLY it — never a developer's own `claude` CLI, the Claude
# Code daemon, or a co-running live app's SDK subprocess (those live at different paths). BOTH must
# be present in the cmdline.
_SDK_SUBPROCESS_MARKERS = ("claude_agent_sdk", "_bundled")


def _descendant_pids(root: int) -> list[int]:
    """Every transitive child PID of ``root`` (walked via ``pgrep -P``). A kill restricted to this
    set stays inside THIS process's own subprocess tree, so it can never reach a co-running live
    app's or the editor session's CLI subprocess (those are not our descendants)."""
    out: list[int] = []
    frontier = [root]
    while frontier:
        pid = frontier.pop()
        try:
            res = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True, timeout=5)
        except Exception:  # noqa: BLE001 — pgrep missing/slow: best-effort, just stop walking this branch
            continue
        for tok in res.stdout.split():
            with contextlib.suppress(ValueError):
                child = int(tok)
                out.append(child)
                frontier.append(child)
    return out


def _proc_cmdline(pid: int) -> str:
    with contextlib.suppress(Exception):
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return fh.read().replace(b"\x00", b" ").decode("utf-8", "replace")
    return ""


def kill_wedged_sdk_subprocesses(*, markers: tuple[str, ...] = _SDK_SUBPROCESS_MARKERS,
                                 root_pid: int | None = None) -> int:
    """SIGKILL the claude-agent-sdk CLI subprocess(es) spawned UNDER this process.

    ``asyncio`` cancellation does NOT propagate through the SDK's subprocess receive loop, so a
    stalled live call only unblocks once the CLI subprocess dies (its stream read then returns
    EOF and the SDK's await finally raises). This is the force-kill the per-call/per-flow watchdog
    needs. It is doubly scoped for safety: only DESCENDANTS of ``root_pid`` (default this process)
    AND only those whose cmdline carries every SDK ``marker`` — so it can never kill a developer's
    own ``claude``, the Claude Code daemon, or a live app's in-flight SDK subprocess. Returns the
    number signalled (best-effort)."""
    root = os.getpid() if root_pid is None else root_pid
    killed = 0
    for pid in _descendant_pids(root):
        cmd = _proc_cmdline(pid)
        if cmd and all(m in cmd for m in markers):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
                killed += 1
    return killed


class _TimeoutTurn(ProviderTurn):
    """Wraps a :class:`ProviderTurn` so every ``chat()`` step is bounded by ``timeout_s``.
    Delegates open/close to the inner turn unchanged (the SDK's warm-subprocess lifecycle)."""

    def __init__(self, inner: ProviderTurn, timeout_s: float):
        self._inner = inner
        self._timeout_s = timeout_s

    async def __aenter__(self) -> _TimeoutTurn:
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return await self._inner.__aexit__(*exc)

    async def chat(self, messages, *, on_text=None):
        # NOTE: a bare ``asyncio.timeout`` is NOT enough — when the SDK's CLI subprocess wedges
        # (no output), cancelling the awaiting task does not terminate the subprocess, so the call
        # hangs indefinitely (observed: a 28-min stall the 90s deadline never broke). So on the
        # deadline we FORCE-KILL the subprocess (unblocking the SDK's read), THEN cancel and raise
        # a TimeoutError the agent loop turns into a clean ``error`` event + fast flow failure.
        task = asyncio.ensure_future(self._inner.chat(messages, on_text=on_text))
        done, _pending = await asyncio.wait({task}, timeout=self._timeout_s)
        if task in done:
            return task.result()  # re-raises a real provider error to the loop, unchanged
        kill_wedged_sdk_subprocesses()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        raise TimeoutError(
            f"LLM call exceeded {self._timeout_s:g}s (force-killed the wedged CLI subprocess)")


class _PerCallTimeoutProvider(LLMProvider):
    """Provider wrapper giving every LLM call its own deadline WITHOUT losing the inner
    provider's amortized turn. Duck-typed: the loop's ``open_provider_turn`` finds ``open_turn``
    here and gets a timeout-wrapped turn over whatever the inner provider would have used (its
    own warm-subprocess turn, or a plain StatelessTurn). The bare ``chat()`` is there only for
    completeness / direct callers — the agent loop always goes through ``open_turn``."""

    def __init__(self, inner: Any, timeout_s: float):
        self._inner = inner
        self._timeout_s = timeout_s

    async def chat(self, *, system, messages, tools, cache_key=None) -> AssistantTurn:
        async with asyncio.timeout(self._timeout_s):
            return await self._inner.chat(
                system=system, messages=messages, tools=tools, cache_key=cache_key)

    def open_turn(self, *, system, tools, cache_key=None) -> ProviderTurn:
        inner_turn = open_provider_turn(self._inner, system=system, tools=tools, cache_key=cache_key)
        return _TimeoutTurn(inner_turn, self._timeout_s)


# ---- capturing runner --------------------------------------------------------

@dataclass
class CannedResult:
    """A canned command OUTCOME for a matching argv — the FAILING-command primitive.

    The plain ``canned={needle: "stdout"}`` form (a ``str`` value) is the happy path: synthetic
    stdout with ``exit_code=0`` (unchanged, fully backward-compatible). When a flow needs to
    simulate a command that *fails* — a standup that hits a CrashLoopBackOff, a benchmark ``run``
    that exits non-zero, a Job that won't progress — give that needle a ``CannedResult`` instead:
    an explicit ``exit_code`` (non-zero) plus the error ``output`` the CLI would print. The agent
    sees the same structured result it would in production (``execute_llmdbenchmark`` RETURNS the
    exit_code rather than raising), so the golden transcript can assert it recovers correctly
    instead of blindly proceeding. ``timed_out`` covers the hung/timeout case."""

    output: str = ""
    exit_code: int = 0
    timed_out: bool = False


# A canned value is either raw stdout (exit 0) or a full CannedResult (non-zero / timed-out).
CannedValue = str | CannedResult


class CaptureRunner(CommandRunner):
    """Records logical argv; never spawns a process. Simulates the *side effect* of a
    ``git clone`` (materializes a minimal repo skeleton) so downstream tools that check
    the filesystem (``run_setup`` looking for install.sh) behave realistically.

    ``canned`` maps an argv substring (the needle) to the command's simulated outcome. A plain
    ``str`` value is synthetic stdout with ``exit_code=0`` (the happy path). A :class:`CannedResult`
    value lets a flow simulate a FAILING command (non-zero exit / timeout + error output) so
    error-path flows can be exercised hermetically — see ``CannedResult``. The FIRST needle that
    is a substring of the joined argv wins, so order more-specific needles before generic ones."""

    def __init__(self, repo_paths, *, canned: dict[str, CannedValue] | None = None):
        super().__init__(repo_paths)
        self.calls: list[dict[str, Any]] = []
        self._canned = dict(canned or {})

    async def execute(self, logical_argv, entry, *, on_line=None, timeout=None, cwd=None, extra_env=None):
        argv = list(logical_argv)
        self.calls.append({
            "argv": argv,
            "entry": entry,
            "cwd": str(cwd) if cwd else None,
            "extra_env": dict(extra_env) if extra_env else None,
        })

        self._maybe_simulate_clone(argv, cwd)

        output = ""
        exit_code = 0
        timed_out = False
        joined = " ".join(argv)
        for needle, value in self._canned.items():
            if needle in joined:
                if isinstance(value, CannedResult):
                    output, exit_code, timed_out = value.output, value.exit_code, value.timed_out
                else:
                    output = value
                break
        if on_line and output:
            for line in output.splitlines():
                await on_line(line)
        return RunResult(
            exit_code=exit_code,
            duration_s=0.0,
            real_argv=argv,
            cwd=str(cwd) if cwd else None,
            output=output,
            lines=output.splitlines(),
            timed_out=timed_out,
        )

    @staticmethod
    def _maybe_simulate_clone(argv: list[str], cwd) -> None:
        # Mirror reality: `git clone https://github.com/llm-d/<name>` creates <cwd>/<name>.
        if argv[:2] != ["git", "clone"] or len(argv) < 3 or not cwd:
            return
        url = argv[2].removesuffix(".git")
        name = url.rstrip("/").rsplit("/", 1)[-1]
        if name not in (BENCH_REPO_NAME, GUIDE_REPO_NAME):
            return
        repo = Path(cwd) / name
        (repo / ".git").mkdir(parents=True, exist_ok=True)
        if name == BENCH_REPO_NAME:
            (repo / "install.sh").write_text("#!/usr/bin/env bash\n")  # presence is enough


# ---- result of running a flow ------------------------------------------------

@dataclass
class CapturedCommand:
    argv: list[str]
    mode: str             # read_only | mutating  (per the real allowlist + frozen catalog)
    approved: bool        # did it pass through the approval gate?
    cwd: str | None

    @property
    def exe(self) -> str:
        return self.argv[0] if self.argv else ""


@dataclass
class FlowRun:
    commands: list[CapturedCommand]
    approval_requests: list[dict[str, Any]]   # [{kind, payload, approved}]
    events: list[tuple[str, dict]]
    errors: list[str]
    assistant_texts: list[str]
    tool_calls: list[dict[str, Any]]          # [{name, input}]
    session: Session
    # True when the flow ran in SIMULATE mode (mutating commands auto-run as no-ops with the
    # per-command approval gate intentionally skipped — see ``run_flow``/``gating_problems``).
    simulate: bool = False

    @property
    def significant(self) -> list[CapturedCommand]:
        return [c for c in self.commands if c.exe in SIGNIFICANT_EXES]

    @property
    def ended_done(self) -> bool:
        return bool(self.events) and self.events[-1][0] == "done"

    def tool_result(self, name: str) -> dict[str, Any] | None:
        """The result payload of the last ``tool_result`` event for ``name`` (or None)."""
        for t, p in reversed(self.events):
            if t == "tool_result" and p.get("name") == name:
                return p.get("result")
        return None

    def tool_errored(self, name: str) -> bool:
        """True if any captured ``tool_result`` for ``name`` carried an error/refusal."""
        for t, p in self.events:
            if t == "tool_result" and p.get("name") == name:
                res = p.get("result") or {}
                if res.get("error") or res.get("rejected") or res.get("valid") is False:
                    return True
        return False

    def subcommands(self, exe: str = "llmdbenchmark") -> list[str]:
        """The CLI subcommand of each captured invocation of ``exe`` (best-effort:
        first non-flag token after the executable / after a global flag value)."""
        from app.tools.execute import _SUBCOMMANDS  # the known subcommand set
        out = []
        for c in self.commands:
            if c.exe != exe:
                continue
            for tok in c.argv[1:]:
                if tok in _SUBCOMMANDS:
                    out.append(tok)
                    break
        return out


# ---- repo-state materialization ----------------------------------------------

def _materialize_repo_state(repos_dir: Path, state: str) -> None:
    """Lay down just enough of the (fake) bench repo for the tool preconditions to
    behave as the named ``state`` describes. No network, no real clone."""
    bench = repos_dir / BENCH_REPO_NAME
    repos_dir.mkdir(parents=True, exist_ok=True)
    if state == "absent":
        return  # nothing on disk → ensure_repos will (fake-)clone
    # present_* : the repo exists as a git checkout with install.sh
    (bench / ".git").mkdir(parents=True, exist_ok=True)
    (bench / "install.sh").write_text("#!/usr/bin/env bash\n")
    if state == "present_with_venv":
        venv_bin = bench / ".venv" / "bin"
        venv_bin.mkdir(parents=True, exist_ok=True)
        (venv_bin / "python").write_text("")          # run_setup sees the venv → no-op
        (bench / ".venv" / "pyvenv.cfg").write_text("version = 3.11.0\n")


# ---- the entry point ---------------------------------------------------------

async def run_flow(
    flow,
    *,
    tmp_path: Path,
    provider: LLMProvider | None = None,
    approve=None,
    simulate: bool = False,
    call_timeout: float | None = None,
) -> FlowRun:
    """Run one flow through the real agent loop in a hermetic sandbox.

    ``provider`` defaults to a :class:`ScriptedProvider` replaying ``flow.turns`` (the
    deterministic path). Pass a real provider for the live eval. ``approve`` is a sync
    ``(kind, payload) -> bool``; defaults to approving everything.

    ``call_timeout`` bounds EACH live LLM call (seconds) via the per-call watchdog so one hung
    call fails the flow fast instead of stalling to the test backstop — see
    :class:`_PerCallTimeoutProvider`. It applies ONLY when a real ``provider`` is supplied (the
    scripted/deterministic gate path never hangs and is left exactly as-is); ``None`` resolves
    from ``LLM_EVAL_CALL_TIMEOUT`` or the default. The scripted path is byte-for-byte unchanged.

    ``simulate=True`` turns on the app's SIMULATE mode for this run: the system prompt gains
    the SIMULATE_NOTE (the agent is told to walk the WHOLE workflow end-to-end without stopping
    for confirmations or missing hardware), and mutating commands auto-run as no-ops with the
    per-command approval gate skipped. Passed as an explicit ``Settings`` kwarg so it overrides
    conftest's ``SIMULATE=0`` (init kwargs beat env vars in pydantic-settings). The
    universal safety invariant in :func:`gating_problems` adapts: in simulate mode the
    intentionally-skipped per-command gate is NOT treated as a violation (the upfront
    SessionPlan approval still applies).
    """
    repos_dir = tmp_path / "repos"
    _materialize_repo_state(repos_dir, flow.repo_state)

    settings = Settings(
        _env_file=None,                       # fully hermetic — ignore the developer's .env
        repos_dir=repos_dir,
        workspace_dir=tmp_path / "ws",
        llm_provider="anthropic",
        anthropic_api_key="not-used-in-scripted-mode",
        simulate=simulate,
        # The resilience-drill flow opts into the backend chaos gate; default OFF (production).
        chaos_enabled=getattr(flow, "chaos_enabled", False),
    )
    allowlist = Allowlist.from_file(settings.allowlist_path)
    runner = CaptureRunner(settings.repo_paths, canned=flow.canned)
    workspace = settings.resolved_workspace_dir / "sessions" / "flow"
    ctx = ToolContext(settings=settings, allowlist=allowlist, runner=runner, workspace=workspace)
    # Pin the catalog to the frozen snapshot. Setting the field is not enough: tools like
    # ensure_repos call ctx.catalog(refresh=True), which would re-scan the empty fake repo
    # and wipe it. Shadow the method so every lookup returns the snapshot.
    frozen = frozen_catalog()
    ctx._catalog = frozen
    ctx.catalog = lambda *, refresh=False: frozen
    session = Session(id="flow", ctx=ctx)

    if provider is None:
        provider = ScriptedProvider(flow.turns)
    else:
        # Real provider (the live eval): give every LLM call a fail-fast deadline so one hung
        # call can't stall the flow up to the 300s test backstop. The scripted provider above
        # never hangs, so it is deliberately left unwrapped — the deterministic gate is unchanged.
        timeout_s = _resolve_call_timeout(call_timeout)
        if timeout_s > 0:
            provider = _PerCallTimeoutProvider(provider, timeout_s)
    if approve is None:
        approve = lambda kind, payload: True  # noqa: E731

    events: list[tuple[str, dict]] = []
    approval_requests: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []

    async def emit(t, p):
        events.append((t, p))
        if t == "tool_call":
            tool_calls.append({"name": p["name"], "input": p["input"]})
        elif t == "command":
            # Mirror production (app/main.py): record the executed-command trail on the
            # session so the persist -> reload -> replay path is exercised end-to-end.
            session.record_command(p)

    async def request_approval(kind, payload):
        decision = bool(approve(kind, payload))
        approval_requests.append({"kind": kind, "payload": payload, "approved": decision})
        return decision

    def fake_which(name, *a, **k):
        return f"/usr/bin/{name}" if name in flow.tools_present else None

    # Patch the environment-sensing layer so probe behaviour is identical on every host.
    with patch("app.tools.probe.shutil.which", side_effect=fake_which):
        loop = AgentLoop(provider)
        await loop.run_turn(session, flow.mock_user_input, emit=emit, request_approval=request_approval)

    # Label each captured command with its real allowlist mode + whether it was gated.
    cat = catalog_for_allowlist(frozen_catalog())
    approved_argvs = [r["payload"].get("argv") for r in approval_requests if r["kind"] == "command"]
    commands: list[CapturedCommand] = []
    for call in runner.calls:
        argv = call["argv"]
        # run_shell (the agent's always-on ad-hoc `bash -lc` surface) is governed by the
        # read-only/mutating CLASSIFIER + approval gate, NOT the allowlist — which governs only the
        # DEDICATED command tools (see app/tools/shell.py + app/tools/CLAUDE.md). Validating a
        # run_shell command against the allowlist wrongly marks it "denied", which would trip the
        # bypass check in gating_problems and falsely fail any LIVE flow where the real model
        # improvises with run_shell. Classify it the way production does, so the SAME safety
        # invariant (mutating ⇒ approval-gated; read-only ⇒ auto-run) still applies — correctly — to it.
        if argv[:2] == ["bash", "-lc"] and len(argv) >= 3:
            mode = classify_shell_command(argv[2])
        else:
            d = allowlist.validate(argv, catalog=cat)
            mode = d.mode if d.allowed else "denied"
        commands.append(CapturedCommand(
            argv=argv,
            mode=mode,
            approved=argv in approved_argvs,
            cwd=call["cwd"],
        ))

    return FlowRun(
        commands=commands,
        approval_requests=approval_requests,
        events=events,
        errors=[p.get("message", "") for (t, p) in events if t == "error"],
        assistant_texts=[p["text"] for (t, p) in events if t == "assistant_text"],
        tool_calls=tool_calls,
        session=session,
        simulate=simulate,
    )


# ---- matchers ----------------------------------------------------------------

def argv_matches(expected: list[str], actual: list[str]) -> bool:
    """Element-wise compare; the token ``"*"`` in ``expected`` matches any single token
    (used for the run command's dynamic ``-r <results_dir>`` path)."""
    if len(expected) != len(actual):
        return False
    return all(e == "*" or e == a for e, a in zip(expected, actual, strict=True))


def diff_significant(run: FlowRun, expected: list[ExpectedCommand]) -> list[str]:
    """Return human-readable mismatches between a flow's significant captured commands
    and its expected (ordered) command list. Empty == match."""
    actual = run.significant
    problems: list[str] = []
    if len(actual) != len(expected):
        problems.append(
            f"expected {len(expected)} significant command(s), got {len(actual)}:\n"
            f"  expected: {[e.argv for e in expected]}\n"
            f"  actual:   {[c.argv for c in actual]}"
        )
        return problems
    for i, (exp, got) in enumerate(zip(expected, actual, strict=True)):
        if not argv_matches(exp.argv, got.argv):
            problems.append(f"command #{i} argv mismatch:\n  expected: {exp.argv}\n  actual:   {got.argv}")
        if exp.mode != got.mode:
            problems.append(f"command #{i} mode mismatch: expected {exp.mode!r}, got {got.mode!r}\n  argv: {got.argv}")
    return problems


def _specs_used(run: FlowRun) -> set[str]:
    out: set[str] = set()
    for c in run.commands:
        if c.exe == "llmdbenchmark" and "--spec" in c.argv:
            out.add(c.argv[c.argv.index("--spec") + 1])
    return out


def score_flow(run: FlowRun, flow) -> tuple[bool, list[str]]:
    """Coarse, order-tolerant scoring for the LIVE eval (a real model drives the flow):
    did the agent run the *required* subcommands with the right spec, avoid the forbidden
    ones, and respect read-only/refusal expectations? Returns (passed, human notes).

    Deliberately looser than ``diff_significant`` — a real model may add extra read-only
    probing or phrase things differently; we score the substance, not the exact argv."""
    notes: list[str] = []
    ok = True
    subs = run.subcommands()

    if not run.ended_done:
        ok, _ = False, notes.append("loop did not finish cleanly")
    if run.errors:
        ok, _ = False, notes.append(f"loop emitted errors: {run.errors}")

    if flow.required_subcommands:
        missing = [s for s in flow.required_subcommands if s not in subs]
        if missing:
            ok = False
            notes.append(f"missing required subcommand(s) {missing} (ran {subs or 'none'})")
        else:
            notes.append(f"ran required subcommand(s) {flow.required_subcommands}")

    bad = [s for s in flow.forbidden_subcommands if s in subs]
    if bad:
        ok = False
        notes.append(f"ran FORBIDDEN subcommand(s) {bad}")

    # Tool-CHOICE scoring: for flows whose substance is a tool the model must pick from
    # natural language (DOE/analysis/history/orchestrator/capacity/readiness/observe/cancel),
    # not an llmdbenchmark subcommand. Order-tolerant, like the subcommand check above.
    if flow.required_tools or flow.forbidden_tools:
        called = {tc["name"] for tc in run.tool_calls}
        missing_tools = [t for t in flow.required_tools if t not in called]
        if missing_tools:
            ok = False
            notes.append(f"missing required tool call(s) {missing_tools} "
                         f"(called {sorted(called) or 'none'})")
        elif flow.required_tools:
            notes.append(f"called required tool(s) {flow.required_tools}")
        bad_tools = [t for t in flow.forbidden_tools if t in called]
        if bad_tools:
            ok = False
            notes.append(f"called FORBIDDEN tool(s) {bad_tools}")

    if flow.required_spec:
        used = _specs_used(run)
        if flow.required_spec not in used:
            ok = False
            notes.append(f"expected --spec {flow.required_spec!r}, saw {sorted(used) or 'none'}")

    if flow.expect_all_readonly:
        muts = [c.argv for c in run.commands if c.mode == MUTATING]
        if muts:
            ok = False
            notes.append(f"expected read-only-only, but these mutate: {muts}")

    if flow.expect_no_significant and run.significant:
        ok = False
        notes.append(f"expected nothing to run, but ran {[c.argv for c in run.significant]}")

    # --- phase-group lazy-loading (token-budget mechanism) -----------------------------------
    # A real model only ever sees the STARTER_KIT; to call any GROUPED tool it must FIRST call
    # load_tools(['<group>']) — the loop then re-opens the turn with that group exposed. This
    # scores that the live model navigates that extra step correctly. For a flow whose substance
    # is a grouped TOOL choice (required_tools), the group(s) those tools live in must have been
    # loaded. Per the chosen policy ("right group, extras allowed") loading an EXTRA group is NOT
    # a failure — it's surfaced as a NOTE, because over-loading is exactly what re-inflates the
    # resident tool schema the lazy-loading is meant to save, and the live eval is the only place
    # that signal is observable. Only NEVER loading a needed group is a hard failure. (score_flow
    # is the live-eval scorer only; the deterministic gate does not call it, and a scripted golden
    # transcript may legitimately omit load_tools — dispatch ignores the exposed set there.)
    loaded_groups = set(run.session.loaded_groups)
    called_load_tools = any(tc["name"] == "load_tools" for tc in run.tool_calls)
    needed_groups = {g for t in flow.required_tools if (g := _group_of(t))}
    if needed_groups:
        missing_groups = needed_groups - loaded_groups
        if missing_groups:
            ok = False
            notes.append(
                f"never loaded tool group(s) {sorted(missing_groups)} needed for "
                f"{flow.required_tools} (loaded {sorted(loaded_groups) or 'none'}) — the model "
                "did not call load_tools to reach them")
        else:
            notes.append(f"loaded the needed group(s) {sorted(needed_groups)} via load_tools")
            extra = loaded_groups - needed_groups
            if extra:
                notes.append(
                    f"NOTE: also loaded unneeded group(s) {sorted(extra)} — allowed, but each "
                    "extra group re-inflates the resident tool schema the lazy-loading saves")
    # Mechanism integrity (independent of any flow's expectations): groups don't load themselves,
    # so if any group ended up loaded, load_tools MUST have been the thing that loaded it.
    if loaded_groups and not called_load_tools:
        ok = False
        notes.append(f"group(s) {sorted(loaded_groups)} are loaded but load_tools was never "
                     "called — phase-group mechanism regression")

    g = gating_problems(run)
    if g:
        ok = False
        notes.extend(g)
    return ok, notes


def gating_problems(run: FlowRun) -> list[str]:
    """The universal safety invariant, independent of any flow's expectations:
    every mutating command must have been approval-gated; no read-only command should be;
    no denied command may reach the runner.

    In SIMULATE mode the per-command approval gate is deliberately skipped (mutating commands
    run as harmless no-ops, so the walk isn't stalled — see ``app/tools/context.py``); the
    upfront SessionPlan approval still applies. So under ``run.simulate`` we do NOT treat an
    un-gated mutating command as a violation — but the deny-bypass and read-only-gating
    invariants still hold unconditionally."""
    problems: list[str] = []
    for c in run.commands:
        if c.mode == MUTATING and not c.approved and not run.simulate:
            problems.append(f"mutating command was NOT approval-gated: {c.argv}")
        if c.mode == READ_ONLY and c.approved:
            problems.append(f"read-only command went through the approval gate (should auto-run): {c.argv}")
        if c.mode == "denied":
            problems.append(f"a denied command reached the runner (allowlist bypass!): {c.argv}")
    return problems


# Re-exported here to avoid a circular import in flows.py type hints.
@dataclass
class ExpectedCommand:
    argv: list[str]
    mode: str  # READ_ONLY | MUTATING
