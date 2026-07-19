"""Hermetic flow harness.

Drives the **real** :class:`SdkNativeEngine` (real tool dispatch, real policy, real approval
gating — the SDK's own protocol machinery over a hermetic FakeTransport) with two substitutions
that make a whole deploy+benchmark flow observable without an API key, Docker, kind, the
upstream repos, or any real side effect:

  * :class:`CaptureRunner` — a ``CommandRunner`` that RECORDS the logical argv of every
    command instead of spawning a subprocess. It bypasses path resolution (no real venv /
    repos needed) and returns synthetic success, so the turn runs to completion and we can
    inspect exactly which commands the agent would have run.
  * a seeded **frozen catalog** (see ``catalog_snapshot``) so the policy's
    ``ref_catalog`` checks and the ``SessionPlan`` validator behave as they do in prod.

The same machinery powers three callers:
  * the deterministic gating tests (a golden transcript replays over the FakeTransport),
  * the opt-in live eval (``live=True`` — the REAL logged-in CLI + a real model drive from
    natural-language input), and
  * the local ``scripts/eval/validate_flows.py`` CLI.

Nothing here mutates the host. Read-only probes ``shutil.which`` for tools; the harness
patches that to a flow-declared set so probe behaviour is identical on every machine.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

from app.agent.engine import SdkNativeEngine
from app.agent.session import Session
from app.config import BENCH_REPO_NAME, GUIDE_REPO_NAME, Settings
from app.security.policy import MUTATING, READ_ONLY, CommandPolicy
from app.security.runner import CommandRunner, RunResult
from app.tools.context import ToolContext
from app.tools.run.shell import classify_shell_command
from app.tools.setup.catalog import catalog_for_policy
from tests._scripted import sdk_script
from tests._sdk_fake import FakeTransport

from .catalog_snapshot import frozen_catalog

# Executables whose invocation IS the flow (vs. read-only environment probes like
# docker/kubectl/kind, which are host-dependent and validated only loosely). Exact,
# ordered matching of "the right commands" is done over these.
SIGNIFICANT_EXES = frozenset({"llmdbenchmark", "install.sh", "git", "helm"})


# ---- live-eval fail-fast deadline -------------------------------------------
# The LIVE eval drives a REAL model over the CLI subprocess. A wedged call would otherwise
# stall a flow until the test-level backstop (pytest.mark.timeout(300)) fires. The engine's own
# stream watchdog handles this natively — no stream progress for the deadline (with tool
# execution exempt) interrupts the CLI and raises StreamStalledError, which run_turn turns into
# a clean ``error`` event + an orderly stop, and the engine's disconnect terminates the CLI
# subprocess. The harness just feeds it a tighter live-eval deadline than the production
# default. Tunable via ``LLM_EVAL_CALL_TIMEOUT`` (seconds; <=0 disables).

_DEFAULT_LLM_CALL_TIMEOUT_S = 90.0


def _resolve_call_timeout(call_timeout: float | None) -> float:
    """Resolve the live-eval stall deadline in seconds. An explicit ``call_timeout`` arg wins;
    else the ``LLM_EVAL_CALL_TIMEOUT`` env var (a bad value is ignored); else the default. A
    value ``<= 0`` disables the watchdog (the flow then relies on the 300s test backstop)."""
    if call_timeout is not None:
        return call_timeout
    env = os.getenv("LLM_EVAL_CALL_TIMEOUT")
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    return _DEFAULT_LLM_CALL_TIMEOUT_S


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

    # This fake never spawns a real process and already makes every command safe (canned/no-op),
    # so the SIMULATE caller-gate must NOT pre-empt mutating commands here — it must still call us
    # so `calls` records them and `_maybe_simulate_clone` fires (the flows assert over `calls`).
    runs_real_subprocess = False

    def __init__(self, repo_paths, *, canned: dict[str, CannedValue] | None = None,
                 hydrate_bench: bool = False):
        super().__init__(repo_paths)
        self.calls: list[dict[str, Any]] = []
        self._canned = dict(canned or {})
        # LIVE-eval only, absent-flow only: after the fake clone/install, leave the bench repo's
        # config tree + venv on disk (a REAL clone/install would), so the read-only pre-flight
        # tools don't error on a half-built skeleton. See _maybe_hydrate_bench_repo.
        self._hydrate_bench = hydrate_bench

    async def execute(self, logical_argv, entry, *, on_line=None, timeout=None, cwd=None, extra_env=None):
        argv = list(logical_argv)
        self.calls.append({
            "argv": argv,
            "entry": entry,
            "cwd": str(cwd) if cwd else None,
            "extra_env": dict(extra_env) if extra_env else None,
        })

        self._maybe_simulate_clone(argv, cwd)
        self._maybe_hydrate_bench_repo(argv, cwd)

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

    def _maybe_hydrate_bench_repo(self, argv: list[str], cwd) -> None:
        """LIVE-eval fidelity for the absent→clone→install path (kind-quickstart): a REAL
        ``git clone`` + ``install.sh`` leave the bench repo's ``config/`` tree AND its ``.venv`` on
        disk. The bare skeleton the fake left instead makes the read-only pre-flight tools contradict
        the "I just set it up" state — ``check_capacity`` errors on the missing
        ``config/templates/values/defaults.yaml`` and ``run_setup``/``probe`` report ``venv_exists:
        false`` right after a "successful" install — which intermittently stalls the live agent
        before standup. Materializing what the real commands produce removes that contradiction.

        Gated to the absent flow via ``hydrate_bench`` (set only for a live absent-repo run), so the
        present_* flows keep their intentional repo/venv state and the scripted path is untouched."""
        if not self._hydrate_bench:
            return
        bench = self._repos.get(BENCH_REPO_NAME)
        if bench is None:
            return
        if argv[:2] == ["git", "clone"] and len(argv) > 2:
            # The clone brings the REAL config tree (idempotent; a no-op when no real repo is
            # resolvable, e.g. a bare CI runner — the helper checks that).
            name = argv[2].removesuffix(".git").rstrip("/").rsplit("/", 1)[-1]
            if name == BENCH_REPO_NAME:
                _materialize_real_bench_config(bench.parent)
        elif argv and argv[0].rsplit("/", 1)[-1] == "install.sh":
            # install.sh builds the framework venv — lay down the same skeleton marker
            # _materialize_repo_state uses for present_with_venv (run_setup/probe key on its presence).
            venv_bin = bench / ".venv" / "bin"
            if not (venv_bin / "python").exists():
                venv_bin.mkdir(parents=True, exist_ok=True)
                (venv_bin / "python").write_text("")
                (bench / ".venv" / "pyvenv.cfg").write_text("version = 3.11.0\n")


# ---- result of running a flow ------------------------------------------------

@dataclass
class CapturedCommand:
    argv: list[str]
    mode: str             # read_only | mutating  (per the real policy + frozen catalog)
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
        from app.tools.run.execute import _SUBCOMMANDS  # the known subcommand set
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


def _materialize_real_bench_config(repos_dir: Path) -> None:
    """Copy the REAL ``llm-d-benchmark`` ``config/`` tree into the (otherwise fake) bench repo so the
    LIVE eval's capacity tools (``plan_config_for_spec`` → ``check_capacity``) read true
    defaults/scenarios and REACH the canned bridge, instead of erroring on a missing
    ``config/templates/values/defaults.yaml``.

    This makes the live sandbox match every gated/capacity flow's documented promise — "in LIVE eval
    the REAL repo is present, so check_capacity reaches the bridge". The real repo is resolved via
    ``REPOS_DIR`` (the isolated runner points it at the primary checkout); absent it, this is a no-op
    and the flow keeps the fake skeleton. We COPY (never symlink) the config tree so the harness /
    agent can never write back into the READ-ONLY upstream repo."""
    src = Settings(_env_file=None).bench_repo / "config"     # honors REPOS_DIR env
    if not src.is_dir():
        return  # no real repo on this box (e.g. a bare CI runner) → leave the fake skeleton as-is
    dst = repos_dir / BENCH_REPO_NAME / "config"
    if dst.exists():
        return  # already materialized (idempotent)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst)


# ---- the entry point ---------------------------------------------------------

async def run_flow(
    flow,
    *,
    tmp_path: Path,
    approve=None,
    simulate: bool = False,
    call_timeout: float | None = None,
    live: bool = False,
) -> FlowRun:
    """Run one flow through the real engine in a hermetic sandbox.

    The deterministic path (default) replays ``flow.turns`` (the golden transcript) through the
    SDK's real protocol machinery over a hermetic FakeTransport — see
    :func:`tests._scripted.sdk_script`. ``approve`` is a sync ``(kind, payload) -> bool``;
    defaults to approving everything.

    ``live=True`` (opt-in live eval) drops the FakeTransport: the engine spawns the REAL
    logged-in CLI and a real model drives the flow inside the same sandbox (CaptureRunner —
    nothing can mutate). Model/effort ride in via the AGENT_SDK_* env vars (the hermetic
    Settings ignores .env but still reads real env).

    ``call_timeout`` (live only) bounds stream stalls (seconds) via the engine's own stream
    watchdog so one wedged call fails the flow fast instead of stalling to the 300s test
    backstop; ``None`` resolves from ``LLM_EVAL_CALL_TIMEOUT`` or the default. The scripted
    path never hangs and runs with the watchdog inert.

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
    # LIVE-eval fidelity (real model only): a "present" repo state must carry the REAL config tree
    # so capacity tools reach the canned bridge instead of erroring on a missing defaults.yaml — what
    # the gated/capacity flows already document ("in LIVE eval the REAL repo is present"). The
    # scripted golden path is left byte-for-byte unchanged, and an "absent" repo
    # state (testing the clone) is never pre-populated.
    if live and flow.repo_state != "absent":
        _materialize_real_bench_config(repos_dir)

    settings = Settings(
        _env_file=None,                       # fully hermetic — ignore the developer's .env
        repos_dir=repos_dir,
        workspace_dir=tmp_path / "ws",
        simulate=simulate,
    )
    policy = CommandPolicy.from_file(settings.command_policy_path)
    # LIVE eval on the absent flow (kind-quickstart): the agent clones + installs mid-run, so the
    # fake clone/install must leave the config tree + venv behind (a real one does) or the read-only
    # pre-flight tools contradict the just-completed setup — see CaptureRunner._maybe_hydrate_bench_repo.
    hydrate_bench = live and flow.repo_state == "absent"
    runner = CaptureRunner(settings.repo_paths, canned=flow.canned, hydrate_bench=hydrate_bench)
    workspace = settings.resolved_workspace_dir / "sessions" / "flow"
    ctx = ToolContext(settings=settings, policy=policy, runner=runner, workspace=workspace)
    # Pin the catalog to the frozen snapshot. Setting the field is not enough: tools like
    # ensure_repos call ctx.catalog(refresh=True), which would re-scan the empty fake repo
    # and wipe it. Shadow the method so every lookup returns the snapshot.
    frozen = frozen_catalog()
    ctx._catalog = frozen
    ctx.catalog = lambda *, refresh=False: frozen
    session = Session(id="flow", ctx=ctx)

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
    with patch("app.tools.setup.probe.shutil.which", side_effect=fake_which):
        if live:
            # No factory → the real logged-in CLI; the engine's stream watchdog is the
            # live-eval fail-fast (a stall interrupts + errors instead of hanging the flow).
            timeout_s = _resolve_call_timeout(call_timeout)
            engine = SdkNativeEngine(stream_watchdog_s=timeout_s if timeout_s > 0 else 0.0)
        else:
            script = sdk_script(flow.turns)
            engine = SdkNativeEngine(transport_factory=lambda: FakeTransport(script))
        await engine.run_turn(
            session, flow.mock_user_input, emit=emit, request_approval=request_approval)

    # Label each captured command with its real policy mode + whether it was gated.
    cat = catalog_for_policy(frozen_catalog())
    approved_argvs = [r["payload"].get("argv") for r in approval_requests if r["kind"] == "command"]
    commands: list[CapturedCommand] = []
    for call in runner.calls:
        argv = call["argv"]
        # run_shell (the agent's always-on ad-hoc `bash -lc` surface) is governed by the
        # read-only/mutating CLASSIFIER + approval gate, NOT the policy — which governs only the
        # DEDICATED command tools (see app/tools/run/shell.py + app/tools/CLAUDE.md). Validating a
        # run_shell command against the policy wrongly marks it "denied", which would trip the
        # bypass check in gating_problems and falsely fail any LIVE flow where the real model
        # improvises with run_shell. Classify it the way production does, so the SAME safety
        # invariant (mutating ⇒ approval-gated; read-only ⇒ auto-run) still applies — correctly — to it.
        if argv[:2] == ["bash", "-lc"] and len(argv) >= 3:
            mode = classify_shell_command(argv[2])
        else:
            d = policy.validate(argv, catalog=cat)
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
        ok, _ = False, notes.append("turn did not finish cleanly")
    if run.errors:
        ok, _ = False, notes.append(f"turn emitted errors: {run.errors}")

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

    This holds **unconditionally, SIMULATE included**: simulate previews a mutation (it no-ops
    the command) but does not waive its approval card, so a simulated walk is gated exactly like
    the live one."""
    problems: list[str] = []
    for c in run.commands:
        if c.mode == MUTATING and not c.approved:
            problems.append(f"mutating command was NOT approval-gated: {c.argv}")
        if c.mode == READ_ONLY and c.approved:
            problems.append(f"read-only command went through the approval gate (should auto-run): {c.argv}")
        if c.mode == "denied":
            problems.append(f"a denied command reached the runner (policy bypass!): {c.argv}")
    return problems


# Re-exported here to avoid a circular import in flows.py type hints.
@dataclass
class ExpectedCommand:
    argv: list[str]
    mode: str  # READ_ONLY | MUTATING
