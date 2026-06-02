"""The two new server->client events (W1 suggestions, W3 resource_stats) must be treated as
NON-turn events: they are in NON_TURN_EVENTS and Channel.emit must NOT buffer them into the
per-turn live ring (so a mid-turn reconnect replays real progress, not a wall of stat samples
or a stale handshake)."""
from __future__ import annotations

from app.agent import events
from app.agent.channel import Channel


def test_new_events_are_non_turn():
    assert events.SUGGESTIONS in events.NON_TURN_EVENTS
    assert events.RESOURCE_STATS in events.NON_TURN_EVENTS


class _FakeSession:
    """Minimal stand-in: Channel.emit only touches record_command for COMMAND events."""
    def record_command(self, payload):  # pragma: no cover - not exercised here
        raise AssertionError("record_command should not fire for non-command events")


async def test_channel_does_not_buffer_resource_stats():
    ch = Channel.__new__(Channel)
    Channel.__init__(ch, _FakeSession())  # type: ignore[arg-type]
    ch.begin_turn()
    await ch.emit(events.RESOURCE_STATS, {"available": True, "rows": []})
    await ch.emit(events.SUGGESTIONS, {"chips": []})
    # A real turn event IS buffered, proving the buffer works and the two above were excluded.
    await ch.emit(events.ASSISTANT_TEXT, {"text": "hi"})
    types = [f["type"] for f in ch.buffered_events]
    assert events.RESOURCE_STATS not in types
    assert events.SUGGESTIONS not in types
    assert events.ASSISTANT_TEXT in types
