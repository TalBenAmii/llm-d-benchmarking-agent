"""W2 — environment pre-probe injection into the first turn.

When the /ws handler pre-probed the environment (session.env_snapshot set, prewarmed False),
loop.run_turn injects a synthetic "[environment pre-probe …]" user message BEFORE the real user
text and flips prewarmed True. A second turn must NOT re-inject. Driven by the repo's scripted
FakeProvider so no API key / cluster is needed.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.agent.loop import AgentLoop
from app.config import get_settings
from app.llm.provider import AssistantTurn
from tests._helpers import _session

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeProvider:
    """Returns a no-tool-call turn each time and records the messages it was handed, so the
    test can inspect exactly what the loop assembled for the model."""
    def __init__(self, n=4):
        self.seen_messages: list[list[dict]] = []
        self._n = n

    async def chat(self, *, system, messages, tools, cache_key=None):
        self.seen_messages.append([dict(m) for m in messages])
        return AssistantTurn(text="ok", tool_calls=[])


def _pre_probe_msgs(messages):
    return [m for m in messages if m.get("role") == "user"
            and str(m.get("content", "")).startswith("[environment pre-probe")]


async def test_prewarm_snapshot_injected_before_user_text(tmp_path):
    if not get_settings().bench_repo.is_dir():
        pytest.skip("repo not present")
    session = _session(tmp_path)
    session.env_snapshot = {"tools": {"kubectl": True}, "kind_clusters": {"clusters": ["c1"]}}
    assert session.prewarmed is False

    provider = FakeProvider()

    async def emit(t, p):
        pass

    async def approve(kind, payload):
        return True

    await AgentLoop(provider).run_turn(session, "benchmark a tiny model", emit=emit, request_approval=approve)

    # The synthetic pre-probe message was appended, carrying the snapshot, BEFORE the real user
    # text — and prewarmed flipped. (A one-shot live-catalog snapshot is injected between the
    # pre-probe and the real user text; the real text still follows the pre-probe.)
    msgs = session.messages
    pre = _pre_probe_msgs(msgs)
    assert len(pre) == 1
    assert "kubectl" in pre[0]["content"]
    # It is tagged synthetic so the UI / title logic can skip it, but the provider still receives
    # it as a real user-role context message (the FakeProvider saw it on the wire).
    assert pre[0].get("synthetic") is True
    assert any(m.get("content") == pre[0]["content"] for m in provider.seen_messages[0])
    idx = msgs.index(pre[0])
    real = {"role": "user", "content": "benchmark a tiny model"}
    assert real in msgs[idx + 1:], "the real user text must follow the pre-probe snapshot"
    assert session.prewarmed is True


async def test_prewarm_large_snapshot_stays_valid_json(tmp_path):
    # A real cluster's snapshot (many namespaces / verbose cluster_info) can exceed the 4000-char
    # budget. The injected message must carry VALID JSON, not an object sliced mid-structure.
    import json

    if not get_settings().bench_repo.is_dir():
        pytest.skip("repo not present")
    session = _session(tmp_path)
    session.env_snapshot = {
        "tools": {"kubectl": True},
        "namespaces": {"items": [f"ns-{i}-{'x' * 40}" for i in range(300)]},
    }
    provider = FakeProvider()

    async def emit(t, p):
        pass

    async def approve(kind, payload):
        return True

    await AgentLoop(provider).run_turn(session, "go", emit=emit, request_approval=approve)

    pre = _pre_probe_msgs(session.messages)
    assert len(pre) == 1
    # The message is "<prose prefix>\n<json>"; the JSON tail must parse cleanly.
    json_part = pre[0]["content"].split("\n", 1)[1]
    parsed = json.loads(json_part)  # would raise on the old mid-structure slice
    assert parsed["_truncated"] is True  # overflow -> truncation envelope


async def test_prewarm_snapshot_excluded_from_history_items(tmp_path):
    # Regression (TODO #6 probe-leak): the synthetic pre-probe message must NOT be replayed as a
    # user bubble when a resumed chat rebuilds its transcript (_history_items skips synthetic).
    from app.main import _history_items

    if not get_settings().bench_repo.is_dir():
        pytest.skip("repo not present")
    session = _session(tmp_path)
    session.env_snapshot = {"tools": {"kubectl": True}}

    provider = FakeProvider()

    async def emit(t, p):
        pass

    async def approve(kind, payload):
        return True

    await AgentLoop(provider).run_turn(session, "benchmark a tiny model", emit=emit, request_approval=approve)

    items = _history_items(session)
    user_texts = [it["text"] for it in items if it["role"] == "user"]
    assert "benchmark a tiny model" in user_texts
    assert not any("environment pre-probe" in t for t in user_texts)


async def test_prewarm_not_reinjected_on_second_turn(tmp_path):
    if not get_settings().bench_repo.is_dir():
        pytest.skip("repo not present")
    session = _session(tmp_path)
    session.env_snapshot = {"tools": {"kubectl": True}}

    provider = FakeProvider()

    async def emit(t, p):
        pass

    async def approve(kind, payload):
        return True

    await AgentLoop(provider).run_turn(session, "first", emit=emit, request_approval=approve)
    await AgentLoop(provider).run_turn(session, "second", emit=emit, request_approval=approve)

    # Exactly ONE pre-probe message across both turns.
    assert len(_pre_probe_msgs(session.messages)) == 1


async def test_prewarm_not_reinjected_after_persist_reload(tmp_path):
    # Regression (BUG F probe-leak across resume): the prewarmed flag is PERSISTED, so a chat
    # that already injected the pre-probe snapshot does NOT re-inject it after a disk reload —
    # even if a fresh env_snapshot gets set (e.g. a later pre-probe). Were prewarmed runtime-only
    # it would reset to False on load and the loop would re-inject the "[environment pre-probe …]"
    # snapshot mid-transcript, leaking it into the rendered chat + sidebar title.
    from app.agent.session import SessionManager
    from app.config import Settings as _Settings
    from app.security.allowlist import Allowlist as _Allowlist
    from app.security.runner import CommandRunner as _Runner

    if not get_settings().bench_repo.is_dir():
        pytest.skip("repo not present")

    # A SessionManager rooted under tmp so persist() + load() agree on the on-disk location and
    # we never touch the real workspace (mirrors test_sessions.make_manager).
    settings = _Settings(workspace_dir=tmp_path)
    mgr = SessionManager(settings, _Allowlist.from_file(settings.allowlist_path),
                         _Runner(settings.repo_paths))
    session = mgr.create()

    provider = FakeProvider()

    async def emit(t, p):
        pass

    async def approve(kind, payload):
        return True

    # First turn injects the snapshot once and persists prewarmed=True.
    session.env_snapshot = {"tools": {"kubectl": True}}
    await AgentLoop(provider).run_turn(session, "first", emit=emit, request_approval=approve)
    assert session.prewarmed is True
    session.persist()

    # Reload from disk as a returning browser would (drops the in-memory copy).
    mgr._sessions.clear()
    reloaded = mgr.load(session.id)
    assert reloaded is not None and reloaded.prewarmed is True
    # A later pre-probe sets a fresh snapshot — but prewarmed survived, so the loop must NOT
    # re-inject it as a visible/user message on the resumed turn.
    reloaded.env_snapshot = {"tools": {"kubectl": True}, "kind_clusters": {"clusters": ["c1"]}}
    await AgentLoop(provider).run_turn(reloaded, "second", emit=emit, request_approval=approve)

    assert len(_pre_probe_msgs(reloaded.messages)) == 1, \
        "the pre-probe snapshot must not be re-injected after a persist/reload resume"


async def test_no_snapshot_means_no_injection(tmp_path):
    if not get_settings().bench_repo.is_dir():
        pytest.skip("repo not present")
    session = _session(tmp_path)
    assert session.env_snapshot is None

    provider = FakeProvider()

    async def emit(t, p):
        pass

    async def approve(kind, payload):
        return True

    await AgentLoop(provider).run_turn(session, "go", emit=emit, request_approval=approve)
    assert _pre_probe_msgs(session.messages) == []
    assert session.prewarmed is False
