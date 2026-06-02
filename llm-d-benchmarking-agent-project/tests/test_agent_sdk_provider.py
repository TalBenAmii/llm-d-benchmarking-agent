"""Claude Agent SDK provider — hermetic unit tests (no CLI subprocess, no network).

The provider runs inference on the user's Claude subscription via the ``claude`` CLI; here we
exercise the pure conversion logic and the ``chat()`` orchestration by monkeypatching
``claude_agent_sdk.query`` to yield real SDK message objects. The live path (auth, model,
tool-capture) is validated out-of-band against a real Max-plan login.
"""
from __future__ import annotations

import claude_agent_sdk as sdk
import pytest

from app.config import Settings
from app.llm.agent_sdk_provider import (
    AgentSdkProvider,
    _render_assistant_text,
    _render_tool_results,
    _strip_prefix,
    _to_sdk_messages,
    _usage_from,
)
from app.llm.provider import ProviderError, get_provider

# ---- pure helpers --------------------------------------------------------------------------

def test_strip_prefix_roundtrips_mcp_name():
    assert _strip_prefix("mcp__benchtools__probe_environment") == "probe_environment"
    assert _strip_prefix("probe_environment") == "probe_environment"  # already bare


def test_usage_from_maps_and_normalizes():
    u = _usage_from({
        "input_tokens": 120, "output_tokens": 45,
        "cache_read_input_tokens": 4000, "cache_creation_input_tokens": 300,
    })
    assert (u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens) == (120, 45, 4000, 300)
    # total_input is non-cached + both cache portions; never double-counts.
    assert u.total_input == 120 + 4000 + 300
    # missing / None usage is tolerated.
    assert _usage_from(None).total_input == 0
    assert _usage_from({"input_tokens": None}).input_tokens == 0


def test_render_assistant_and_results_are_plain_text():
    txt = _render_assistant_text("on it", [{"id": "x", "name": "probe_environment", "input": {"namespace": "llm-d"}}])
    assert "on it" in txt
    assert '[called tool probe_environment with {"namespace": "llm-d"}]' in txt
    # an empty assistant turn still yields a non-empty string (empty content can be rejected)
    assert _render_assistant_text("", []) == "(no output)"

    res = _render_tool_results([{"tool_call_id": "x", "name": "probe_environment", "content": '{"kind": true}'}])
    assert res.startswith("[tool results]")
    assert 'probe_environment → {"kind": true}' in res


def test_to_sdk_messages_renders_history_as_user_assistant_text_turns():
    neutral = [
        {"role": "user", "content": "check my cluster"},
        {"role": "assistant", "content": "checking", "tool_calls": [
            {"id": "t1", "name": "probe_environment", "input": {"namespace": "llm-d"}}]},
        {"role": "tool_results", "results": [
            {"tool_call_id": "t1", "name": "probe_environment", "content": '{"gpus": 0}'}]},
        {"role": "user", "content": "is it ready?"},
    ]
    out = _to_sdk_messages(neutral)
    roles = [m["message"]["role"] for m in out]
    # the tool_results turn is rendered as a USER text message (no native tool blocks)
    assert roles == ["user", "assistant", "user", "user"]
    assert all(isinstance(m["message"]["content"], str) for m in out)
    assert "[called tool probe_environment" in out[1]["message"]["content"]
    assert "[tool results]" in out[2]["message"]["content"]


# ---- chat() orchestration via a monkeypatched query ----------------------------------------

def _settings() -> Settings:
    return Settings(llm_provider="claude-agent-sdk", agent_sdk_model="claude-sonnet-4-6")


def _fake_query(messages, *, raises: Exception | None = None):
    """Build a stand-in for claude_agent_sdk.query that yields the given SDK messages
    (then optionally raises, mimicking the terminal max_turns error)."""
    async def _q(*, prompt, options, transport=None):
        for m in messages:
            yield m
        if raises is not None:
            raise raises
    return _q


@pytest.fixture()
def tools():
    return [{"name": "probe_environment", "description": "Probe the env.",
             "input_schema": {"type": "object", "properties": {"namespace": {"type": "string"}}}}]


async def test_chat_text_only_turn(monkeypatch, tools):
    msgs = [
        sdk.AssistantMessage(content=[sdk.TextBlock(text="All set — your cluster is ready.")],
                             model="claude-sonnet-4-6"),
        sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                          num_turns=1, session_id="s",
                          usage={"input_tokens": 10, "output_tokens": 8,
                                 "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 0}),
    ]
    monkeypatch.setattr(sdk, "query", _fake_query(msgs))
    turn = await AgentSdkProvider(_settings()).chat(
        system="sys", messages=[{"role": "user", "content": "ready?"}], tools=tools)
    assert turn.text == "All set — your cluster is ready."
    assert turn.tool_calls == []
    assert turn.stop_reason == "end_turn"
    assert (turn.usage.output_tokens, turn.usage.cache_read_tokens) == (8, 1000)


async def test_chat_tool_turn_captures_calls_strips_prefix_and_swallows_maxturns(monkeypatch, tools):
    msgs = [
        sdk.AssistantMessage(
            content=[sdk.TextBlock(text="probing now"),
                     sdk.ToolUseBlock(id="toolu_1", name="mcp__benchtools__probe_environment",
                                      input={"namespace": "llm-d"})],
            model="claude-sonnet-4-6"),
        sdk.ResultMessage(subtype="error_max_turns", duration_ms=1, duration_api_ms=1, is_error=True,
                          num_turns=1, session_id="s",
                          usage={"input_tokens": 3, "output_tokens": 101,
                                 "cache_read_input_tokens": 0, "cache_creation_input_tokens": 1639}),
    ]
    # the SDK raises a terminal error after the one allowed turn — must be treated as the
    # EXPECTED stop, not a failure, because we captured a usable tool call.
    monkeypatch.setattr(sdk, "query",
                        _fake_query(msgs, raises=Exception("Reached maximum number of turns (1)")))
    turn = await AgentSdkProvider(_settings()).chat(
        system="sys", messages=[{"role": "user", "content": "check cluster"}], tools=tools)
    assert turn.text == "probing now"
    assert turn.stop_reason == "tool_use"
    assert len(turn.tool_calls) == 1
    tc = turn.tool_calls[0]
    assert (tc.id, tc.name, tc.input) == ("toolu_1", "probe_environment", {"namespace": "llm-d"})
    assert turn.usage.cache_write_tokens == 1639


async def test_chat_surfaces_api_error_as_provider_error(monkeypatch, tools):
    msgs = [
        sdk.AssistantMessage(content=[], model="<synthetic>", error="invalid_request"),
        sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=True,
                          num_turns=1, session_id="s", usage=None, api_error_status=400),
    ]
    monkeypatch.setattr(sdk, "query", _fake_query(msgs))
    with pytest.raises(ProviderError):
        await AgentSdkProvider(_settings()).chat(
            system="sys", messages=[{"role": "user", "content": "x"}], tools=tools)


async def test_chat_connection_failure_raises_provider_error(monkeypatch, tools):
    # Nothing usable came back (e.g. CLI missing / not logged in) -> ProviderError, not a hang.
    monkeypatch.setattr(sdk, "query", _fake_query([], raises=RuntimeError("CLI not found")))
    with pytest.raises(ProviderError):
        await AgentSdkProvider(_settings()).chat(
            system="sys", messages=[{"role": "user", "content": "x"}], tools=tools)


# ---- factory + self-check wiring -----------------------------------------------------------

def test_get_provider_selects_agent_sdk():
    assert isinstance(get_provider(_settings()), AgentSdkProvider)
    # aliases resolve to the same provider
    assert isinstance(get_provider(Settings(llm_provider="agent-sdk")), AgentSdkProvider)


def test_self_check_treats_agent_sdk_as_keyless_coherent():
    from app.storage.retention import _check_provider_coherent
    out = _check_provider_coherent(_settings())
    assert out.ok is True
    assert out.data["key_attr"] is None
