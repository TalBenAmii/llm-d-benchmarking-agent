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

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

from app.agent.loop import AgentLoop
from app.agent.session import Session
from app.config import BENCH_REPO_NAME, GUIDE_REPO_NAME, Settings
from app.llm.provider import AssistantTurn, LLMProvider
from app.security.allowlist import MUTATING, READ_ONLY, Allowlist
from app.security.runner import CommandRunner, RunResult
from app.tools.catalog import catalog_for_allowlist
from app.tools.context import ToolContext

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
) -> FlowRun:
    """Run one flow through the real agent loop in a hermetic sandbox.

    ``provider`` defaults to a :class:`ScriptedProvider` replaying ``flow.turns`` (the
    deterministic path). Pass a real provider for the live eval. ``approve`` is a sync
    ``(kind, payload) -> bool``; defaults to approving everything.

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
        d = allowlist.validate(call["argv"], catalog=cat)
        commands.append(CapturedCommand(
            argv=call["argv"],
            mode=d.mode if d.allowed else "denied",
            approved=call["argv"] in approved_argvs,
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
