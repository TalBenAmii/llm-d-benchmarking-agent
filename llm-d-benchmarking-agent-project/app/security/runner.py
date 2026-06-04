"""Subprocess runner: turns a validated *logical* argv into a real invocation and
executes it with ``shell=False``, a pinned cwd, a scrubbed environment, and a timeout.

The runner trusts that argv has already passed :class:`~app.security.allowlist.Allowlist`.
It NEVER constructs a shell string, so command injection is structurally impossible.
Secrets (LLM API keys) are excluded from the child environment.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from app.config import PROJECT_ROOT
from app.paths import is_within

# Records emitted here automatically carry the turn's corr_id/session_id/tool via the
# logging ContextFilter installed at startup — no need to thread anything through (Phase 11).
log = logging.getLogger("app.security.runner")

# Environment keys allowed through to child processes. Notably EXCLUDES
# ANTHROPIC_API_KEY / OPENAI_API_KEY and anything else secret.
_ENV_PASSTHROUGH = (
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TERM",
    "KUBECONFIG", "TMPDIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "DOCKER_HOST", "container",
)

_MAX_CAPTURE_CHARS = 200_000  # keep a bounded tail in memory

OnLine = Callable[[str], Awaitable[None]]


class RunnerError(RuntimeError):
    pass


@dataclass
class RunResult:
    exit_code: int
    duration_s: float
    real_argv: list[str]
    cwd: str | None
    output: str = ""              # captured stdout+stderr (tail-bounded)
    timed_out: bool = False
    lines: list[str] = field(default_factory=list)


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the child's whole process group (it is its own session leader — see
    ``start_new_session`` below) so a command that double-forks a daemon doesn't leave a
    grandchild running. Falls back to killing just the child."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


class CommandRunner:
    def __init__(
        self,
        repo_paths: dict[str, Path],
        *,
        default_timeout: float = 7200.0,
        extra_env: dict[str, str] | None = None,
    ):
        self._repos = {k: Path(v) for k, v in repo_paths.items()}
        self._default_timeout = default_timeout
        self._extra_env = dict(extra_env or {})

    # ---- resolution -------------------------------------------------------
    def _resolve_repo_ref(self, ref: str) -> Path:
        if not ref.startswith("repo:"):
            raise RunnerError(f"bad repo ref {ref!r}")
        name, _, sub = ref[len("repo:"):].partition("/")
        base = self._repos.get(name)
        if base is None:
            raise RunnerError(f"unknown repo {name!r} (have: {sorted(self._repos)})")
        return base / sub if sub else base

    def resolve(self, logical_argv: list[str], entry: dict | None) -> tuple[list[str], str | None]:
        """Map a logical argv to (real_argv, cwd). ``entry`` is the allowlist policy
        entry for argv[0] (carries optional runner/cwd hints)."""
        if not logical_argv:
            raise RunnerError("empty argv")
        exe, rest = logical_argv[0], logical_argv[1:]
        entry = entry or {}
        runner = entry.get("runner", {})
        invoke = runner.get("invoke")

        cwd: str | None = None
        if entry.get("cwd_must_be"):
            cwd_path = self._resolve_repo_ref(entry["cwd_must_be"])
            if not cwd_path.is_dir():
                raise RunnerError(f"required cwd {cwd_path} does not exist")
            cwd = str(cwd_path)

        if invoke == "venv-bin":
            binpath = self._resolve_repo_ref(runner["venv"]) / "bin" / runner.get("bin", exe)
            if not binpath.exists():
                raise RunnerError(
                    f"{binpath} not found — the benchmark venv is not set up yet "
                    f"(run install.sh first)"
                )
            real = [str(binpath), *rest]
        elif invoke == "repo-script":
            script = self._resolve_repo_ref(entry["cwd_must_be"]) / runner["script"]
            if not script.exists():
                raise RunnerError(f"script {script} not found")
            real = [str(script), *rest]
        elif invoke == "project-script":
            # A vetted script shipped with the agent project (e.g. scripts/install_prereqs.sh),
            # resolved against the project root — not a cloned repo. The allowlist constrains
            # which script + flags may run; the script's own contents are the only commands it
            # can execute (the allowlist grants no raw apt-get/curl/sudo).
            script = (PROJECT_ROOT / runner["script"]).resolve()
            if not is_within(script, PROJECT_ROOT.resolve()):
                raise RunnerError(f"project script {runner['script']!r} escapes the project root")
            if not script.exists():
                raise RunnerError(f"project script {script} not found")
            # Optional: run the project script through a specific repo venv's Python (e.g.
            # the benchmark repo's venv, which is the only interpreter carrying the `planner`
            # package the capacity pre-flight imports). Still a vetted, project-shipped script
            # — the interpreter only changes which dependencies are importable, not the
            # command surface (the allowlist already pins the script + its single argument).
            python_via = runner.get("python_via")
            if python_via:
                py = self._resolve_repo_ref(python_via) / "bin" / "python"
                if not py.exists():
                    raise RunnerError(
                        f"{py} not found — the benchmark venv is not set up yet "
                        f"(run install.sh first)"
                    )
                real = [str(py), str(script), *rest]
            else:
                real = [str(script), *rest]
        else:
            which = shutil.which(exe)
            if which is None:
                raise RunnerError(f"{exe!r} not found on PATH")
            real = [which, *rest]
        return real, cwd

    # ---- environment ------------------------------------------------------
    def _build_env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = {k: os.environ[k] for k in _ENV_PASSTHROUGH if k in os.environ}
        # Benchmark configuration vars are safe to forward; secrets are not among them.
        for k, v in os.environ.items():
            if k.startswith("LLMDBENCH_"):
                env[k] = v
        env.update(self._extra_env)  # e.g. HF_TOKEN, explicitly provided to the backend
        # Per-execution, agent-chosen env (e.g. a right-sized LLMDBENCH_HARNESS_CPU_NR for a
        # small Kind node) is merged LAST so it wins over both os.environ and the global
        # _extra_env. Backend-only — it never reaches the browser (no command event carries env).
        if extra:
            env.update({str(k): str(v) for k, v in extra.items()})
        env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
        return env

    # ---- execution --------------------------------------------------------
    async def execute(
        self,
        logical_argv: list[str],
        entry: dict | None,
        *,
        on_line: OnLine | None = None,
        timeout: float | None = None,
        cwd: str | Path | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> RunResult:
        """Resolve, run, and stream a command. Lines (stdout+stderr merged) are passed
        to ``on_line`` as they arrive and always captured (tail-bounded) in the result.
        ``cwd`` overrides the resolved working directory (e.g. clone target dir).
        ``extra_env`` is a per-execution env overlay merged LAST into the built child env
        (backend-only — never emitted to the browser)."""
        real_argv, resolved_cwd = self.resolve(logical_argv, entry)
        cwd = str(cwd) if cwd is not None else resolved_cwd
        env = self._build_env(extra_env)
        deadline = timeout if timeout is not None else self._default_timeout

        start = time.monotonic()
        log.info("runner.exec.start", extra={"exe": real_argv[0] if real_argv else "", "cwd": cwd})
        try:
            proc = await asyncio.create_subprocess_exec(
                *real_argv,
                cwd=cwd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,  # own process group → we can reap grandchildren on timeout
            )
        except FileNotFoundError as exc:
            log.error("runner.exec.launch_failed", extra={"exe": real_argv[0] if real_argv else ""})
            raise RunnerError(f"failed to launch {real_argv[0]!r}: {exc}") from exc

        captured: list[str] = []
        cap_chars = 0
        timed_out = False

        async def _pump() -> None:
            nonlocal cap_chars
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                if cap_chars < _MAX_CAPTURE_CHARS:
                    captured.append(line)
                    cap_chars += len(line) + 1
                if on_line is not None:
                    await on_line(line)

        try:
            # Bound the WHOLE process lifecycle — both draining stdout AND the process exit —
            # under one deadline. A child that closes stdout without exiting (e.g. it
            # double-forks a daemon) must not hang here forever: that would pin a concurrency
            # slot (the caller may hold a run-cap semaphore around this call) indefinitely.
            await asyncio.wait_for(asyncio.gather(_pump(), proc.wait()), timeout=deadline)
        except TimeoutError:
            timed_out = True
            log.warning("runner.exec.timeout", extra={
                "exe": real_argv[0] if real_argv else "", "deadline_s": deadline})
            _kill_process_group(proc)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=5.0)  # brief grace to reap
        except asyncio.CancelledError:
            # The awaiting task was cancelled (Phase 16: a cancelled run/turn, or graceful
            # shutdown). Reap the child's whole process group so cancellation never ORPHANS a
            # subprocess (e.g. a long standup that double-forks a daemon). The slot the caller
            # holds around this call is released as cancellation unwinds the `async with`. Then
            # re-raise so the cancellation propagates to the turn task as normal.
            log.warning("runner.exec.cancelled", extra={"exe": real_argv[0] if real_argv else ""})
            _kill_process_group(proc)
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.shield(asyncio.wait_for(proc.wait(), timeout=5.0))  # reap, best-effort
            raise

        duration = time.monotonic() - start
        return RunResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            duration_s=round(duration, 2),
            real_argv=real_argv,
            cwd=cwd,
            output="\n".join(captured),
            timed_out=timed_out,
            lines=captured,
        )


class SimRunner(CommandRunner):
    """Dry-run runner: never spawns a process and never resolves paths, so a missing
    venv/repos can't raise. Every command is a synthetic success — it just streams a
    couple of "[simulate] …" lines and returns ``exit_code=0``. This mirrors the test
    harness's ``CaptureRunner`` but for the live app (SIMULATE mode), carrying no
    command-specific knowledge."""

    async def execute(
        self,
        logical_argv: list[str],
        entry: dict | None,
        *,
        on_line: OnLine | None = None,
        timeout: float | None = None,
        cwd: str | Path | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> RunResult:
        # ``extra_env`` is accepted for signature parity with CommandRunner.execute but never
        # used here — SimRunner spawns no process, so there is no child env to overlay.
        lines = [
            f"[simulate] (no-op) would run: {' '.join(logical_argv)}",
            "[simulate] exit_code=0",
        ]
        if on_line is not None:
            for line in lines:
                await on_line(line)
        return RunResult(
            exit_code=0,
            duration_s=0.0,
            real_argv=list(logical_argv),
            cwd=str(cwd) if cwd else None,
            output="\n".join(lines),
            lines=lines,
            timed_out=False,
        )
