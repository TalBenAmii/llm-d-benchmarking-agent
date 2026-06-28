"""metrics-server pre-flight fact in probe_environment — the deterministic "do we have live
resource stats?" signal that lets the agent OFFER the install BEFORE a run (instead of the old
mid-run button that collided with the in-flight-turn guard).

MECHANISM ONLY: facts (available/installed/ready_replicas). WHETHER/when to offer the install is
the agent's HARD_RULE + knowledge/observability.md — there is no install branch in the probe.
No live cluster, no network — `kubectl` is mocked via shutil.which + a CaptureRunner."""
from __future__ import annotations

import json
from unittest.mock import patch

from app.agent.prompt import HARD_RULES
from app.config import Settings
from app.security.allowlist import Allowlist
from app.tools.context import ToolContext
from app.tools.probe import probe_environment
from tests.flows.catalog_snapshot import frozen_catalog
from tests.flows.harness import CannedResult, CaptureRunner

_DEPLOY_READY = json.dumps({"items": [{"metadata": {"name": "metrics-server"},
                                       "status": {"availableReplicas": 1}}]})
_DEPLOY_NOTREADY = json.dumps({"items": [{"metadata": {"name": "metrics-server"},
                                          "status": {"availableReplicas": 0}}]})
_DEPLOY_ABSENT = json.dumps({"items": []})
_GET_DEPLOY_ARGV = ["kubectl", "get", "deployment", "-n", "kube-system",
                    "-l", "k8s-app=metrics-server", "-o", "json"]


def _ctx(tmp_path, *, canned):
    settings = Settings(_env_file=None, repos_dir=tmp_path / "repos", workspace_dir=tmp_path / "ws")
    runner = CaptureRunner(settings.repo_paths, canned=canned)
    ctx = ToolContext(
        settings=settings,
        allowlist=Allowlist.from_file(settings.allowlist_path),
        runner=runner,
        workspace=tmp_path / "ws",
    )
    frozen = frozen_catalog()
    ctx._catalog = frozen
    ctx.catalog = lambda *, refresh=False: frozen
    return ctx, runner


async def test_metrics_server_available(tmp_path):
    """`kubectl top nodes` succeeds → available True; the Deployment is present + ready."""
    ctx, runner = _ctx(tmp_path, canned={
        "top nodes": "NAME   CPU   MEM\nnode1  100m  500Mi\n",
        "get deployment": _DEPLOY_READY,
    })
    with patch("app.tools.probe.shutil.which", side_effect=lambda n, *a, **k: f"/usr/bin/{n}"):
        out = await probe_environment(ctx, checks=["metrics_server"])
    assert out["metrics_server"] == {"available": True, "installed": True, "ready_replicas": 1}
    argvs = [c["argv"] for c in runner.calls]
    assert ["kubectl", "top", "nodes"] in argvs
    assert _GET_DEPLOY_ARGV in argvs  # label-selector form (get permits one positional)


async def test_metrics_server_absent(tmp_path):
    """No metrics-server: `kubectl top` fails (Metrics API not available) and no Deployment."""
    ctx, _ = _ctx(tmp_path, canned={
        "top nodes": CannedResult(output="error: Metrics API not available", exit_code=1),
        "get deployment": _DEPLOY_ABSENT,
    })
    with patch("app.tools.probe.shutil.which", side_effect=lambda n, *a, **k: f"/usr/bin/{n}"):
        out = await probe_environment(ctx, checks=["metrics_server"])
    assert out["metrics_server"] == {"available": False, "installed": False, "ready_replicas": None}


async def test_metrics_server_installed_but_not_ready(tmp_path):
    """kind gotcha: installed WITHOUT --kubelet-insecure-tls — Deployment exists but
    availableReplicas 0 and `kubectl top` still fails, so the agent can phrase it precisely."""
    ctx, _ = _ctx(tmp_path, canned={
        "top nodes": CannedResult(output="error: Metrics API not available", exit_code=1),
        "get deployment": _DEPLOY_NOTREADY,
    })
    with patch("app.tools.probe.shutil.which", side_effect=lambda n, *a, **k: f"/usr/bin/{n}"):
        out = await probe_environment(ctx, checks=["metrics_server"])
    assert out["metrics_server"] == {"available": False, "installed": True, "ready_replicas": 0}


async def test_metrics_server_no_kubectl(tmp_path):
    """No kubectl on PATH → degrade to all-absent, no raise, no command issued."""
    ctx, runner = _ctx(tmp_path, canned={})
    with patch("app.tools.probe.shutil.which", return_value=None):
        out = await probe_environment(ctx, checks=["metrics_server"])
    assert out["metrics_server"] == {"available": False, "installed": False, "ready_replicas": None}
    assert runner.calls == []


def test_hard_rule_drives_the_pre_run_offer():
    """The offer is guaranteed by a HARD_RULE (not buried playbook prose): the system prompt
    references the probe fact AND the vetted install command, so the agent offers before running.
    It also presents Grafana as the richer alternative the agent CAN deploy (approval-gated), not as
    an advice-only / can't-do-it-for-you surface, keyed off the probe fact."""
    assert "metrics_server" in HARD_RULES
    assert "install_metrics_server.sh" in HARD_RULES
    # The pre-run offer presents BOTH live-view options as a pair, and the rule must say the agent
    # CAN stand Grafana up for the user (no false refusal) — only the env var is the user's to set.
    assert "Grafana" in HARD_RULES
    assert "stand this up for them" in HARD_RULES  # deployable by the agent, not advice-only
    assert "GRAFANA_DASHBOARD_URL" in HARD_RULES
    assert "grafana_dashboard.configured" in HARD_RULES


def _grafana_ctx(tmp_path, *, grafana_url=""):
    """A ToolContext whose Settings carry a chosen GRAFANA_DASHBOARD_URL (default unset)."""
    settings = Settings(_env_file=None, repos_dir=tmp_path / "repos",
                        workspace_dir=tmp_path / "ws", grafana_dashboard_url=grafana_url)
    runner = CaptureRunner(settings.repo_paths, canned={})
    ctx = ToolContext(
        settings=settings,
        allowlist=Allowlist.from_file(settings.allowlist_path),
        runner=runner,
        workspace=tmp_path / "ws",
    )
    frozen = frozen_catalog()
    ctx._catalog = frozen
    ctx.catalog = lambda *, refresh=False: frozen
    return ctx, runner


async def test_grafana_dashboard_configured(tmp_path):
    """GRAFANA_DASHBOARD_URL set → grafana_dashboard.configured True. Pure config introspection:
    NO cluster read is issued (the agent uses this to tailor its pre-run Grafana offer)."""
    ctx, runner = _grafana_ctx(tmp_path, grafana_url="https://grafana.example/d/llm-d/overview")
    out = await probe_environment(ctx, checks=["grafana_dashboard"])
    assert out["grafana_dashboard"] == {"configured": True}
    assert runner.calls == []  # config-only, never a kubectl call


async def test_grafana_dashboard_unconfigured(tmp_path):
    """Unset (default) → configured False; a whitespace-only value is treated as unset (stripped)."""
    ctx, _ = _grafana_ctx(tmp_path, grafana_url="   ")
    out = await probe_environment(ctx, checks=["grafana_dashboard"])
    assert out["grafana_dashboard"] == {"configured": False}
