"""Claude Agent SDK provider — hermetic unit tests (no CLI subprocess, no network).

The provider runs inference on the user's Claude subscription via the ``claude`` CLI; here we
exercise the pure conversion logic and the ``chat()`` orchestration by monkeypatching
``claude_agent_sdk.query`` to yield real SDK message objects. The live path (auth, model,
tool-capture) is validated out-of-band against a real Max-plan login.
"""
from __future__ import annotations

import asyncio

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
    # STRICTLY alternating: the trailing tool_results (rendered as user text) and the real user
    # message that follows it are COALESCED into one user turn — the CLI's streaming input only
    # reliably delivers alternating user/assistant turns (see _to_sdk_messages).
    assert roles == ["user", "assistant", "user"]
    # REGRESSION: content MUST be a list of blocks, never a bare string. The CLI scans every
    # input message with content.some(...) for tool_use blocks; a string has no .some and
    # crashes the CLI on the first replayed turn ("H.message.content.some is not a function").
    for m in out:
        content = m["message"]["content"]
        assert isinstance(content, list)
        assert content and all(b.get("type") == "text" and isinstance(b.get("text"), str) for b in content)
    assert "[called tool probe_environment" in out[1]["message"]["content"][0]["text"]
    # the coalesced final user turn carries BOTH the tool results AND the follow-up question.
    merged_user = out[2]["message"]["content"][0]["text"]
    assert "[tool results]" in merged_user and "is it ready?" in merged_user


def test_to_sdk_messages_coalesces_turn1_user_run_so_task_survives():
    """Regression (blank-message bug): on turn 1 the loop injects the env pre-probe snapshot, the
    live-catalog snapshot, and the real user message as THREE back-to-back user turns. The CLI's
    streaming input only surfaced the first, so the model saw the env snapshot but "no user
    message" and replied "I received a blank message". They must coalesce into ONE user turn that
    still carries the user's actual task."""
    neutral = [
        {"role": "user", "synthetic": True,
         "content": "[environment pre-probe — read-only snapshot] docker: up; repos: present"},
        {"role": "user", "content": "[live catalog snapshot] specs: cicd/kind, guides/optimized-baseline"},
        {"role": "user", "content": "I want to benchmark a small chat model on the local quickstart."},
    ]
    out = _to_sdk_messages(neutral)
    # all three user turns collapse to a single user message — not three the CLI would truncate.
    assert [m["message"]["role"] for m in out] == ["user"]
    text = out[0]["message"]["content"][0]["text"]
    # the user's real task is NOT lost (the bug) — and the injected context rides along with it.
    assert "I want to benchmark a small chat model on the local quickstart." in text
    assert "environment pre-probe" in text and "live catalog snapshot" in text


# ---- chat() orchestration via a monkeypatched query ----------------------------------------

def _settings() -> Settings:
    return Settings(llm_provider="claude-agent-sdk", agent_sdk_model="claude-haiku-4-5")


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
                             model="claude-haiku-4-5"),
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
            model="claude-haiku-4-5"),
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


# ---- connection prewarm pool (latency #1b) -------------------------------------------------
# These exercise the pool bookkeeping WITHOUT a real CLI: _connect_client is monkeypatched to
# hand back a fake client (recording connect/disconnect) so adopt / fingerprint-match / TTL /
# leak-cleanup are all verified hermetically.

class _FakeClient:
    def __init__(self):
        self.connected = True
        self.disconnected = False

    async def disconnect(self):
        self.disconnected = True
        self.connected = False


def _provider_with_fake_connect(monkeypatch, *, connects: list, fail: bool = False):
    """An AgentSdkProvider whose _connect_client returns a fresh _FakeClient each call (or raises
    when ``fail``), appending every produced client to ``connects`` so a test can inspect them."""
    p = AgentSdkProvider(_settings())

    async def _fake_connect(system, tools):
        if fail:
            raise RuntimeError("CLI not found")
        c = _FakeClient()
        connects.append(c)
        return c

    monkeypatch.setattr(p, "_connect_client", _fake_connect)
    return p


def _tools():
    return [{"name": "probe_environment", "description": "d", "input_schema": {"type": "object"}}]


async def test_prewarm_adopted_by_matching_next_turn(monkeypatch):
    connects: list = []
    p = _provider_with_fake_connect(monkeypatch, connects=connects)
    # Prewarm one connection, then acquire with the SAME (system, tools) -> adopts it, no new connect.
    p.start_prewarm("sys", _tools())
    adopted = await p.acquire_client("sys", _tools())
    assert adopted is connects[0]           # the prewarmed client itself
    assert len(connects) == 1               # acquire did NOT connect a second time
    assert p._prewarm_task is None          # slot consumed


async def test_acquire_without_prewarm_connects_fresh(monkeypatch):
    connects: list = []
    p = _provider_with_fake_connect(monkeypatch, connects=connects)
    c = await p.acquire_client("sys", _tools())
    assert c is connects[0]
    assert len(connects) == 1


async def test_prewarm_fingerprint_mismatch_is_discarded_not_adopted(monkeypatch):
    connects: list = []
    p = _provider_with_fake_connect(monkeypatch, connects=connects)
    p.start_prewarm("sys-A", _tools())
    await asyncio.sleep(0)                    # let the background connect task run
    spare = connects[0]
    # A turn with a DIFFERENT system prompt must not adopt the mismatched spare; it connects fresh
    # and the spare is disconnected in the background (leak guard).
    c = await p.acquire_client("sys-B", _tools())
    assert c is not spare
    assert len(connects) == 2               # spare + the fresh connect
    await asyncio.sleep(0)                   # let the background cleanup task run
    assert spare.disconnected is True


async def test_stale_prewarm_past_ttl_is_discarded(monkeypatch):
    connects: list = []
    p = _provider_with_fake_connect(monkeypatch, connects=connects)
    p.start_prewarm("sys", _tools())
    await asyncio.sleep(0)                    # let the background connect task run
    spare = connects[0]
    # Force the prewarm to look older than the TTL -> not adopted; a fresh client is connected.
    from app.llm import agent_sdk_provider as mod
    p._prewarm_at -= (mod._PREWARM_TTL_S + 1.0)
    c = await p.acquire_client("sys", _tools())
    assert c is not spare
    assert len(connects) == 2
    await asyncio.sleep(0)
    assert spare.disconnected is True


async def test_start_prewarm_replaces_prior_spare_no_leak(monkeypatch):
    connects: list = []
    p = _provider_with_fake_connect(monkeypatch, connects=connects)
    p.start_prewarm("sys", _tools())
    await asyncio.sleep(0)                    # let the first background connect task run
    first = connects[0]
    p.start_prewarm("sys", _tools())         # a second prewarm before the first was used
    await asyncio.sleep(0.01)                 # background cleanup of the displaced first spare
    assert first.disconnected is True        # the displaced spare is disconnected (single slot)
    assert p._prewarm_task is not None        # the new spare is held


async def test_acquire_falls_back_when_prewarm_connect_failed(monkeypatch):
    # The background prewarm connect raised; acquire must transparently connect fresh (which here
    # also raises, surfacing as the same error the live __aenter__ degrades on) — never adopt a
    # broken task silently.
    p = _provider_with_fake_connect(monkeypatch, connects=[], fail=True)
    p.start_prewarm("sys", _tools())
    with pytest.raises(RuntimeError):
        await p.acquire_client("sys", _tools())


async def test_aclose_disconnects_spare(monkeypatch):
    connects: list = []
    p = _provider_with_fake_connect(monkeypatch, connects=connects)
    p.start_prewarm("sys", _tools())
    await asyncio.sleep(0)                    # let the background connect task run
    spare = connects[0]
    await p.aclose()
    await asyncio.sleep(0.01)
    assert spare.disconnected is True
    assert p._prewarm_task is None
