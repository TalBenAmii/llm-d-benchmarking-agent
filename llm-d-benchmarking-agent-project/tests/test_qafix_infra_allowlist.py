"""QA-fix infra tests: narrow allowlist entries for multi-cluster context recovery.

Finding real-1 07:20: with multiple kind clusters present the active kubectl context drifted to
a sibling cluster and the agent could not recover because `kind export kubeconfig` and
`kubectl config use-context` were both blocked. Both are read-only-ish (they only rewrite a
local kubeconfig / its active-context pointer — no cluster mutation), so they auto-run.

Also asserts the WSL2-realistic read-only kubectl deadline (real-1 00:15 / real-2 08:10): the
read-only kubectl probe subcommands now declare `timeout_s: 25` (DATA) so the runner stops
flooding the log with 12s timeouts on a slow-but-reachable apiserver.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.security.allowlist import READ_ONLY, Allowlist

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_CATALOG = {"specs": [], "harnesses": [], "workloads": []}


@pytest.fixture(scope="module")
def allowlist() -> Allowlist:
    # Loading also runs the governance + positional schema validators — a malformed edit would
    # raise here, so this fixture doubles as a "yaml still loads" guard.
    return Allowlist.from_file(PROJECT_ROOT / "security" / "allowlist.yaml")


def _v(allowlist, argv):
    return allowlist.validate(argv, catalog=_CATALOG)


# ---- kind export kubeconfig -------------------------------------------------
def test_kind_export_kubeconfig_by_name(allowlist):
    d = _v(allowlist, ["kind", "export", "kubeconfig", "--name", "ralph-real-1-x3"])
    assert d.allowed and d.mode == READ_ONLY


def test_kind_export_kubeconfig_with_kubeconfig_path(allowlist):
    d = _v(allowlist, ["kind", "export", "kubeconfig", "--name", "foo", "--kubeconfig", "/tmp/kc"])
    assert d.allowed and d.mode == READ_ONLY


def test_kind_export_rejects_unknown_positional(allowlist):
    # Only `kubeconfig` is a valid first positional for `export`.
    d = _v(allowlist, ["kind", "export", "logs"])
    assert not d.allowed


def test_kind_export_rejects_traversing_kubeconfig(allowlist):
    # kubeconfig_path forbids `..` traversal.
    d = _v(allowlist, ["kind", "export", "kubeconfig", "--kubeconfig", "../../etc/passwd"])
    assert not d.allowed


# ---- kubectl config use-context ---------------------------------------------
def test_kubectl_use_context(allowlist):
    d = _v(allowlist, ["kubectl", "config", "use-context", "kind-ralph-real-1-x3"])
    assert d.allowed and d.mode == READ_ONLY


def test_kubectl_existing_config_verbs_still_work(allowlist):
    for verb in ("current-context", "view", "get-contexts"):
        d = _v(allowlist, ["kubectl", "config", verb])
        assert d.allowed and d.mode == READ_ONLY, verb


def test_kubectl_config_rejects_unknown_verb(allowlist):
    d = _v(allowlist, ["kubectl", "config", "set-credentials", "bad"])
    assert not d.allowed


def test_kubectl_use_context_rejects_dangerous_context(allowlist):
    # The metacharacter screen still rejects shell-dangerous tokens.
    d = _v(allowlist, ["kubectl", "config", "use-context", "foo;rm -rf /"])
    assert not d.allowed


# ---- WSL2-realistic read-only kubectl deadline ------------------------------
@pytest.mark.parametrize("argv", [
    ["kubectl", "config", "current-context"],
    ["kubectl", "cluster-info"],
    ["kubectl", "version", "--client"],
    ["kubectl", "get", "pods", "-n", "llm-d", "-o", "json"],
    ["kubectl", "top", "nodes"],
])
def test_readonly_kubectl_probes_have_wsl2_deadline(allowlist, argv):
    d = _v(allowlist, argv)
    assert d.allowed and d.mode == READ_ONLY
    # The YAML timeout_s (which OVERRIDES the probe tool's 12s caller timeout) is the WSL2-realistic 25s.
    assert d.timeout_s == 25, argv


def test_mutating_kubectl_keeps_default_deadline(allowlist):
    # apply/delete intentionally do NOT get the read-only probe deadline — they keep their own
    # (unset → runner global default) so a long apply isn't artificially capped.
    d = _v(allowlist, ["kubectl", "apply", "-f", "job.yaml"])
    assert d.allowed and d.mode != READ_ONLY
    assert d.timeout_s is None
