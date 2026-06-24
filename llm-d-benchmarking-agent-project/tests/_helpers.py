"""Shared test-input builders.

Verbatim ToolContext / Session constructors that were previously copy-pasted across many test
modules. They build *inputs* only (no assertions), so centralizing them changes no behavior — each
test still exercises the same code paths. File-local helpers that look similar but differ in logic
(e.g. the capacity-gated ``_real_repo_ctx`` or the sweep-tool ``_argv``) are intentionally NOT here.
"""
from __future__ import annotations

from pathlib import Path

from app.agent.session import Session
from app.config import Settings, get_settings
from app.security.allowlist import Allowlist
from app.security.runner import CommandRunner
from app.tools.context import ToolContext
from tests.flows.catalog_snapshot import frozen_catalog
from tests.flows.harness import CaptureRunner

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST_PATH = PROJECT_ROOT / "security" / "allowlist.yaml"


async def _approve_all(kind, payload):
    return True


def _argv(subcommand, *rest):
    return ["llmdbenchmark", "--spec", "cicd/kind", subcommand, *rest]


def _real_repo_ctx(tmp_path, *, canned=None):
    """A ToolContext wired to the REAL repos/allowlist but with a CaptureRunner that fakes the
    bridge subprocess (CaptureRunner bypasses path resolution, so no real venv/tool is needed). No
    approval channel — the read-only tools that use this must auto-run."""
    s = get_settings()
    runner = CaptureRunner(s.repo_paths, canned=canned or {})
    emitted: list = []

    async def emit(t, p):
        emitted.append((t, p))

    ctx = ToolContext(
        settings=s,
        allowlist=Allowlist.from_file(ALLOWLIST_PATH),
        runner=runner,
        workspace=tmp_path / "ws",
        emit=emit,
    )
    return ctx, runner, emitted


def _ctx(tmp_path, *, nodes_json: str, emit=None):
    settings = Settings(_env_file=None, repos_dir=tmp_path / "repos", workspace_dir=tmp_path / "ws")
    runner = CaptureRunner(settings.repo_paths, canned={"kubectl get nodes": nodes_json})
    ctx = ToolContext(
        settings=settings,
        allowlist=Allowlist.from_file(settings.allowlist_path),
        runner=runner,
        workspace=tmp_path / "ws",
        emit=emit,
        request_approval=_approve_all,
    )
    frozen = frozen_catalog()
    ctx._catalog = frozen
    ctx.catalog = lambda *, refresh=False: frozen
    return ctx, runner


def _session(tmp_path) -> Session:
    s = get_settings()
    al = Allowlist.from_file(PROJECT_ROOT / "security" / "allowlist.yaml")
    runner = CommandRunner(s.repo_paths)
    ctx = ToolContext(settings=s, allowlist=al, runner=runner, workspace=tmp_path / "ws")
    return Session(id="t", ctx=ctx)
