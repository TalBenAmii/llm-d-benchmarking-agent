"""Token-tracking + provider-agnostic prompt-caching unit tests.

Two concerns, all hermetic (no network, no live cluster, no repo dependency):
  1. Anthropic provider attaches ephemeral cache_control at the 3 prefix breakpoints (tools,
     system, rolling conversation tail) AND parses real usage from resp.usage.
  2. The agent loop accumulates per-call Usage into the running turn + persisted session tally,
     emits a `usage` event per step, and the session totals survive persist()/load().
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.agent import events
from app.agent.loop import AgentLoop
from app.agent.session import SessionManager
from app.config import Settings, get_settings
from app.llm.anthropic_provider import AnthropicProvider, _mark_last_cacheable
from app.llm.provider import AssistantTurn, Usage
from app.security.allowlist import Allowlist
from app.security.runner import CommandRunner
from tests._helpers import _session

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST_PATH = PROJECT_ROOT / "security" / "allowlist.yaml"


# ---- capturing fake provider clients -------------------------------------------------------

class _FakeAnthropicMessages:
    def __init__(self, parent):
        self._parent = parent

    async def create(self, **kwargs):
        self._parent.captured = kwargs
        # A realistic Anthropic response: text block + a usage object. Anthropic EXCLUDES the
        # cached read/write tokens from input_tokens, so the provider must NOT subtract.
        usage = SimpleNamespace(
            input_tokens=120,
            output_tokens=45,
            cache_read_input_tokens=4000,
            cache_creation_input_tokens=300,
        )
        content = [SimpleNamespace(type="text", text="hi there")]
        return SimpleNamespace(content=content, stop_reason="end_turn", usage=usage)


class _FakeAnthropicClient:
    def __init__(self):
        self.captured = None
        self.messages = _FakeAnthropicMessages(self)


def _anthropic_provider() -> AnthropicProvider:
    p = AnthropicProvider.__new__(AnthropicProvider)
    p._client = _FakeAnthropicClient()
    p._model = "claude-test"
    return p


_TOOLS = [{"name": "probe", "description": "d", "input_schema": {"type": "object"}}]


# ---- Anthropic: caching breakpoints + usage parsing ----------------------------------------

async def test_anthropic_marks_cache_breakpoints_and_parses_usage():
    p = _anthropic_provider()
    messages = [
        {"role": "user", "content": "deploy something"},
        {"role": "assistant", "content": "ok", "tool_calls": []},
        {"role": "user", "content": "go"},
    ]
    turn = await p.chat(system="SYSTEM PREFIX", messages=messages, tools=_TOOLS, cache_key="sess1")
    cap = p._client.captured

    # (i) the LAST tool carries an ephemeral cache_control (caches the whole tools block).
    assert cap["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    # (ii) the system block is a single text block with cache_control.
    assert cap["system"] == [{"type": "text", "text": "SYSTEM PREFIX", "cache_control": {"type": "ephemeral"}}]
    # (iii) the LAST message's LAST content block carries cache_control (rolling conversation).
    last_msg = cap["messages"][-1]
    assert isinstance(last_msg["content"], list)
    assert last_msg["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert last_msg["content"][-1]["text"] == "go"  # the plain-string user content was block-ified

    # usage parsed with the correct normalization (Anthropic input_tokens excludes cache).
    assert turn.usage.input_tokens == 120
    assert turn.usage.output_tokens == 45
    assert turn.usage.cache_read_tokens == 4000
    assert turn.usage.cache_write_tokens == 300
    assert turn.usage.total_input == 120 + 4000 + 300


async def test_anthropic_usage_defensive_when_missing():
    p = _anthropic_provider()

    async def _create(**kwargs):
        p._client.captured = kwargs
        # A response with NO usage attribute / partial usage must not crash.
        content = [SimpleNamespace(type="text", text="hi")]
        return SimpleNamespace(content=content, stop_reason="end_turn", usage=None)

    p._client.messages.create = _create  # type: ignore[assignment]
    turn = await p.chat(system="s", messages=[{"role": "user", "content": "x"}], tools=_TOOLS)
    assert turn.usage == Usage()


def test_mark_last_cacheable_noop_on_empty():
    out: list = []
    _mark_last_cacheable(out)  # must not raise
    assert out == []


def test_mark_last_cacheable_handles_list_content():
    out = [{"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}]
    _mark_last_cacheable(out)
    assert out[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in out[-1]["content"][0]  # only the LAST block is marked


# ---- loop accumulation + persist/load ------------------------------------------------------

class _UsageProvider:
    """Returns text-only AssistantTurns (no tool calls -> the turn ends after this call), each
    carrying a fixed Usage so we can assert exact accumulation."""

    def __init__(self, usages: list[Usage]):
        self._usages = usages
        self.i = 0

    async def chat(self, *, system, messages, tools, cache_key=None):
        u = self._usages[self.i]
        self.i += 1
        return AssistantTurn(text="step", tool_calls=[], usage=u)


async def test_loop_emits_usage_and_accumulates_session_totals(tmp_path):
    # A text-only turn ends after one LLM call, so each run_turn = one step. Run two turns and
    # assert the session tally accumulates across both and a `usage` event is emitted each step.
    session = _session(tmp_path, sid="tok")
    captured: list[tuple[str, dict]] = []

    async def emit(t, p):
        captured.append((t, p))

    async def request_approval(kind, payload):
        return True

    # Turn 1: one call, usage A.
    provider1 = _UsageProvider([Usage(input_tokens=100, output_tokens=20, cache_read_tokens=900, cache_write_tokens=10)])
    await AgentLoop(provider1).run_turn(session, "hello", emit=emit, request_approval=request_approval)

    usage_events = [p for (t, p) in captured if t == events.USAGE]
    assert len(usage_events) == 1
    ue = usage_events[0]
    assert ue["turn"]["input"] == 100
    assert ue["turn"]["output"] == 20
    assert ue["turn"]["cache_read"] == 900
    assert ue["turn"]["cache_write"] == 10
    assert ue["turn"]["calls"] == 1
    # turn.total = total_input + output = (100+900+10) + 20
    assert ue["turn"]["total"] == 1030
    assert ue["session"]["total"] == 1030
    # context_window = total_input of THIS call (fresh+cache_read+cache_write), NOT the per-turn
    # sum and NOT including output. No model limit/percentage (model can change) — just the count.
    assert ue["context_window"]["tokens"] == 100 + 900 + 10
    assert "limit" not in ue["context_window"]
    assert session.last_context_tokens == 1010

    # Session cumulative fields after turn 1.
    assert session.total_input_tokens == 100
    assert session.total_output_tokens == 20
    assert session.total_cache_read_tokens == 900
    assert session.total_cache_write_tokens == 10
    assert session.session_total == 1030

    # Turn 2: another call, usage B — must ADD to the session tally.
    captured.clear()
    provider2 = _UsageProvider([Usage(input_tokens=50, output_tokens=5, cache_read_tokens=1000, cache_write_tokens=0)])
    await AgentLoop(provider2).run_turn(session, "again", emit=emit, request_approval=request_approval)

    ue2 = [p for (t, p) in captured if t == events.USAGE][-1]
    # turn totals reset per turn.
    assert ue2["turn"]["input"] == 50
    assert ue2["turn"]["calls"] == 1
    # context_window tracks the LATEST call only — it does NOT accumulate across turns.
    assert ue2["context_window"]["tokens"] == 50 + 1000 + 0
    assert session.last_context_tokens == 1050
    # session totals accumulated across both turns.
    assert session.total_input_tokens == 150
    assert session.total_output_tokens == 25
    assert session.total_cache_read_tokens == 1900
    assert session.total_cache_write_tokens == 10
    assert session.session_total == 2085
    assert ue2["session"]["total"] == 2085


async def test_session_token_totals_survive_persist_and_load(tmp_path):
    al = Allowlist.from_file(ALLOWLIST_PATH)
    runner = CommandRunner(get_settings().repo_paths)
    # Point the manager's root at the temp workspace.
    s_settings = Settings(workspace_dir=tmp_path)
    mgr2 = SessionManager(s_settings, al, runner)

    sess = mgr2.create()
    sess.messages.append({"role": "user", "content": "hi"})
    sess.total_input_tokens = 111
    sess.total_output_tokens = 22
    sess.total_cache_read_tokens = 3333
    sess.total_cache_write_tokens = 44
    sess.last_context_tokens = 5050
    sess.persist()

    # Drop from memory and reload from disk.
    mgr2._sessions.clear()
    loaded = mgr2.load(sess.id)
    assert loaded is not None
    assert loaded.total_input_tokens == 111
    assert loaded.total_output_tokens == 22
    assert loaded.total_cache_read_tokens == 3333
    assert loaded.total_cache_write_tokens == 44
    # The context-window meter is correct on reload before the next turn refreshes it.
    assert loaded.last_context_tokens == 5050
    assert loaded.session_total == 111 + 22 + 3333 + 44


def test_old_state_json_without_tokens_loads_as_zero(tmp_path):
    al = Allowlist.from_file(ALLOWLIST_PATH)
    runner = CommandRunner(get_settings().repo_paths)
    s_settings = Settings(workspace_dir=tmp_path)
    mgr = SessionManager(s_settings, al, runner)
    sess = mgr.create()
    sess.messages.append({"role": "user", "content": "hi"})
    sess.persist()
    # Simulate a pre-feature state.json by stripping the token fields.
    import json
    state = mgr._root / sess.id / "state.json"
    data = json.loads(state.read_text())
    for k in list(data):
        if k.startswith("total_") and k.endswith("_tokens"):
            del data[k]
    state.write_text(json.dumps(data))
    mgr._sessions.clear()
    loaded = mgr.load(sess.id)
    assert loaded is not None
    assert loaded.session_total == 0
