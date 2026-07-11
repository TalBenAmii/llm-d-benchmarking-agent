"""Phase 62 — Gated-model access pre-flight (check_model_access / GatedStatus).

Hermetic: NO live HuggingFace call, NO GPU, NO live cluster, NO real standup. The bridge's
``_gated_block`` is driven by an INJECTED fake ``llmdbenchmark.utilities.huggingface``
module that returns a fixture ``ModelAccessResult`` for each ``GatedStatus`` (public /
NOT_GATED, gated+authorized, gated+denied) — exactly the repo's own gating contract, with
zero network. The tool layer is exercised end-to-end through a ``CaptureRunner`` whose
faked bridge stdout carries each ``gated_access`` block, asserting the structured
``{gated, authorized, gated_reason}`` verdict reaches the plan gate. The secret-scrub test
asserts the configured ``HF_TOKEN`` value never appears in the structured result or in any
emitted command event.
"""
from __future__ import annotations

import enum
import importlib.util
import json
import logging
import sys
import types
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.capacity.planner import (
    CapacityVerdict,
    classify_diagnostics,
    merge_gated_access,
)
from app.config import Settings
from app.security.allowlist import Allowlist
from app.tools.context import ToolContext
from app.tools.setup.capacity import check_capacity
from tests.flows.harness import CaptureRunner

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST_PATH = PROJECT_ROOT / "security" / "allowlist.yaml"
BRIDGE_PATH = PROJECT_ROOT / "scripts" / "bridges" / "capacity_check.py"

# A sentinel token value the backend would hold. The whole point of the scrub assertions
# is that THIS string never leaks into the structured result or the command events.
_FAKE_HF_TOKEN = "hf_SECRET_phase62_must_never_leak_0xDEADBEEF"


@pytest.fixture
def _restore_logging_disable():
    """The bridge's ``main`` calls ``logging.disable(CRITICAL)`` (process-global) to keep
    planner chatter off its stdout JSON channel. Any test that invokes ``bridge.main`` must
    restore the global disable level afterwards so it doesn't suppress logging in unrelated
    tests (e.g. tests/platform/test_logging.py)."""
    saved = logging.root.manager.disable
    try:
        yield
    finally:
        logging.disable(saved)


# --------------------------------------------------------------------------- #
# A faithful fake of the repo's gating utility (mechanism we REUSE, never reimplement).
# We inject this into sys.modules so the bridge's lazy import resolves to it — driving
# _gated_block with fixture ModelAccessResults without the real huggingface_hub dep.
# --------------------------------------------------------------------------- #
class _GatedStatus(enum.Enum):
    NOT_GATED = "not_gated"
    GATED = "gated"
    ERROR = "error"


class _AccessStatus(enum.Enum):
    AUTHORIZED = "authorized"
    UNAUTHORIZED = "unauthorized"
    ERROR = "error"


@dataclass
class _ModelAccessResult:
    model_id: str
    gated: _GatedStatus
    access: _AccessStatus | None = None
    detail: str = ""


def _install_fake_hf(monkeypatch, responder, *, token_box=None):
    """Inject a fake ``llmdbenchmark.utilities.huggingface`` whose ``check_model_access``
    returns ``responder(model_id, hf_token)``. ``token_box`` (a list) records each token
    the bridge passes through — so a test can prove the token reaches the util but never
    the result."""
    hf = types.ModuleType("llmdbenchmark.utilities.huggingface")
    hf.GatedStatus = _GatedStatus
    hf.AccessStatus = _AccessStatus
    hf.ModelAccessResult = _ModelAccessResult

    def check_model_access(model_id, hf_token=None):
        if token_box is not None:
            token_box.append(hf_token)
        return responder(model_id, hf_token)

    hf.check_model_access = check_model_access

    # Build the parent package chain so ``from llmdbenchmark.utilities.huggingface import ...``
    # resolves cleanly under monkeypatch (auto-undone at test end).
    root = types.ModuleType("llmdbenchmark")
    root.__path__ = []  # mark as a package
    utilities = types.ModuleType("llmdbenchmark.utilities")
    utilities.__path__ = []
    monkeypatch.setitem(sys.modules, "llmdbenchmark", root)
    monkeypatch.setitem(sys.modules, "llmdbenchmark.utilities", utilities)
    monkeypatch.setitem(sys.modules, "llmdbenchmark.utilities.huggingface", hf)


def _load_bridge():
    """Import scripts/bridges/capacity_check.py as a module (it is not on the package path)."""
    spec = importlib.util.spec_from_file_location("capacity_check_bridge", BRIDGE_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Canned responders for the three GatedStatus situations the spec enumerates.
def _public(model_id, _token):
    return _ModelAccessResult(
        model_id=model_id,
        gated=_GatedStatus.NOT_GATED,
        detail=f'Model "{model_id}" is not gated -- access is authorized by default',
    )


def _gated_authorized(model_id, _token):
    return _ModelAccessResult(
        model_id=model_id,
        gated=_GatedStatus.GATED,
        access=_AccessStatus.AUTHORIZED,
        detail=f'Verified access to gated model "{model_id}" is authorized',
    )


def _gated_denied(model_id, _token):
    return _ModelAccessResult(
        model_id=model_id,
        gated=_GatedStatus.GATED,
        access=_AccessStatus.UNAUTHORIZED,
        detail=(
            f'Unauthorized access to gated model "{model_id}". Your HuggingFace token does '
            f"not have access to this model. Visit https://huggingface.co/{model_id} to "
            f"request access."
        ),
    )


# =========================================================================== #
# 1. The bridge's _gated_block over fixture ModelAccessResults (one per GatedStatus)
# =========================================================================== #
def _block_for(monkeypatch, responder, model, *, token_env=None, token_box=None):
    _install_fake_hf(monkeypatch, responder, token_box=token_box)
    if token_env is None:
        monkeypatch.delenv("HF_TOKEN", raising=False)
    else:
        monkeypatch.setenv("HF_TOKEN", token_env)
    bridge = _load_bridge()
    return bridge._gated_block({"model": {"name": model}})


def test_public_model_block_needs_no_token(monkeypatch):
    block = _block_for(monkeypatch, _public, "facebook/opt-125m")
    assert block["gated"] is False
    # PUBLIC: authorized is None (no token is needed to decide), reason explains "not gated".
    assert block["authorized"] is None
    assert "not gated" in block["reason"]
    assert block["models"][0]["gated"] == "not_gated"


def test_gated_authorized_block_says_authorized(monkeypatch):
    block = _block_for(
        monkeypatch, _gated_authorized, "meta-llama/Llama-3.1-8B", token_env=_FAKE_HF_TOKEN
    )
    assert block["gated"] is True
    assert block["authorized"] is True
    assert "authorized" in block["reason"]
    assert block["models"][0]["gated"] == "gated"


def test_gated_denied_block_says_unauthorized(monkeypatch):
    block = _block_for(
        monkeypatch, _gated_denied, "meta-llama/Llama-3.1-8B", token_env=_FAKE_HF_TOKEN
    )
    assert block["gated"] is True
    assert block["authorized"] is False
    # The reason is the upstream remediation text — the "request access" fix the agent quotes.
    assert "does not have access" in block["reason"]


def test_no_model_id_yields_none_block(monkeypatch):
    _install_fake_hf(monkeypatch, _public)
    bridge = _load_bridge()
    assert bridge._gated_block({"model": {}}) is None
    assert bridge._gated_block({}) is None


def test_huggingface_id_preferred_over_name(monkeypatch):
    seen = []

    def responder(model_id, _token):
        seen.append(model_id)
        return _public(model_id, _token)

    _install_fake_hf(monkeypatch, responder)
    bridge = _load_bridge()
    bridge._gated_block({"model": {"huggingfaceId": "org/gated", "name": "ignored/name"}})
    assert seen == ["org/gated"]  # mirrors capacity_validator._extract_params precedence


def test_multi_model_blocked_when_any_unauthorized(monkeypatch):
    def responder(model_id, token):
        return _gated_authorized(model_id, token) if "ok" in model_id else _gated_denied(
            model_id, token
        )

    _install_fake_hf(monkeypatch, responder)
    monkeypatch.setenv("HF_TOKEN", _FAKE_HF_TOKEN)
    bridge = _load_bridge()
    block = bridge._gated_block({"model": {"name": "org/ok-model, org/denied-model"}})
    assert block["gated"] is True
    # Aggregate authorized is False because ONE gated model cannot be pulled.
    assert block["authorized"] is False
    assert len(block["models"]) == 2
    # The aggregate reason is the BLOCKING detail (not the authorized one).
    assert "does not have access" in block["reason"]


def test_gated_check_import_failure_degrades_without_token(monkeypatch):
    # No fake module installed -> the lazy import inside _gated_block raises ImportError.
    for name in ("llmdbenchmark", "llmdbenchmark.utilities", "llmdbenchmark.utilities.huggingface"):
        monkeypatch.setitem(sys.modules, name, None)  # force ImportError on import
    monkeypatch.setenv("HF_TOKEN", _FAKE_HF_TOKEN)
    bridge = _load_bridge()
    block = bridge._gated_block({"model": {"name": "org/whatever"}})
    assert block["gated"] is None and block["authorized"] is None
    assert "gated check unavailable" in block["reason"]
    # Degraded path must be token-free and traceback-free.
    assert _FAKE_HF_TOKEN not in json.dumps(block)


def test_bridge_block_never_contains_the_token(monkeypatch):
    """The token reaches the gating util but NEVER the returned block (secret scrub)."""
    token_box: list = []
    block = _block_for(
        monkeypatch,
        _gated_denied,
        "meta-llama/Llama-3.1-8B",
        token_env=_FAKE_HF_TOKEN,
        token_box=token_box,
    )
    # The util DID receive the real token (mechanism reuse), ...
    assert token_box == [_FAKE_HF_TOKEN]
    # ... but it is nowhere in the structured block.
    assert _FAKE_HF_TOKEN not in json.dumps(block)


def test_bridge_placeholder_token_treated_as_none(monkeypatch):
    token_box: list = []
    _block_for(
        monkeypatch,
        _gated_denied,
        "org/gated",
        token_env="REPLACE_TOKEN",
        token_box=token_box,
    )
    assert token_box == [None]  # repo's placeholder is normalized to "no token"


# =========================================================================== #
# 2. planner.merge_gated_access — pure field copy (facts only, no policy)
# =========================================================================== #
def test_merge_gated_access_copies_facts():
    v = classify_diagnostics([])
    out = merge_gated_access(
        v, {"gated": True, "authorized": False, "reason": "your token can't pull this"}
    )
    assert out is v  # mutates + returns the same verdict
    assert v.gated is True and v.authorized is False
    assert v.gated_reason == "your token can't pull this"
    assert v.as_dict()["gated"] is True
    assert v.as_dict()["authorized"] is False
    assert v.as_dict()["gated_reason"] == "your token can't pull this"


def test_merge_gated_access_none_block_leaves_defaults():
    v = CapacityVerdict(feasible=True, will_fail=False)
    merge_gated_access(v, None)
    assert v.gated is None and v.authorized is None and v.gated_reason == ""
    # Defaulted fields are still present in as_dict (legacy/non-gated paths unchanged shape).
    d = v.as_dict()
    assert d["gated"] is None and d["authorized"] is None and d["gated_reason"] == ""


def test_capacity_verdict_default_gated_fields_are_neutral():
    v = classify_diagnostics(["[decode] facebook/opt-125m requires 0.25 GB of memory"])
    assert v.gated is None and v.authorized is None and v.gated_reason == ""


# =========================================================================== #
# 3. The tool end-to-end: each gated_access block reaches the plan gate verdict
# =========================================================================== #
def _gated_settings():
    """Settings carrying an HF token, so extra_subprocess_env would expose HF_TOKEN to the
    child — the exact path the scrub test guards."""
    s = Settings(hf_token=_FAKE_HF_TOKEN, simulate=False)
    return s


def _real_repo_ctx(tmp_path, *, canned):
    s = _gated_settings()
    runner = CaptureRunner(s.repo_paths, canned=canned)
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


def _bridge_json(gated_access):
    return json.dumps({
        "ok": True,
        "diagnostics": ["[decode] facebook/opt-125m requires 0.25 GB of memory"],
        "gated_access": gated_access,
    })


async def test_tool_surfaces_public_verdict(tmp_path):
    canned = {"capacity_check.py": _bridge_json(
        {"gated": False, "authorized": None, "reason": "not gated", "models": []}
    )}
    ctx, _, _ = _real_repo_ctx(tmp_path, canned=canned)
    res = await check_capacity(ctx, spec="cicd/kind")
    assert res["ran"] is True
    assert res["gated"] is False and res["authorized"] is None
    assert "capacity.md" in res["gated_note"]


async def test_tool_surfaces_gated_authorized_verdict(tmp_path):
    canned = {"capacity_check.py": _bridge_json(
        {"gated": True, "authorized": True, "reason": "authorized", "models": []}
    )}
    ctx, _, _ = _real_repo_ctx(tmp_path, canned=canned)
    res = await check_capacity(ctx, spec="cicd/kind", overrides={"model": "meta-llama/Llama-3.1-8B"})
    assert res["gated"] is True and res["authorized"] is True
    assert res["gated_reason"] == "authorized"


async def test_tool_surfaces_gated_unauthorized_verdict_before_any_mutation(tmp_path):
    reason = "Unauthorized access to gated model. Your HuggingFace token does not have access."
    canned = {"capacity_check.py": _bridge_json(
        {"gated": True, "authorized": False, "reason": reason, "models": []}
    )}
    ctx, runner, emitted = _real_repo_ctx(tmp_path, canned=canned)
    res = await check_capacity(ctx, spec="cicd/kind", overrides={"model": "meta-llama/Llama-3.1-8B"})
    assert res["gated"] is True and res["authorized"] is False
    assert "does not have access" in res["gated_reason"]
    # The ONLY command run was the read-only capacity pre-flight — no mutating step.
    assert all(c["argv"][0] == "capacity_check.py" for c in runner.calls)
    cmd_events = [p for t, p in emitted if t == "command"]
    assert cmd_events and all(e["mode"] != "mutating" for e in cmd_events)


async def test_tool_legacy_bridge_without_gated_access_keeps_neutral(tmp_path):
    # A bridge that predates Phase 62 omits gated_access entirely -> fields stay neutral.
    legacy = json.dumps({"ok": True, "diagnostics": ["[decode] ok"]})
    ctx, _, _ = _real_repo_ctx(tmp_path, canned={"capacity_check.py": legacy})
    res = await check_capacity(ctx, spec="cicd/kind")
    assert res["ran"] is True
    assert res["gated"] is None and res["authorized"] is None and res["gated_reason"] == ""


# =========================================================================== #
# 4. SECRET SCRUB — the HF token never appears in the result or the command events
# =========================================================================== #
async def test_token_absent_from_result_and_events(tmp_path):
    # The bridge (correctly) never echoes the token; even if a reason somehow carried it,
    # the agent-visible surface must be clean. We assert the whole result + all events.
    reason = "Unauthorized access to gated model -- your token cannot pull it."
    canned = {"capacity_check.py": _bridge_json(
        {"gated": True, "authorized": False, "reason": reason, "models": [
            {"model": "meta-llama/Llama-3.1-8B", "gated": "gated", "authorized": False,
             "reason": reason},
        ]}
    )}
    ctx, runner, emitted = _real_repo_ctx(tmp_path, canned=canned)
    res = await check_capacity(ctx, spec="cicd/kind", overrides={"model": "meta-llama/Llama-3.1-8B"})

    # The token is configured (would be passed to the child via extra_subprocess_env)...
    assert ctx.settings.extra_subprocess_env["HF_TOKEN"] == _FAKE_HF_TOKEN
    # ...but it appears NOWHERE in the structured result the agent sees.
    assert _FAKE_HF_TOKEN not in json.dumps(res)
    # ...nor in ANY emitted event (command announcements, outputs, etc.).
    assert _FAKE_HF_TOKEN not in json.dumps(emitted, default=str)
    # ...nor in the request file written to the workspace (only plan_config goes there).
    req = next(c for c in runner.calls if c["argv"][0] == "capacity_check.py")
    assert _FAKE_HF_TOKEN not in Path(req["argv"][1]).read_text()


def test_bridge_emit_contract_includes_gated_access(
    monkeypatch, tmp_path, capsys, _restore_logging_disable
):
    """The bridge's success stdout JSON carries the gated_access field end-to-end (the
    contract app/tools/setup/capacity.py reads), driven through the fake gating util."""
    _install_fake_hf(monkeypatch, _gated_denied)
    monkeypatch.setenv("HF_TOKEN", _FAKE_HF_TOKEN)
    # A self-contained plan_config + a fake run_capacity_planner so no real planner is needed.
    cap = types.ModuleType("llmdbenchmark.utilities.capacity_validator")

    def run_capacity_planner(plan_config, logger=None, ignore_failures=True):
        return ["[decode] sized ok"]

    cap.run_capacity_planner = run_capacity_planner
    monkeypatch.setitem(sys.modules, "llmdbenchmark.utilities.capacity_validator", cap)

    req = tmp_path / "req.json"
    req.write_text(json.dumps({
        "plan_config": {"model": {"name": "meta-llama/Llama-3.1-8B"}},
        "ignore_failures": True,
    }))
    bridge = _load_bridge()
    rc = bridge.main(["capacity_check.py", str(req)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["gated_access"]["gated"] is True
    assert out["gated_access"]["authorized"] is False
    # The bridge stdout (the agent's channel) carries no token.
    assert _FAKE_HF_TOKEN not in json.dumps(out)


@pytest.mark.parametrize("responder,exp_gated,exp_auth", [
    (_public, False, None),
    (_gated_authorized, True, True),
    (_gated_denied, True, False),
])
def test_each_gatedstatus_maps_to_structured_verdict(monkeypatch, responder, exp_gated, exp_auth):
    """One assertion per GatedStatus that the structured {gated, authorized, reason} verdict
    is produced from a fixture ModelAccessResult — the spec's core acceptance."""
    block = _block_for(monkeypatch, responder, "some/model", token_env=_FAKE_HF_TOKEN)
    assert block["gated"] is exp_gated
    assert block["authorized"] is exp_auth
    assert isinstance(block["reason"], str) and block["reason"]
