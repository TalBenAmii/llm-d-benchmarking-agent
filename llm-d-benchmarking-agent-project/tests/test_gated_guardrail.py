"""Deterministic gated-model access guardrail (app/tools/gated_access.py).

Steering alone (system-prompt HARD_RULE + knowledge/capacity.md + check_capacity's gated_note)
could not RELIABLY stop a flaky model from deploying a gated model whose weights the backend HF
token can't pull. This guardrail is the non-bypassable backstop: once check_capacity reports a
model gated+unauthorized, a standup/run/smoketest of it is REFUSED at the command chokepoint
(execute_llmdbenchmark via ctx.run_command) AND on the ad-hoc run_shell surface, until a later
check_capacity reports it authorized. provision_hf_secret is never blocked.

These tests exercise: the pure block logic, the verdict-recording in check_capacity, and the two
enforcement surfaces (run_command + run_shell), including that the block clears on re-auth.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings, get_settings
from app.security.allowlist import Allowlist
from app.tools.capacity import check_capacity
from app.tools.context import ToolContext, ToolError
from app.tools.gated_access import (
    gated_block,
    gated_block_message,
    record_capacity_verdict,
)
from app.tools.shell import run_shell
from tests._helpers import _approve_all
from tests.flows.catalog_snapshot import frozen_catalog
from tests.flows.flows import _CAPACITY_GATED_NO_TOKEN
from tests.flows.harness import CaptureRunner

MODEL = "meta-llama/Llama-3.1-8B"
_GATED_UNAUTH = {"gated": True, "authorized": False, "gated_reason": "no token configured"}
_GATED_AUTH = {"gated": True, "authorized": True, "gated_reason": ""}
_PUBLIC = {"gated": False, "authorized": None, "gated_reason": ""}
ALLOWLIST_PATH = Path(__file__).resolve().parents[1] / "security" / "allowlist.yaml"


def _stub_ctx(**verdicts):
    """A minimal stand-in: gated_block only reads ctx.gated_access."""
    return SimpleNamespace(gated_access=dict(verdicts))


def _exec_ctx(tmp_path):
    """A ToolContext wired to the real allowlist + the frozen catalog (so a cicd/kind standup
    validates) with a CaptureRunner — no real cluster. Auto-approves mutating commands so the
    guardrail (which fires BEFORE approval) is what blocks, not a declined prompt."""
    settings = Settings(_env_file=None, repos_dir=tmp_path / "repos", workspace_dir=tmp_path / "ws")
    runner = CaptureRunner(settings.repo_paths)
    ctx = ToolContext(
        settings=settings,
        allowlist=Allowlist.from_file(settings.allowlist_path),
        runner=runner,
        workspace=tmp_path / "ws",
        request_approval=_approve_all,
    )
    frozen = frozen_catalog()
    ctx._catalog = frozen
    ctx.catalog = lambda *, refresh=False: frozen
    return ctx, runner


def _standup_argv(*, model: str | None = None):
    argv = ["llmdbenchmark", "--spec", "cicd/kind", "standup", "-p", "q", "--skip-smoketest"]
    if model:
        argv += ["-m", model]
    return argv


# --- pure block logic ---------------------------------------------------------------------

def test_gated_unauthorized_blocks_standup_without_explicit_model():
    ctx = _stub_ctx(**{MODEL: _GATED_UNAUTH})
    block = gated_block(ctx, _standup_argv())
    assert block is not None
    assert block[0] == MODEL


def test_gated_unauthorized_blocks_explicit_model_standup_and_run():
    ctx = _stub_ctx(**{MODEL: _GATED_UNAUTH})
    assert gated_block(ctx, _standup_argv(model=MODEL)) is not None
    assert gated_block(ctx, ["llmdbenchmark", "--spec", "cicd/kind", "run", "-p", "q"]) is not None


def test_authorized_and_public_do_not_block():
    assert gated_block(_stub_ctx(**{MODEL: _GATED_AUTH}), _standup_argv()) is None
    assert gated_block(_stub_ctx(**{MODEL: _PUBLIC}), _standup_argv()) is None


def test_no_prior_capacity_check_does_not_block():
    assert gated_block(_stub_ctx(), _standup_argv()) is None


def test_explicitly_deploying_a_different_safe_model_is_allowed():
    # A different model that check_capacity POSITIVELY cleared (recorded public) is allowed even
    # while another model is blocked — the cleared verdict is what unlocks it.
    ctx = _stub_ctx(**{MODEL: _GATED_UNAUTH, "facebook/opt-125m": _PUBLIC})
    assert gated_block(ctx, _standup_argv(model="facebook/opt-125m")) is None


def test_unrecorded_explicit_model_is_refused_while_a_block_is_outstanding():
    # The real-eval gap: check_capacity recorded a DIFFERENT model (e.g. the cicd/kind spec
    # default facebook/opt-125m) as gated+unauthorized, but the standup explicitly names a model
    # that was NEVER check_capacity'd. The earlier (buggy) logic allowed it because the -m model
    # wasn't itself in the blocked set; now it is refused — that unconfirmed model may BE the gated
    # one under a different key. The refusal names the model the agent must re-check.
    ctx = _stub_ctx(**{"facebook/opt-125m": _GATED_UNAUTH})
    block = gated_block(ctx, _standup_argv(model=MODEL))
    assert block is not None
    assert block[0] == MODEL


def test_recorded_authorized_model_is_allowed_while_another_is_blocked():
    # Symmetric to the public case: a model recorded gated+AUTHORIZED is cleared, so deploying it
    # is allowed even though a sibling model is still gated+unauthorized.
    ctx = _stub_ctx(**{"facebook/opt-125m": _GATED_UNAUTH, MODEL: _GATED_AUTH})
    assert gated_block(ctx, _standup_argv(model=MODEL)) is None


def test_non_deploy_subcommands_are_never_blocked():
    ctx = _stub_ctx(**{MODEL: _GATED_UNAUTH})
    for sub in ("plan", "teardown", "results", "experiment"):
        assert gated_block(ctx, ["llmdbenchmark", "--spec", "cicd/kind", sub]) is None


def test_non_llmdbenchmark_commands_are_never_blocked():
    ctx = _stub_ctx(**{MODEL: _GATED_UNAUTH})
    assert gated_block(ctx, ["provision_hf_secret.py", "--name", "llm-d-hf-token"]) is None
    assert gated_block(ctx, ["git", "clone", "https://github.com/llm-d/llm-d"]) is None
    assert gated_block(ctx, ["install.sh", "--uv"]) is None


def test_run_shell_style_bash_lc_standup_is_detected():
    ctx = _stub_ctx(**{MODEL: _GATED_UNAUTH})
    argv = ["bash", "-lc", "llmdbenchmark --spec cicd/kind standup -p q"]
    assert gated_block(ctx, argv) is not None


def test_path_qualified_binary_is_detected_and_blocked():
    # The CLI is matched by BASENAME, so spelling the binary with a path (a real possibility on the
    # ad-hoc run_shell surface) cannot bypass the guardrail.
    ctx = _stub_ctx(**{MODEL: _GATED_UNAUTH})
    for binary in ("/usr/local/bin/llmdbenchmark", "./llmdbenchmark"):
        argv = ["bash", "-lc", f"{binary} --spec cicd/kind standup -p q"]
        assert gated_block(ctx, argv) is not None, binary


def test_equals_form_model_flag_blocks_the_named_gated_model():
    # --models=<gated> must be parsed (not only the space-form --models <gated>) so the named gated
    # model is recognized and refused.
    ctx = _stub_ctx(**{MODEL: _GATED_UNAUTH})
    argv = ["llmdbenchmark", "--spec", "cicd/kind", "standup", f"--models={MODEL}"]
    block = gated_block(ctx, argv)
    assert block is not None
    assert block[0] == MODEL


def test_equals_form_cleared_model_is_allowed_while_another_is_blocked():
    # The MEDIUM-severity false-positive this fix closes: a model check_capacity POSITIVELY cleared,
    # deployed via the equals-form flag, must NOT be wrongly refused just because the parser only
    # understood the space-form. The equals-form value is extracted, matched to the cleared verdict,
    # and allowed even while a sibling model is blocked.
    ctx = _stub_ctx(**{MODEL: _GATED_UNAUTH, "facebook/opt-125m": _PUBLIC})
    argv = ["llmdbenchmark", "--spec", "cicd/kind", "standup", "--models=facebook/opt-125m"]
    assert gated_block(ctx, argv) is None


def test_block_message_nudges_provision_and_recheck():
    msg = gated_block_message(MODEL, "no token configured cluster-side")
    assert MODEL in msg
    assert "provision_hf_secret" in msg
    assert "check_capacity" in msg


def test_record_overwrite_clears_block_on_reauthorization():
    ctx = _stub_ctx(**{MODEL: _GATED_UNAUTH})
    assert gated_block(ctx, _standup_argv()) is not None
    record_capacity_verdict(ctx, model=MODEL, gated=True, authorized=True, gated_reason="")
    assert gated_block(ctx, _standup_argv()) is None


def test_record_is_noop_for_falsy_model():
    ctx = _stub_ctx()
    record_capacity_verdict(ctx, model=None, gated=True, authorized=False)
    assert ctx.gated_access == {}


# --- enforcement at the command chokepoint (execute_llmdbenchmark path) -------------------

async def test_run_command_blocks_gated_standup_before_execution(tmp_path):
    ctx, runner = _exec_ctx(tmp_path)
    ctx.gated_access[MODEL] = dict(_GATED_UNAUTH)
    with pytest.raises(ToolError) as ei:
        await ctx.run_command(_standup_argv())
    assert "provision_hf_secret" in str(ei.value)
    assert runner.calls == []  # never executed


async def test_run_command_allows_standup_after_reauthorization(tmp_path):
    ctx, runner = _exec_ctx(tmp_path)
    ctx.gated_access[MODEL] = dict(_GATED_UNAUTH)
    with pytest.raises(ToolError):
        await ctx.run_command(_standup_argv())
    ctx.gated_access[MODEL] = dict(_GATED_AUTH)
    await ctx.run_command(_standup_argv())
    assert len(runner.calls) == 1


# --- enforcement on the ad-hoc run_shell surface ------------------------------------------

async def test_run_shell_blocks_gated_standup(tmp_path):
    ctx, runner = _exec_ctx(tmp_path)
    ctx.gated_access[MODEL] = dict(_GATED_UNAUTH)
    with pytest.raises(ToolError) as ei:
        await run_shell(ctx, command="llmdbenchmark --spec cicd/kind standup -p q")
    assert "provision_hf_secret" in str(ei.value)
    assert runner.calls == []


async def test_run_shell_allows_non_deploy_command(tmp_path):
    ctx, runner = _exec_ctx(tmp_path)
    ctx.gated_access[MODEL] = dict(_GATED_UNAUTH)
    await run_shell(ctx, command="echo hello")
    assert len(runner.calls) == 1


# --- check_capacity records the verdict so the guardrail can act --------------------------

async def test_check_capacity_records_gated_verdict():
    s = get_settings()
    td = Path(tempfile.mkdtemp())
    runner = CaptureRunner(s.repo_paths, canned={"capacity_check.py": _CAPACITY_GATED_NO_TOKEN})
    ctx = ToolContext(
        settings=s,
        allowlist=Allowlist.from_file(ALLOWLIST_PATH),
        runner=runner,
        workspace=td / "ws",
    )
    res = await check_capacity(ctx, spec="cicd/kind", overrides={"model": MODEL})
    assert res["gated"] is True and res["authorized"] is False
    assert MODEL in ctx.gated_access
    assert ctx.gated_access[MODEL]["gated"] is True
    assert ctx.gated_access[MODEL]["authorized"] is False
