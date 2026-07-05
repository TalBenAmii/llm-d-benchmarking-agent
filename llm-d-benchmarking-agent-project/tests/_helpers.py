"""Shared test-input builders.

Verbatim ToolContext / Session constructors that were previously copy-pasted across many test
modules. They build *inputs* only (no assertions), so centralizing them changes no behavior — each
test still exercises the same code paths. File-local helpers that look similar but differ in logic
(e.g. the capacity-gated ``_real_repo_ctx`` or the sweep-tool ``_argv``) are intentionally NOT here.
"""
from __future__ import annotations

import copy
from pathlib import Path

import yaml

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
    return _capture_ctx(tmp_path, emit=emit, approve=_approve_all,
                        canned={"kubectl get nodes": nodes_json})


def _capture_ctx(tmp_path, *, emit=None, approve=None, canned=None):
    """A ToolContext on a CaptureRunner + frozen catalog over an isolated temp workspace.

    The verbatim builder several tool tests copy-pasted: a fake-repo Settings, a CaptureRunner
    (fakes the bridge subprocess so no real venv/tool is needed), and the catalog pinned to the
    frozen snapshot so validate()'s ref checks never scan the empty fake repo. ``approve`` becomes
    ``request_approval`` (default None → no approval channel, for read-only-only callers).
    ``canned`` forwards canned command outputs to the CaptureRunner (default None → none)."""
    settings = Settings(_env_file=None, repos_dir=tmp_path / "repos", workspace_dir=tmp_path / "ws")
    runner = CaptureRunner(settings.repo_paths, canned=canned or {})
    ctx = ToolContext(
        settings=settings,
        allowlist=Allowlist.from_file(settings.allowlist_path),
        runner=runner,
        workspace=tmp_path / "ws",
        emit=emit,
        request_approval=approve,
    )
    frozen = frozen_catalog()
    ctx._catalog = frozen
    ctx.catalog = lambda *, refresh=False: frozen
    return ctx, runner


def _session(tmp_path, *, sid="t") -> Session:
    s = get_settings()
    al = Allowlist.from_file(PROJECT_ROOT / "security" / "allowlist.yaml")
    runner = CommandRunner(s.repo_paths)
    ctx = ToolContext(settings=s, allowlist=al, runner=runner, workspace=tmp_path / "ws")
    return Session(id=sid, ctx=ctx)


def write_br_report(dirpath, base: dict, *, ttft_s, out_rate, p99=None, uid=None,
                    harness=None, model=None):
    """Write a Benchmark Report v0.2 YAML into ``dirpath`` from ``base`` with the given metrics.

    The one shared builder behind the per-test ``_write_report`` shims: deep-copies ``base``,
    overrides the ttft mean (+ p99 when given), the output-token-rate mean, and the optional run
    uid / harness tool / model name, then dumps it to ``benchmark_report_v0.2.yaml``.
    """
    rep = copy.deepcopy(base)
    if uid is not None:
        rep["run"]["uid"] = uid
    if harness is not None:
        rep["scenario"]["load"]["standardized"]["tool"] = harness
    if model is not None:
        rep["scenario"]["stack"][0]["standardized"]["model"]["name"] = model
    agg = rep["results"]["request_performance"]["aggregate"]
    agg["latency"]["time_to_first_token"]["mean"] = ttft_s
    if p99 is not None:
        agg["latency"]["time_to_first_token"]["p99"] = p99
    agg["throughput"]["output_token_rate"]["mean"] = out_rate
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / "benchmark_report_v0.2.yaml").write_text(yaml.safe_dump(rep, sort_keys=False))


def kubectl_present(monkeypatch, *, target="app.readiness.probes"):
    """Force ``shutil.which('kubectl')`` to look present at ``target`` so the canned runner is
    reached on every host (the readiness probes guard on ``shutil.which``) with no real binary."""
    real_which = __import__("shutil").which

    def fake_which(name, *a, **k):
        return "/usr/bin/kubectl" if name == "kubectl" else real_which(name, *a, **k)

    monkeypatch.setattr(f"{target}.shutil.which", fake_which)
