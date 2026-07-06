"""Chat-UI model + reasoning-effort picker (agent-SDK provider only) — hermetic, no CLI/network.

Covers the backend half of the feature:
  * ``SetModelIn`` validates the ``set_model`` frame SHAPE (valid frames parse; extras/bad shape
    are rejected so the /ws handler can answer with a structured error and keep the socket alive);
  * the pure catalog builder (``served_models``/``valid_selection``/``model_views``) — the served
    list always includes the configured default, preserves catalog order, synthesizes an unknown
    default, and the allowlist/effort validation the /ws handler applies before storing a selection;
  * the per-turn override reaching the agent-SDK provider — ``open_provider_turn`` carries the
    override into the turn's effective model/reasoning, ``chat()`` builds ``ClaudeAgentOptions`` with
    the overridden model + effort, and the prewarm fingerprint changes when model/effort differ (so
    a switched turn never adopts a spare built for the old model).
"""
from __future__ import annotations

import claude_agent_sdk as sdk
import pytest
from fastapi.testclient import TestClient

from app.agent.ws_schemas import SetModelIn, ValidationError, parse_inbound
from app.config import Settings
from app.llm.agent_sdk_provider import _EFFORT_LEVELS, AgentSdkProvider
from app.llm.model_catalog import CATALOG, model_views, served_models, valid_selection
from app.llm.provider import open_provider_turn

# ---------------------------------------------------------------------------
# (b) SetModelIn — frame SHAPE validation
# ---------------------------------------------------------------------------


def test_set_model_parses_valid_frames():
    msg = parse_inbound({"type": "set_model", "model": "claude-opus-4-8", "effort": "xhigh"})
    assert isinstance(msg, SetModelIn)
    assert (msg.model, msg.effort) == ("claude-opus-4-8", "xhigh")
    # effort is optional (a no-effort model like Haiku sends none) and defaults to None.
    assert parse_inbound({"type": "set_model", "model": "claude-haiku-4-5"}).effort is None
    # an explicit null effort is accepted too (the UI always includes the key).
    assert parse_inbound({"type": "set_model", "model": "x", "effort": None}).effort is None


def test_set_model_rejects_extras_and_bad_shape():
    # extra field forbidden (extra="forbid"), mirroring the sibling control frames.
    with pytest.raises(ValidationError):
        parse_inbound({"type": "set_model", "model": "x", "session_id": "nope"})
    # model is required and must be non-empty.
    with pytest.raises(ValidationError):
        parse_inbound({"type": "set_model"})
    with pytest.raises(ValidationError):
        parse_inbound({"type": "set_model", "model": ""})
    # a non-dict / unknown tag is not a set_model frame.
    with pytest.raises(ValidationError):
        parse_inbound({"type": "set_model", "model": "x", "effort": 3})


# ---------------------------------------------------------------------------
# (c) catalog builder — served list, order, validation
# ---------------------------------------------------------------------------


def _ids(models):
    return [m.id for m in models]


def test_served_list_curated_plus_default_in_catalog_order():
    # The configured default is folded in at its CANONICAL catalog position (not appended).
    assert _ids(served_models("claude-sonnet-4-6")) == [
        "claude-opus-4-8", "claude-sonnet-5", "claude-sonnet-4-6", "claude-haiku-4-5",
    ]
    # A default already in the curated set doesn't duplicate it.
    assert _ids(served_models("claude-haiku-4-5")) == [
        "claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5",
    ]
    # A prior-gen catalog default (not curated) slots into its catalog position.
    assert _ids(served_models("claude-opus-4-7")) == [
        "claude-opus-4-8", "claude-sonnet-5", "claude-opus-4-7", "claude-haiku-4-5",
    ]


def test_served_list_synthesizes_unknown_default():
    served = served_models("my-private-model")
    assert _ids(served)[-1] == "my-private-model"      # appended after the curated set
    synth = served[-1]
    assert synth.label == "my-private-model" and synth.efforts == ()   # selectable, no effort switch


def test_model_views_wire_shape():
    views = model_views("claude-haiku-4-5")
    for v in views:
        assert set(v) == {"id", "label", "efforts"} and isinstance(v["efforts"], list)
    by_id = {v["id"]: v for v in views}
    assert by_id["claude-haiku-4-5"]["efforts"] == []          # Haiku: no effort control
    assert "xhigh" in by_id["claude-opus-4-8"]["efforts"]      # Opus supports xhigh


def test_catalog_efforts_are_subset_of_master_effort_levels():
    # Per-model efforts are subsets of the provider's master _EFFORT_LEVELS — reuse the single
    # source of truth so the two can't drift (Sonnet 4.6 lacks xhigh; Haiku has none).
    for m in CATALOG:
        assert set(m.efforts) <= _EFFORT_LEVELS, m.id


def test_valid_selection_enforces_allowlist_and_effort():
    d = "claude-haiku-4-5"   # the configured default (only curated models + this are served)
    # an effort-capable model needs a SUPPORTED effort.
    assert valid_selection("claude-opus-4-8", "xhigh", d).id == "claude-opus-4-8"
    assert valid_selection("claude-opus-4-8", "bogus", d) is None
    assert valid_selection("claude-opus-4-8", None, d) is None      # effort required here
    # a no-effort model (Haiku) must carry NO effort.
    assert valid_selection("claude-haiku-4-5", None, d).id == "claude-haiku-4-5"
    assert valid_selection("claude-haiku-4-5", "high", d) is None
    # the allowlist is the SERVED list, not the whole catalog: Sonnet 4.6 is in CATALOG but not
    # served under this default, so it is rejected.
    assert valid_selection("claude-sonnet-4-6", "high", d) is None
    # …but when it IS the configured default it becomes served and selectable.
    assert valid_selection("claude-sonnet-4-6", "high", "claude-sonnet-4-6").id == "claude-sonnet-4-6"
    # an entirely unknown id is rejected.
    assert valid_selection("does-not-exist", "high", d) is None


# ---------------------------------------------------------------------------
# (d) the override reaches the agent-SDK provider
# ---------------------------------------------------------------------------


def _settings() -> Settings:
    return Settings(llm_provider="claude-agent-sdk", agent_sdk_model="claude-haiku-4-5")


def _tools():
    return [{"name": "probe_environment", "description": "d", "input_schema": {"type": "object"}}]


def _capturing_query(box):
    async def _q(*, prompt, options, transport=None):
        box["options"] = options
        yield sdk.AssistantMessage(content=[sdk.TextBlock(text="ok")], model=options.model)
        yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                                num_turns=1, session_id="s",
                                usage={"input_tokens": 1, "output_tokens": 1,
                                       "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})
    return _q


def test_open_provider_turn_carries_override_into_effective_model_and_reasoning():
    p = AgentSdkProvider(_settings())
    turn = open_provider_turn(p, system="sys", tools=_tools(),
                              model="claude-opus-4-8", effort="xhigh")
    # The turn resolved the override ONCE (held for the whole turn); effort replaces the effort
    # portion of the reasoning opts while extended-thinking (a process setting) carries over.
    assert turn._eff_model == "claude-opus-4-8"
    assert turn._eff_reasoning["effort"] == "xhigh"
    assert turn._eff_reasoning["thinking"] == {"type": "adaptive"}
    # No override → the configured defaults, unchanged behavior.
    plain = open_provider_turn(p, system="sys", tools=_tools())
    assert plain._eff_model == "claude-haiku-4-5"
    assert plain._eff_reasoning["effort"] == "high"


async def test_chat_builds_options_with_overridden_model_and_effort(monkeypatch):
    box: dict = {}
    monkeypatch.setattr(sdk, "query", _capturing_query(box))
    p = AgentSdkProvider(_settings())
    await p.chat(system="sys", messages=[{"role": "user", "content": "hi"}], tools=_tools(),
                 model="claude-opus-4-8", effort="xhigh")
    assert box["options"].model == "claude-opus-4-8"
    assert box["options"].effort == "xhigh"


async def test_chat_without_override_uses_configured_defaults(monkeypatch):
    box: dict = {}
    monkeypatch.setattr(sdk, "query", _capturing_query(box))
    s = _settings()
    await AgentSdkProvider(s).chat(
        system="sys", messages=[{"role": "user", "content": "hi"}], tools=_tools())
    assert box["options"].model == s.agent_sdk_model
    assert box["options"].effort == s.agent_sdk_effort   # "high"


def test_prewarm_fingerprint_differs_on_model_and_effort():
    tools = _tools()
    r_high = {"thinking": {"type": "adaptive"}, "effort": "high"}
    r_xhigh = {"thinking": {"type": "adaptive"}, "effort": "xhigh"}
    fp = AgentSdkProvider._fingerprint
    base = fp("sys", tools, "claude-haiku-4-5", r_high)
    assert fp("sys", tools, "claude-haiku-4-5", r_high) == base      # deterministic
    assert fp("sys", tools, "claude-opus-4-8", r_high) != base       # different model
    assert fp("sys", tools, "claude-haiku-4-5", r_xhigh) != base     # different effort


# ---------------------------------------------------------------------------
# the /ws handler: validate against runtime truth, store per-session, reject cleanly
# ---------------------------------------------------------------------------


def _drain_for(ws, want_type, *, limit=40):
    for _ in range(limit):
        ev = ws.receive_json()
        if ev["type"] == want_type:
            return ev
    raise AssertionError(f"never saw a {want_type!r} frame")


def test_ws_set_model_validates_stores_and_rejects(monkeypatch):
    import app.main as main_mod

    with TestClient(main_mod.app) as client:
        # Provider None disables the brand-new-chat pre-probe (no subprocess noise); set_model does
        # not touch the provider. Then make the handler see a switchable agent-SDK settings so the
        # served catalog is non-empty regardless of the ambient .env.
        main_mod.app.state.provider = None
        with client.websocket_connect("/ws") as ws:
            sid = _drain_for(ws, "ready")["data"]["session_id"]
            monkeypatch.setattr(
                main_mod, "get_settings",
                lambda: Settings(llm_provider="claude-agent-sdk", agent_sdk_model="claude-haiku-4-5"))

            # A valid switch stores the selection on the session (ephemeral, applied next turn).
            ws.send_json({"type": "set_model", "model": "claude-opus-4-8", "effort": "xhigh"})
            ws.send_json({"type": "ping"})
            assert _drain_for(ws, "pong")
            sess = main_mod.app.state.sessions.get(sid)
            assert (sess.model_override, sess.effort_override) == ("claude-opus-4-8", "xhigh")
            # Not persisted (ephemeral): the set_model handler deliberately never persists, and even
            # a full persist() (what a real turn/toggle triggers) omits the override — so a reload
            # resets to the configured default. Force the snapshot to exist, then prove it's absent.
            import json
            sess.persist()
            assert "model_override" not in json.loads((sess.ctx.workspace / "state.json").read_text())

            # An invalid effort → structured error, socket alive, PRIOR selection kept.
            ws.send_json({"type": "set_model", "model": "claude-opus-4-8", "effort": "bogus"})
            assert _drain_for(ws, "error")
            ws.send_json({"type": "ping"})
            assert _drain_for(ws, "pong")
            assert (sess.model_override, sess.effort_override) == ("claude-opus-4-8", "xhigh")

            # Switching to a no-effort model (Haiku) stores effort None.
            ws.send_json({"type": "set_model", "model": "claude-haiku-4-5"})
            ws.send_json({"type": "ping"})
            assert _drain_for(ws, "pong")
            assert (sess.model_override, sess.effort_override) == ("claude-haiku-4-5", None)
