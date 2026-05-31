"""Subprocess runner: turns a validated *logical* argv into a real invocation and
executes it with ``shell=False``, a pinned cwd, a scrubbed environment, and a timeout.

The runner trusts that argv has already passed :class:`~app.security.allowlist.Allowlist`.
It NEVER constructs a shell string, so command injection is structurally impossible.
Secrets (LLM API keys) are excluded from the child environment.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

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
        else:
            which = shutil.which(exe)
            if which is None:
                raise RunnerError(f"{exe!r} not found on PATH")
            real = [which, *rest]
        return real, cwd

    # ---- environment ------------------------------------------------------
    def _build_env(self) -> dict[str, str]:
        env = {k: os.environ[k] for k in _ENV_PASSTHROUGH if k in os.environ}
        # Benchmark configuration vars are safe to forward; secrets are not among them.
        for k, v in os.environ.items():
            if k.startswith("LLMDBENCH_"):
                env[k] = v
        env.update(self._extra_env)  # e.g. HF_TOKEN, explicitly provided to the backend
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
    ) -> RunResult:
        """Resolve, run, and stream a command. Lines (stdout+stderr merged) are passed
        to ``on_line`` as they arrive and always captured (tail-bounded) in the result.
        ``cwd`` overrides the resolved working directory (e.g. clone target dir)."""
        real_argv, resolved_cwd = self.resolve(logical_argv, entry)
        cwd = str(cwd) if cwd is not None else resolved_cwd
        env = self._build_env()
        deadline = timeout if timeout is not None else self._default_timeout

        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *real_argv,
                cwd=cwd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError as exc:
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
            await asyncio.wait_for(_pump(), timeout=deadline)
            await proc.wait()
        except asyncio.TimeoutError:
            timed_out = True
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()

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
