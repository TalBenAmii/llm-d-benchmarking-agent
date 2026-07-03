"""Phase 30 — HuggingFace gated-model secret provisioning.

Hermetic: NO live cluster, NO real kubectl, NO network, NO GPU. The vetted
``scripts/provision_hf_secret.py`` script is exercised with ``subprocess.run`` monkeypatched
to a fake that RECORDS the kubectl argv + stdin it would have run, so we assert the upstream
two-stage `create secret ... --dry-run=client -o yaml | apply -f -` shape WITHOUT touching a
cluster. The tool layer runs end-to-end through a ``CaptureRunner`` (no subprocess at all),
asserting the mutating command is approval-gated and the configured ``HF_TOKEN`` never
appears in the tool input, the argv, the command events, or the result.

Acceptance covered:
  * the secret-provision command is allowlisted as MUTATING (approval-gated) + value-pinned;
  * the token is read from the env (never an argument) and never leaks into events/argv/result;
  * the upstream kubectl shape is reproduced exactly, idempotently;
  * non-gated/public flows are untouched (the tool is opt-in; the token stays absent unless used).
"""
from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.security.allowlist import MUTATING, Allowlist
from app.security.runner import CommandRunner
from app.tools.context import ToolContext
from app.tools.repos import provision_hf_secret
from app.tools.registry import REGISTRY, tool_definitions
from tests.flows.harness import CaptureRunner

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST_PATH = PROJECT_ROOT / "security" / "allowlist.yaml"
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "provision_hf_secret.py"

# A sentinel token the backend would hold. The point of the scrub assertions is that THIS
# string never leaks into argv, command events, or the structured result.
_FAKE_HF_TOKEN = "hf_SECRET_phase30_must_never_leak_0xC0FFEE"


# --------------------------------------------------------------------------- #
# Load the vetted script as a module (it is not on the package path).
# --------------------------------------------------------------------------- #
def _load_script():
    spec = importlib.util.spec_from_file_location("provision_hf_secret_script", SCRIPT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeKubectl:
    """Records each subprocess.run call (argv + stdin) and returns canned results, so the
    script's two kubectl stages run with NO real cluster."""

    def __init__(self, *, create_rc=0, apply_rc=0, rendered="apiVersion: v1\nkind: Secret\n"):
        self.calls: list[dict] = []
        self._create_rc = create_rc
        self._apply_rc = apply_rc
        self._rendered = rendered

    def run(self, argv, *, capture_output=False, text=False, check=False, input=None):
        self.calls.append({"argv": list(argv), "input": input})
        if argv[:2] == ["kubectl", "create"]:
            return SimpleNamespace(
                returncode=self._create_rc,
                stdout=self._rendered if self._create_rc == 0 else "",
                stderr="" if self._create_rc == 0 else "boom: invalid name",
            )
        if argv[:2] == ["kubectl", "apply"]:
            return SimpleNamespace(
                returncode=self._apply_rc,
                stdout="secret/llm-d-hf-token created" if self._apply_rc == 0 else "",
                stderr="" if self._apply_rc == 0 else "apply failed",
            )
        raise AssertionError(f"unexpected argv: {argv}")


# =========================================================================== #
# 1. The script: upstream kubectl shape, token from env (never argv), token-free output
# =========================================================================== #
def test_script_reproduces_upstream_two_stage_shape(monkeypatch):
    fake = _FakeKubectl()
    script = _load_script()
    monkeypatch.setattr(script.subprocess, "run", fake.run)
    monkeypatch.setenv("HF_TOKEN", _FAKE_HF_TOKEN)

    rc = script.main(["provision_hf_secret.py", "--namespace", "llmd-test", "--name", "llm-d-hf-token"])
    assert rc == 0
    assert len(fake.calls) == 2

    create, apply = fake.calls
    # Stage 1: render the manifest WITHOUT touching the cluster (--dry-run=client -o yaml),
    # mirroring llm-d/helpers/hf-token.md exactly.
    assert create["argv"] == [
        "kubectl", "create", "secret", "generic", "llm-d-hf-token",
        f"--from-literal=HF_TOKEN={_FAKE_HF_TOKEN}",
        "--namespace", "llmd-test",
        "--dry-run=client", "-o", "yaml",
    ]
    # Stage 2: apply the rendered manifest over stdin (the manifest, not the raw token, on argv).
    assert apply["argv"] == ["kubectl", "apply", "--namespace", "llmd-test", "-f", "-"]
    assert apply["input"] == "apiVersion: v1\nkind: Secret\n"
    assert _FAKE_HF_TOKEN not in " ".join(apply["argv"])


def test_script_defaults_name_to_upstream_default(monkeypatch):
    fake = _FakeKubectl()
    script = _load_script()
    monkeypatch.setattr(script.subprocess, "run", fake.run)
    monkeypatch.setenv("HF_TOKEN", _FAKE_HF_TOKEN)
    rc = script.main(["provision_hf_secret.py", "--namespace", "ns"])  # no --name
    assert rc == 0
    # HF_TOKEN_NAME default from llm-d/helpers/hf-token.md.
    assert fake.calls[0]["argv"][4] == "llm-d-hf-token"


def test_script_token_read_from_env_only_never_an_argument(monkeypatch):
    """The token is NEVER accepted as a flag/positional — argparse has no token arg, so a
    fabricated --token would be rejected. It only ever comes from the env."""
    fake = _FakeKubectl()
    script = _load_script()
    monkeypatch.setattr(script.subprocess, "run", fake.run)
    monkeypatch.setenv("HF_TOKEN", _FAKE_HF_TOKEN)
    with pytest.raises(SystemExit):  # argparse rejects the unknown --token flag
        script.main(["provision_hf_secret.py", "--namespace", "ns", "--token", "x"])


def test_script_fails_clean_without_token(monkeypatch, capsys):
    fake = _FakeKubectl()
    script = _load_script()
    monkeypatch.setattr(script.subprocess, "run", fake.run)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    rc = script.main(["provision_hf_secret.py", "--namespace", "ns"])
    assert rc == 1
    assert fake.calls == []  # never ran kubectl without a token
    err = capsys.readouterr().err
    assert "HF_TOKEN is not configured" in err
    assert _FAKE_HF_TOKEN not in err


def test_script_placeholder_token_treated_as_missing(monkeypatch):
    fake = _FakeKubectl()
    script = _load_script()
    monkeypatch.setattr(script.subprocess, "run", fake.run)
    monkeypatch.setenv("HF_TOKEN", "REPLACE_TOKEN")
    rc = script.main(["provision_hf_secret.py", "--namespace", "ns"])
    assert rc == 1 and fake.calls == []  # upstream placeholder is normalized to "no token"


def test_script_create_failure_is_token_free(monkeypatch, capsys):
    fake = _FakeKubectl(create_rc=1)
    script = _load_script()
    monkeypatch.setattr(script.subprocess, "run", fake.run)
    monkeypatch.setenv("HF_TOKEN", _FAKE_HF_TOKEN)
    rc = script.main(["provision_hf_secret.py", "--namespace", "ns"])
    assert rc == 1
    out = capsys.readouterr()
    assert len(fake.calls) == 1  # stopped before apply
    assert _FAKE_HF_TOKEN not in (out.out + out.err)


def test_script_apply_failure_surfaces_kubectl_error(monkeypatch, capsys):
    fake = _FakeKubectl(apply_rc=1)
    script = _load_script()
    monkeypatch.setattr(script.subprocess, "run", fake.run)
    monkeypatch.setenv("HF_TOKEN", _FAKE_HF_TOKEN)
    rc = script.main(["provision_hf_secret.py", "--namespace", "ns"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "apply failed" in err and _FAKE_HF_TOKEN not in err


def test_script_success_surfaces_only_kubectl_confirmation(monkeypatch, capsys):
    fake = _FakeKubectl()
    script = _load_script()
    monkeypatch.setattr(script.subprocess, "run", fake.run)
    monkeypatch.setenv("HF_TOKEN", _FAKE_HF_TOKEN)
    rc = script.main(["provision_hf_secret.py", "--namespace", "ns", "--name", "llm-d-hf-token"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "secret/llm-d-hf-token created" in out
    assert _FAKE_HF_TOKEN not in out  # the confirmation never echoes the token


# =========================================================================== #
# 2. The allowlist: the provision command is MUTATING (approval-gated) + value-pinned
# =========================================================================== #
@pytest.fixture
def allowlist():
    return Allowlist.from_file(ALLOWLIST_PATH)


def test_provision_command_is_mutating_and_allowed(allowlist):
    d = allowlist.validate(
        ["provision_hf_secret.py", "--namespace", "llmd-quickstart", "--name", "llm-d-hf-token"]
    )
    assert d.allowed
    assert d.mode == MUTATING  # writes a Secret → approval-gated, never auto-run
    assert d.requires_approval
    assert d.timeout_s == 120


def test_provision_command_allowed_without_name(allowlist):
    d = allowlist.validate(["provision_hf_secret.py", "--namespace", "ns"])
    assert d.allowed and d.mode == MUTATING


def test_provision_namespace_is_value_pinned(allowlist):
    # An RFC1123 violation (uppercase) is rejected by the namespace constraint.
    d = allowlist.validate(["provision_hf_secret.py", "--namespace", "Bad_NS"])
    assert not d.allowed


def test_provision_name_is_value_pinned(allowlist):
    d = allowlist.validate(["provision_hf_secret.py", "--namespace", "ns", "--name", "Bad Name!"])
    assert not d.allowed


def test_provision_rejects_token_bearing_arg(allowlist):
    """Even though the FLAG policy is relaxed (unknown flags accepted), an embedded
    `--from-literal=HF_TOKEN=...` carries shell-dangerous '=' value with metachars in a way
    the screen rejects — and critically, the allowlist NEVER adds a token flag here, so the
    only argv the agent can express carries --namespace/--name. Assert a literal token arg
    can't sneak through the metacharacter screen."""
    d = allowlist.validate([
        "provision_hf_secret.py", "--namespace", "ns",
        "--from-literal=HF_TOKEN=hf_x; rm -rf /",
    ])
    assert not d.allowed  # the `;`/`/` etc. trip the metacharacter screen


def test_kubectl_create_secret_subcommand_is_NOT_allowlisted(allowlist):
    """The plan's invariant: provisioning goes through the vetted script, NOT a raw
    `kubectl create secret` (which would put the token on an allowlisted argv → leak)."""
    d = allowlist.validate([
        "kubectl", "create", "secret", "generic", "llm-d-hf-token",
        "--from-literal=HF_TOKEN=" + _FAKE_HF_TOKEN, "--namespace", "ns",
    ])
    assert not d.allowed  # kubectl has no `create secret` surface


# =========================================================================== #
# 3. The tool end-to-end: approval-gated, token never in argv/events/result
# =========================================================================== #
def _ctx_with_token(tmp_path, *, approve=True):
    s = Settings(hf_token=_FAKE_HF_TOKEN, simulate=False, _env_file=None)
    runner = CaptureRunner(s.repo_paths)
    emitted: list = []
    approvals: list = []

    async def emit(t, p):
        emitted.append((t, p))

    async def request_approval(kind, payload):
        approvals.append({"kind": kind, "payload": payload})
        return approve

    ctx = ToolContext(
        settings=s,
        allowlist=Allowlist.from_file(ALLOWLIST_PATH),
        runner=runner,
        workspace=tmp_path / "ws",
        emit=emit,
        request_approval=request_approval,
    )
    return ctx, runner, emitted, approvals


async def test_tool_provisions_and_is_approval_gated(tmp_path):
    ctx, runner, emitted, approvals = _ctx_with_token(tmp_path)
    res = await provision_hf_secret(ctx, namespace="llmd-quickstart", name="llm-d-hf-token")

    assert res["provisioned"] is True
    assert res["namespace"] == "llmd-quickstart" and res["name"] == "llm-d-hf-token"
    # It went through the approval gate (mutating).
    assert approvals and approvals[0]["kind"] == "command"
    # The one captured command is the vetted script with ONLY namespace/name on the argv.
    assert len(runner.calls) == 1
    argv = runner.calls[0]["argv"]
    assert argv == ["provision_hf_secret.py", "--namespace", "llmd-quickstart", "--name", "llm-d-hf-token"]
    # The token is configured backend-side (would be injected into the child env)...
    assert ctx.settings.extra_subprocess_env["HF_TOKEN"] == _FAKE_HF_TOKEN
    # ...but NOT on the argv we built.
    assert _FAKE_HF_TOKEN not in " ".join(argv)


async def test_tool_defaults_name_when_omitted(tmp_path):
    ctx, runner, _, _ = _ctx_with_token(tmp_path)
    res = await provision_hf_secret(ctx, namespace="ns")
    assert res["name"] == "llm-d-hf-token"
    assert runner.calls[0]["argv"][-1] == "llm-d-hf-token"


async def test_tool_rejected_when_user_declines(tmp_path):
    from app.tools.context import ApprovalRejected

    ctx, runner, _, _ = _ctx_with_token(tmp_path, approve=False)
    with pytest.raises(ApprovalRejected):
        await provision_hf_secret(ctx, namespace="ns")
    # Rejected at the gate — nothing ran.
    assert runner.calls == []


async def test_token_absent_from_argv_events_and_result(tmp_path):
    """SECRET SCRUB: the configured HF token appears NOWHERE the agent or browser can see."""
    ctx, runner, emitted, approvals = _ctx_with_token(tmp_path)
    res = await provision_hf_secret(ctx, namespace="llmd-quickstart")

    blob = json.dumps({
        "result": res,
        "events": emitted,
        "approvals": approvals,
        "calls": runner.calls,
    }, default=str)
    assert _FAKE_HF_TOKEN not in blob
    # A command event WAS emitted (post-approval) and it is mutating, not auto-run.
    cmd_events = [p for t, p in emitted if t == "command"]
    assert cmd_events and all(e["mode"] == MUTATING for e in cmd_events)
    assert all(e["auto_run"] is False for e in cmd_events)
    assert all(_FAKE_HF_TOKEN not in e["text"] for e in cmd_events)


# =========================================================================== #
# 4. Registration + non-gated flows unchanged
# =========================================================================== #
def test_tool_is_registered_with_schema():
    assert "provision_hf_secret" in REGISTRY
    defs = {d["name"]: d for d in tool_definitions()}
    assert "provision_hf_secret" in defs
    schema = defs["provision_hf_secret"]["input_schema"]
    assert schema["type"] == "object"
    # namespace is required; name is optional (defaults to llm-d-hf-token in the handler).
    assert "namespace" in schema["properties"] and "namespace" in schema.get("required", [])
    assert "name" in schema["properties"] and "name" not in schema.get("required", [])
    # The token is NOT an input — it stays backend-only.
    assert "token" not in schema["properties"] and "hf_token" not in schema["properties"]


def test_non_gated_flow_does_not_provision(tmp_path):
    """A public/non-gated flow simply never calls the tool — provisioning is opt-in and
    leaves the token unused. Asserting the tool is a no-op unless explicitly invoked: a
    fresh context with NO provision call records no command and exposes no token."""
    ctx, runner, emitted, _ = _ctx_with_token(tmp_path)
    # Nothing invoked → no commands, no events carry the token.
    assert runner.calls == []
    assert _FAKE_HF_TOKEN not in json.dumps(emitted, default=str)


# =========================================================================== #
# 5. The REAL runner exec path — the script must be directly executable.
#
# The allowlist entry is `invoke: project-script` with NO `python_via`, so
# app/security/runner.py builds real=[str(script), *rest] and spawns it with
# create_subprocess_exec. If the script lacks its execute bit, that spawn raises
# PermissionError BEFORE any kubectl runs, breaking the feature end-to-end. The
# CaptureRunner-based tests above never spawn a subprocess and so cannot catch
# this; these two tests exercise the real exec path that production uses.
# =========================================================================== #
def test_script_file_is_executable():
    """Committed git mode must carry the execute bit (like capacity_check.py /
    install_prereqs.sh) — the runner exec's it directly, no interpreter prefix."""
    mode = SCRIPT_PATH.stat().st_mode
    assert mode & stat.S_IXUSR, (
        f"{SCRIPT_PATH} is not user-executable (mode {oct(mode)}). The runner spawns it "
        "via create_subprocess_exec; without +x it raises PermissionError before kubectl."
    )
    # os.access mirrors what create_subprocess_exec checks at spawn time.
    assert os.access(SCRIPT_PATH, os.X_OK)


async def test_real_runner_execs_script_and_injects_token_only_via_env(tmp_path, monkeypatch):
    """END-TO-END on the REAL exec path: drive CommandRunner.execute() exactly as the tool
    does in production. A fake `kubectl` is placed first on PATH so no cluster is touched,
    and it RECORDS its argv + the HF_TOKEN it sees in its environment. This proves:
      * the script is actually spawned (would raise PermissionError if not executable) —
        i.e. this test FAILS if the execute bit regresses;
      * the token, injected via the runner's extra_env (settings.extra_subprocess_env, the
        production wiring at app/main.py), reaches the child env and the inner kubectl;
      * the token is NEVER on the logical argv the agent expresses, nor in the RunResult.
    """
    # A fake kubectl that records each invocation (argv + the HF_TOKEN it was handed in env),
    # so the two-stage create|apply runs with no real cluster. `create --dry-run` renders a
    # manifest to stdout; `apply -f -` consumes it from stdin and confirms.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    record = tmp_path / "kubectl_calls.log"
    fake_kubectl = bindir / "kubectl"
    fake_kubectl.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        f"rec = open({str(record)!r}, 'a')\n"
        "rec.write('ARGV ' + repr(sys.argv[1:]) + ' HF_TOKEN=' + os.environ.get('HF_TOKEN','') + '\\n')\n"
        "rec.flush()\n"
        "if sys.argv[1:3] == ['create','secret']:\n"
        "    sys.stdout.write('apiVersion: v1\\nkind: Secret\\n')\n"
        "elif sys.argv[1:2] == ['apply']:\n"
        "    _ = sys.stdin.read()\n"
        "    sys.stdout.write('secret/llm-d-hf-token created\\n')\n"
        "sys.exit(0)\n"
    )
    fake_kubectl.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")

    # Production wiring: settings.extra_subprocess_env carries HF_TOKEN, and app/main.py builds
    # CommandRunner(repo_paths, extra_env=settings.extra_subprocess_env). Mirror that exactly.
    s = Settings(hf_token=_FAKE_HF_TOKEN, simulate=False, _env_file=None)
    assert s.extra_subprocess_env == {"HF_TOKEN": _FAKE_HF_TOKEN}
    runner = CommandRunner(s.repo_paths, extra_env=s.extra_subprocess_env)

    allowlist = Allowlist.from_file(ALLOWLIST_PATH)
    logical_argv = ["provision_hf_secret.py", "--namespace", "llmd-quickstart", "--name", "llm-d-hf-token"]
    decision = allowlist.validate(logical_argv)
    assert decision.allowed and decision.mode == MUTATING
    entry = allowlist.executable(logical_argv[0])

    # The REAL spawn. If the script lacks +x this raises (RunnerError wrapping PermissionError).
    result = await runner.execute(logical_argv, entry, timeout=30)

    assert result.exit_code == 0, result.output
    # The script ran kubectl twice (render | apply) — the fake recorded both.
    lines = record.read_text().splitlines()
    assert len(lines) == 2, lines
    create_line, apply_line = lines
    assert create_line.startswith("ARGV ['create', 'secret', 'generic', 'llm-d-hf-token'")
    assert apply_line.startswith("ARGV ['apply'")
    # The token DID reach the child env (so the inner kubectl could materialize the Secret)...
    assert f"HF_TOKEN={_FAKE_HF_TOKEN}" in create_line
    # ...but it is NOT on the logical argv the agent expressed, nor in the captured RunResult.
    assert _FAKE_HF_TOKEN not in " ".join(logical_argv)
    assert _FAKE_HF_TOKEN not in " ".join(result.real_argv)
    assert _FAKE_HF_TOKEN not in result.output
    # Output surfaces only kubectl's confirmation line.
    assert "secret/llm-d-hf-token created" in result.output
