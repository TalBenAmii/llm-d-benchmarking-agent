"""Prompt-cache byte-stability + one-shot catalog injection + the env-preamble clamp.

The cached system prefix must be byte-identical across builds (the CLI prompt-caches it), the
live catalog snapshot is injected exactly once per session as a synthetic conversation message
(never into the prefix), and the ONE surviving clamp (``engine.clamp_tool_result_content``,
bounding the env pre-probe preamble) always yields valid JSON. All hermetic.
"""
from __future__ import annotations

import json

from app.agent.engine import SdkNativeEngine, clamp_tool_result_content
from app.agent.prompt import build_system_prompt, catalog_brief_message
from app.agent.session import Session, SessionManager, derive_title
from app.config import Settings, get_settings
from app.security.policy import CommandPolicy
from app.security.runner import CommandRunner
from tests._helpers import COMMAND_POLICY_PATH, _capture_ctx
from tests._sdk_fake import FakeTransport, assistant, result, text


# ---- cache-prefix stability: the live catalog is OUT of the cached system prefix -----------

def test_system_prompt_has_no_live_catalog_body(tool_ctx):
    """The dynamic catalog must NOT be inlined into the system prefix (it would break the
    cache prefix). Only the byte-stable pointer is present."""
    prompt = build_system_prompt(tool_ctx)
    # The pointer is present; the actual rendered specs/harnesses listing is not.
    assert "list_catalog" in prompt
    assert "[live catalog snapshot" in prompt or "Live catalog" in prompt
    # The catalog-brief body (the "harnesses: ..." line _catalog_brief renders) is NOT inlined.
    brief = catalog_brief_message(tool_ctx)
    body_lines = [ln for ln in brief.splitlines()
                  if ln.startswith("specs:") or ln.startswith("harnesses:")]
    for ln in body_lines:
        assert ln not in prompt, f"catalog body line leaked into the cached system prefix: {ln!r}"


def test_system_prompt_is_byte_stable_across_calls(tool_ctx):
    """The system prefix is identical on repeated builds (no per-turn dynamic content), so the
    CLI reliably cache-hits it."""
    assert build_system_prompt(tool_ctx) == build_system_prompt(tool_ctx)


# ---- the live catalog is injected ONCE as a synthetic conversation message -----------------

async def test_engine_injects_catalog_message_once(tmp_path):
    ctx, _runner = _capture_ctx(tmp_path)
    session = Session(id="cat-once", ctx=ctx)

    async def emit(t, p):
        pass

    async def approve(kind, payload):
        return True

    def run(reply):
        script = [[assistant(text(reply)), result()]]
        return SdkNativeEngine(transport_factory=lambda: FakeTransport(script))

    await run("ok").run_turn(session, "hello", emit=emit, request_approval=approve)
    catalog_msgs = [m for m in session.messages
                    if m.get("role") == "user"
                    and "[live catalog snapshot" in str(m.get("content", ""))]
    assert len(catalog_msgs) == 1, "catalog must be injected exactly once"
    assert session.catalog_injected is True

    # Second turn: no second catalog message.
    await run("again").run_turn(session, "more", emit=emit, request_approval=approve)
    catalog_msgs = [m for m in session.messages
                    if m.get("role") == "user"
                    and "[live catalog snapshot" in str(m.get("content", ""))]
    assert len(catalog_msgs) == 1, "catalog must NOT be re-injected on later turns"


def test_catalog_injected_flag_survives_persist_and_load(tmp_path):
    al = CommandPolicy.from_file(COMMAND_POLICY_PATH)
    runner = CommandRunner(get_settings().repo_paths)
    mgr = SessionManager(Settings(workspace_dir=tmp_path), al, runner)
    sess = mgr.create()
    sess.messages.append({"role": "user", "content": "hi"})
    sess.catalog_injected = True
    sess.persist()
    mgr._sessions.clear()
    loaded = mgr.load(sess.id)
    assert loaded is not None
    assert loaded.catalog_injected is True


def test_old_state_json_without_catalog_flag_defaults_false(tmp_path):
    al = CommandPolicy.from_file(COMMAND_POLICY_PATH)
    runner = CommandRunner(get_settings().repo_paths)
    mgr = SessionManager(Settings(workspace_dir=tmp_path), al, runner)
    sess = mgr.create()
    sess.messages.append({"role": "user", "content": "hi"})
    sess.persist()
    state = mgr._root / sess.id / "state.json"
    data = json.loads(state.read_text())
    data.pop("catalog_injected", None)
    state.write_text(json.dumps(data))
    mgr._sessions.clear()
    loaded = mgr.load(sess.id)
    assert loaded is not None
    assert loaded.catalog_injected is False


def test_title_skips_synthetic_injected_messages():
    """The chat title comes from the first REAL user message, not the injected catalog/pre-probe
    snapshot messages (which both start with a bracket tag)."""
    msgs = [
        {"role": "user", "content": "[environment pre-probe — snapshot]\n{...}"},
        {"role": "user", "content": "[live catalog snapshot — names]\nspecs: cicd/kind"},
        {"role": "user", "content": "benchmark a tiny chat model"},
    ]
    assert derive_title(msgs) == "benchmark a tiny chat model"


# ---- the env-preamble clamp (the ONE surviving clamp) --------------------------------------

def test_clamp_small_result_passes_through_unchanged():
    # Below budget: byte-identical to the plain serialization (no envelope, no overhead).
    result_ = {"ok": True, "value": 42, "note": "hello"}
    out = clamp_tool_result_content(result_, budget=6_000)
    assert out == json.dumps(result_)
    assert json.loads(out) == result_


def test_clamp_large_result_stays_valid_json_within_budget():
    result_ = {"runs": [{"name": f"run-{i}", "blob": "x" * 200} for i in range(200)]}
    budget = 2_000
    out = clamp_tool_result_content(result_, budget=budget)
    assert len(out) <= budget
    parsed = json.loads(out)  # must not raise — a naive slice hands the model malformed JSON
    assert parsed["_truncated"] is True
    assert parsed["_original_chars"] == len(json.dumps(result_))
    assert "preview" in parsed and "_note" in parsed


def test_clamp_signal_fields_preserved_verbatim():
    # A big payload with small status markers: they must survive intact in the envelope.
    result_ = {"error": "kubectl failed: connection refused", "rejected": True,
               "log": "noise " * 5_000}
    out = clamp_tool_result_content(result_, budget=1_000)
    parsed = json.loads(out)
    assert parsed["error"] == "kubectl failed: connection refused"
    assert parsed["rejected"] is True
    assert parsed["_truncated"] is True
    assert len(out) <= 1_000


def test_clamp_escaping_heavy_payload_respects_budget():
    # Quotes and newlines double under JSON escaping; the clamp must account for that expansion.
    result_ = {"text": '"' * 4_000 + "\n" * 4_000}
    budget = 1_200
    out = clamp_tool_result_content(result_, budget=budget)
    assert len(out) <= budget
    json.loads(out)


def test_clamp_tiny_budget_falls_back_to_minimal_valid_envelope():
    result_ = {"a": "x" * 1_000, "b": "y" * 1_000}
    out = clamp_tool_result_content(result_, budget=120)
    parsed = json.loads(out)  # still valid JSON even when the budget is very tight
    assert parsed["_truncated"] is True
    assert parsed["_original_chars"] == len(json.dumps(result_))
