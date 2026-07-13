"""Phase 29 — Explicit cluster access (``-k``/``--kubeconfig`` FILE, plus URL + TOKEN).

Target a REMOTE cluster instead of relying only on the ambient kube context. Hermetic: no
live cluster / kubeconfig / network. Covers the acceptance criteria:

  * a NON-DEFAULT kubeconfig FILE is threaded into the CLI call — ``build_argv`` emits it as
    ``-k <path>`` (pure mechanism), the policy permits + value-pins it on every
    cluster-touching subcommand, and ``ExecuteInput`` carries it as a top-level field;
  * the cluster URL + bearer TOKEN travel BACKEND-ONLY — they become the
    ``LLMDBENCH_CLUSTER_URL`` / ``LLMDBENCH_CLUSTER_TOKEN`` child-env vars (never argv), and the
    TOKEN NEVER reaches the browser: it is absent from every ``command`` event AND from the
    emitted child env surfaced to the UI;
  * the token is deliberately NOT expressible as an policy-allowed flag (so it can never be an
    argv token), mirroring the HF-token non-leak pattern.

The WHEN/WHICH-cluster judgment lives in knowledge/preconditions.md (asserted present).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.security.policy import MUTATING, READ_ONLY
from app.tools.run.execute import build_argv, execute_llmdbenchmark
from app.tools.schemas import ExecuteInput
from tests._helpers import _approve_all, _argv, _capture_ctx
from tests.flows.harness import CaptureRunner

KUBECONFIG = "/home/me/.kube/remote-staging.yaml"  # a non-default kubeconfig FILE
CLUSTER_URL = "https://api.staging.example.com:6443"
CLUSTER_TOKEN = "sha256~SUPER-SECRET-bearer-token-VALUE"  # the secret that must never leak

# Cluster-touching subcommands that accept -k (results is a local-report read; no -k).
KUBECONFIG_SUBCOMMANDS = ["standup", "plan", "run", "smoketest", "teardown", "experiment"]


# ---------------------------------------------------------------------------
# build_argv — kubeconfig (-k) emission (PURE MECHANISM)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("subcommand", KUBECONFIG_SUBCOMMANDS)
def test_kubeconfig_emits_short_k_flag_on_every_subcommand(subcommand):
    argv = build_argv(subcommand, spec="cicd/kind", kubeconfig=KUBECONFIG)
    assert "-k" in argv
    # -k is a value flag: the exact path immediately follows it.
    assert argv[argv.index("-k") + 1] == KUBECONFIG
    # It follows the subcommand (a post-subcommand flag, like -m).
    assert argv.index("-k") > argv.index(subcommand)


@pytest.mark.parametrize("subcommand", KUBECONFIG_SUBCOMMANDS)
def test_kubeconfig_unset_emits_no_flag(subcommand):
    # No kubeconfig => the ambient context stands; we never inject -k the agent didn't set.
    argv_none = build_argv(subcommand, spec="cicd/kind", kubeconfig=None)
    argv_default = build_argv(subcommand, spec="cicd/kind")
    for argv in (argv_none, argv_default):
        assert "-k" not in argv


def test_kubeconfig_does_not_disturb_other_args():
    argv = build_argv(
        "run", spec="cicd/kind", harness="inference-perf", workload="sanity_random.yaml",
        models="facebook/opt-125m", kubeconfig=KUBECONFIG, flags={"output": "local"},
    )
    assert argv[:3] == ["llmdbenchmark", "--spec", "cicd/kind"]
    assert argv[argv.index("-k") + 1] == KUBECONFIG
    # The model override + harness/workload/output are all still intact.
    assert argv[argv.index("-m") + 1] == "facebook/opt-125m"
    assert "-l" in argv and "inference-perf" in argv
    assert "-w" in argv and "sanity_random.yaml" in argv
    assert "-r" in argv and "local" in argv


def test_execute_schema_accepts_top_level_kubeconfig_field():
    m = ExecuteInput(subcommand="standup", spec="cicd/kind", kubeconfig=KUBECONFIG)
    assert m.kubeconfig == KUBECONFIG
    # It is a TOP-LEVEL field (parallel to models), NOT buried in flags.
    assert m.flags is None


def test_execute_schema_kubeconfig_defaults_to_none():
    assert ExecuteInput(subcommand="standup", spec="cicd/kind").kubeconfig is None


# ---------------------------------------------------------------------------
# policy — -k/--kubeconfig permitted + value-pinned (DATA); no token flag
# ---------------------------------------------------------------------------


# Per-subcommand minimal extra args so the argv validates cleanly.
_EXTRA = {
    "standup": [], "plan": [], "smoketest": [], "teardown": [],
    "run": ["-l", "inference-perf", "-w", "sanity_random.yaml"],
    "experiment": ["-e", "workspace/exp.yaml"],
}


@pytest.mark.parametrize("subcommand", KUBECONFIG_SUBCOMMANDS)
def test_policy_permits_kubeconfig_short_and_long(policy, catalog, subcommand):
    for flag in ("-k", "--kubeconfig"):
        d = policy.validate(_argv(subcommand, *_EXTRA[subcommand], flag, KUBECONFIG), catalog=catalog)
        assert d.allowed, f"{flag} should be allowed on {subcommand}: {d.reason}"


def test_kubeconfig_value_constraint_is_pinned(policy, catalog):
    # A normal path passes; a traversal path and a metachar-laden injection are REFUSED.
    assert policy.validate(_argv("standup", "-k", KUBECONFIG), catalog=catalog).allowed
    assert not policy.validate(
        _argv("standup", "-k", "../../etc/passwd"), catalog=catalog
    ).allowed, "no '..' traversal must be expressible"
    assert not policy.validate(
        _argv("standup", "--kubeconfig", "x; rm -rf /"), catalog=catalog
    ).allowed


def test_kubeconfig_does_not_change_mode_classification(policy, catalog):
    # standup stays mutating with -k; plan stays a read-only preview with -k.
    assert policy.validate(_argv("standup", "-k", KUBECONFIG), catalog=catalog).mode == MUTATING
    assert policy.validate(_argv("plan", "-k", KUBECONFIG), catalog=catalog).mode == READ_ONLY
    # --dry-run still downgrades a remote-cluster standup to a read-only preview.
    d = policy.validate(_argv("standup", "-k", KUBECONFIG, "--dry-run"), catalog=catalog)
    assert d.allowed and d.mode == READ_ONLY


def test_no_cluster_token_or_url_flag_is_policy_allowed():
    """The token must never be expressible as an argv token: NO flag KEY anywhere in the
    llmdbenchmark policy mentions a token or a cluster url/token. We inspect the actual loaded
    policy's flag NAMES (not a substring of the whole YAML — our own explanatory comments
    legitimately mention LLMDBENCH_CLUSTER_TOKEN as the backend-only env path)."""
    import yaml

    doc = yaml.safe_load(COMMAND_POLICY_TEXT)
    llmd = doc["executables"]["llmdbenchmark"]
    flag_keys: set[str] = set(llmd.get("global_flags", {}))
    for sub in llmd.get("subcommands", {}).values():
        flag_keys |= set(sub.get("flags", {}))
    for key in flag_keys:
        low = key.lower()
        assert "token" not in low, f"flag {key!r} must NOT exist (token must never be an argv token)"
        assert "cluster-url" not in low and "cluster_url" not in low, f"flag {key!r} leaks the cluster url surface"
    # The kubeconfig FILE flag IS present (the permitted, non-secret lever).
    assert "-k" in flag_keys or "--kubeconfig" in flag_keys


# ---------------------------------------------------------------------------
# execute_llmdbenchmark — URL/TOKEN ride backend-only child env (NEVER argv)
# ---------------------------------------------------------------------------


def _ctx(tmp_path, *, emit=None):
    return _capture_ctx(tmp_path, emit=emit, approve=_approve_all)


def _last_run_call(runner: CaptureRunner):
    return next(c for c in reversed(runner.calls) if c["argv"][:1] == ["llmdbenchmark"])


async def test_cluster_url_and_token_reach_child_env_not_argv(tmp_path):
    ctx, runner = _ctx(tmp_path)
    await execute_llmdbenchmark(
        ctx, subcommand="run", spec="cicd/kind", namespace="llmd",
        harness="inference-perf", workload="sanity_random.yaml",
        flags={"cluster_url": CLUSTER_URL, "cluster_token": CLUSTER_TOKEN},
    )
    call = _last_run_call(runner)
    # They are ENV VARS carried backend-only — never argv tokens.
    assert call["extra_env"] == {
        "LLMDBENCH_CLUSTER_URL": CLUSTER_URL,
        "LLMDBENCH_CLUSTER_TOKEN": CLUSTER_TOKEN,
    }
    # The token (and url) are absent from the argv entirely.
    joined = " ".join(call["argv"])
    assert CLUSTER_TOKEN not in joined and CLUSTER_URL not in joined
    assert not any(tok in ("--cluster-token", "--cluster-url", "--token", "-k") for tok in call["argv"])


async def test_kubeconfig_file_threads_into_argv_no_env(tmp_path):
    """The non-default kubeconfig FILE is threaded as the -k ARGV flag (non-secret path),
    not as a child-env var — the flag is the single source for the file-path case."""
    ctx, runner = _ctx(tmp_path)
    await execute_llmdbenchmark(
        ctx, subcommand="standup", spec="cicd/kind", namespace="llmd", kubeconfig=KUBECONFIG,
    )
    call = _last_run_call(runner)
    assert "-k" in call["argv"] and call["argv"][call["argv"].index("-k") + 1] == KUBECONFIG
    # No env overlay for the file-path case (nothing else set).
    assert call["extra_env"] is None


async def test_kubeconfig_file_and_url_token_compose(tmp_path):
    """A run can carry BOTH a kubeconfig file (argv) and harness sizing (env), with the
    token still backend-only — the env overlay merges all backend-only keys together."""
    ctx, runner = _ctx(tmp_path)
    await execute_llmdbenchmark(
        ctx, subcommand="run", spec="cicd/kind", namespace="llmd",
        harness="inference-perf", workload="sanity_random.yaml", kubeconfig=KUBECONFIG,
        flags={"cluster_token": CLUSTER_TOKEN, "harness_cpu_nr": 3},
    )
    call = _last_run_call(runner)
    assert "-k" in call["argv"]  # file path in argv
    # Token + the sizing env both ride the SAME backend-only overlay.
    assert call["extra_env"]["LLMDBENCH_CLUSTER_TOKEN"] == CLUSTER_TOKEN
    assert call["extra_env"]["LLMDBENCH_HARNESS_CPU_NR"] == "3"


async def test_token_reaches_real_built_child_env(tmp_path):
    """End-to-end at the runner boundary: the per-run override merges LAST into the built env,
    so the real child process would carry LLMDBENCH_CLUSTER_TOKEN (forwarded by the LLMDBENCH_*
    passthrough), while the runner still scrubs LLM/API secrets."""
    from app.security.runner import CommandRunner

    runner = CommandRunner({})
    env = runner._build_env({"LLMDBENCH_CLUSTER_TOKEN": CLUSTER_TOKEN, "LLMDBENCH_CLUSTER_URL": CLUSTER_URL})
    assert env["LLMDBENCH_CLUSTER_TOKEN"] == CLUSTER_TOKEN
    assert env["LLMDBENCH_CLUSTER_URL"] == CLUSTER_URL


# ---------------------------------------------------------------------------
# THE SCRUB INVARIANT — the token NEVER reaches the browser
# ---------------------------------------------------------------------------


async def test_token_never_appears_in_browser_command_events(tmp_path):
    """The headline acceptance: the cluster token must NOT appear in any browser-facing
    `command` event (which carries only argv/text/mode/auto_run), even though it IS applied to
    the backend child env. No event payload carries an env/extra_env key at all."""
    events: list[tuple[str, dict]] = []

    async def emit(t, p):
        events.append((t, p))

    ctx, runner = _ctx(tmp_path, emit=emit)
    await execute_llmdbenchmark(
        ctx, subcommand="run", spec="cicd/kind", namespace="llmd",
        harness="inference-perf", workload="sanity_random.yaml",
        flags={"cluster_url": CLUSTER_URL, "cluster_token": CLUSTER_TOKEN},
    )
    # It DID reach the backend child env...
    assert _last_run_call(runner)["extra_env"]["LLMDBENCH_CLUSTER_TOKEN"] == CLUSTER_TOKEN
    # ...but the token is absent from EVERY emitted event (not just command events).
    for _t, p in events:
        blob = json.dumps(p)
        assert CLUSTER_TOKEN not in blob, f"token leaked into a {_t!r} event"
        assert "LLMDBENCH_CLUSTER_TOKEN" not in blob
    cmd_events = [p for (t, p) in events if t == "command"]
    assert cmd_events, "expected at least one command event"
    for p in cmd_events:
        assert "extra_env" not in p and "env" not in p
        assert CLUSTER_TOKEN not in p["text"]


# ---------------------------------------------------------------------------
# knowledge — the WHEN/WHICH-cluster judgment is a knowledge file, not Python
# ---------------------------------------------------------------------------

KNOWLEDGE_DIR = Path(__file__).resolve().parents[2] / "knowledge"
COMMAND_POLICY_TEXT = (Path(__file__).resolve().parents[2] / "security" / "command_policy.yaml").read_text()


def test_remote_cluster_knowledge_documents_the_levers_and_secret_rule():
    guide = KNOWLEDGE_DIR / "deploy/preconditions.md"
    text = guide.read_text()
    # Both levers are documented...
    assert "kubeconfig" in text and "-k" in text
    assert "cluster_url" in text and "cluster_token" in text
    assert "LLMDBENCH_CLUSTER_URL" in text and "LLMDBENCH_CLUSTER_TOKEN" in text
    # ...and the non-negotiable secret rule (never echo the token) is present.
    low = text.lower()
    assert "secret" in low and "never echo the token" in low
    assert "ambient" in low  # the default is the ambient context
