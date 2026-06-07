"""A per-session link between a running turn and whatever WebSocket is currently attached.

Turns outlive any single connection: a benchmark keeps running after you navigate away, and
— crucially — a turn parked at an approval gate must survive a chat switch and resume when
you reopen the chat. Binding ``emit``/``request_approval`` to one connection breaks both:
closing the socket would either kill the turn or (as it used to) auto-reject the pending
approval.

So the turn talks to a stable ``Channel`` instead. The Channel forwards events to the
*current* socket (or drops them while none is attached — the turn just pauses), holds the
set of in-flight approval requests so a reconnecting socket can re-surface them, and records
each decided approval onto the session for ordered history replay. A turn parked here holds
no concurrency slot (the run semaphore is acquired only after approval), so it can wait
indefinitely for the user to return without starving other runs.

Live event buffer (Phase 15)
----------------------------
A reconnecting client used to see only the turn's *final* result (replayed from the LLM
transcript on resume), not the live stream it missed while disconnected. To close that gap
the Channel keeps a small per-turn pub/sub buffer: while a turn runs, every emitted event is
appended to a BOUNDED ring buffer (a ``deque(maxlen=...)``) as well as fanned out to the live
socket. On reconnect mid-turn the buffer is replayed so the client catches up to the LIVE
stream and then continues live. The buffer is reset at the start of each turn (so it holds
only the in-flight turn's events) and capped to avoid unbounded memory on a long, chatty turn.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections import deque
from typing import Any

from app.agent import events
from app.agent.session import Session
from app.agent.ws_schemas import outbound

# Cap on the per-turn live buffer. A single turn streams tool calls, command lines, and
# streamed stdout; a few hundred events comfortably covers the visible tail a reconnecting
# client needs to catch up, while bounding memory (the oldest events fall off the deque). This
# is mechanism, not judgment — purely a memory guard.
LIVE_BUFFER_MAX = 500


class Channel:
    def __init__(self, session: Session, buffer_max: int = LIVE_BUFFER_MAX):
        self.session = session
        self.ws: Any = None  # the currently-attached WebSocket, or None
        # request_id -> {future, kind, payload, tool_call_id}
        self.pending: dict[str, dict[str, Any]] = {}
        # Bounded live-event ring buffer for the CURRENTLY-RUNNING turn. Appended on every
        # emit, replayed to a socket that (re)attaches mid-turn, and reset when a new turn
        # begins. deque(maxlen) drops the oldest event once full -> capped memory.
        self._buffer: deque[dict[str, Any]] = deque(maxlen=buffer_max)
        self._turn_active = False
        # Monotonic timestamp of when the in-flight turn began, or None between turns. Used to
        # report server-authoritative elapsed time so a client that reconnects mid-turn shows
        # the REAL accumulated "thinking seconds", not a fresh zero (chat-switch state fix).
        # ``time.monotonic`` is immune to wall-clock/NTP jumps and we only ever take a delta.
        self._turn_started_monotonic: float | None = None
        # Channel-lifetime sequence counter stamped onto every buffered (turn) event. It is a
        # RESUME CURSOR: a reconnecting client says "I have through seq N" and we replay only
        # newer frames onto its cached view. Monotonic across turns (NOT reset in begin_turn,
        # which only clears the buffer) so a cursor stays comparable even past a turn boundary.
        self._seq = 0

    def begin_turn(self) -> None:
        """Mark the start of a fresh turn and clear the live buffer.

        Called before a turn's first event so the buffer holds ONLY the in-flight turn's
        events — a reconnecting client replays the current run, not a stale prior one. The
        sequence counter is deliberately NOT reset (it spans the channel's lifetime) so a
        cursor from the previous turn that predates this turn's first buffered seq reliably
        signals "you missed a turn boundary — do a full rebuild".
        """
        self._buffer.clear()
        self._turn_active = True
        self._turn_started_monotonic = time.monotonic()

    def end_turn(self) -> None:
        """Mark the running turn finished. The buffered events are kept until the next turn
        begins (a client that reconnects in the brief window after ``done`` still catches the
        tail), but no longer treated as a live, in-progress stream."""
        self._turn_active = False
        self._turn_started_monotonic = None

    @property
    def turn_active(self) -> bool:
        return self._turn_active

    @property
    def elapsed_ms(self) -> int | None:
        """Milliseconds since the in-flight turn began, or None when no turn is active.

        Sampled at the instant the handler builds the ``ready`` frame on (re)connect, so a
        client can seed its elapsed-time counter from the true start and keep ticking."""
        if not self._turn_active or self._turn_started_monotonic is None:
            return None
        return int((time.monotonic() - self._turn_started_monotonic) * 1000)

    @property
    def cur_seq(self) -> int:
        """Highest sequence number emitted so far (the live cursor head)."""
        return self._seq

    @property
    def min_buffered_seq(self) -> int | None:
        """Lowest sequence number still retained in the ring buffer, or None if empty.

        A client cursor below ``min_buffered_seq - 1`` has fallen off the buffer (the gap was
        evicted, or a new turn cleared it) → it must do a full rebuild rather than patch."""
        return self._buffer[0].get("seq") if self._buffer else None

    @property
    def buffered_events(self) -> list[dict[str, Any]]:
        """The current live buffer as a list of outbound frames (oldest first)."""
        return list(self._buffer)

    async def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        # Record the executed-command trail on the session so a resumed chat can replay it in
        # the command/debug view (kept out of the LLM message stream) — even if no socket is
        # currently attached to receive the live event.
        if event_type == events.COMMAND:
            self.session.record_command(payload)
        # Append to the bounded live buffer FIRST so a client that reattaches mid-turn can
        # replay this event even if the send below finds no socket attached right now. Skip
        # connection-lifecycle frames (ready/history/pong): the handler emits those on every
        # (re)connect, and buffering them would make a SECOND mid-turn reconnect replay a stale
        # handshake interleaved before the real missed turn events. The buffer holds only the
        # in-flight turn's events, exactly as the docstring promises.
        frame = outbound(event_type, payload)
        if event_type not in events.NON_TURN_EVENTS:
            # Stamp a resume cursor onto turn events only (lifecycle frames stay seqless — the
            # handler re-sends them on every connect, so they must never advance the cursor).
            # The live frame carries the same seq, so the attached client tracks its cursor in
            # real time and asks for exactly the right tail if it later reconnects.
            self._seq += 1
            frame = {**frame, "seq": self._seq}
            self._buffer.append(frame)
        ws = self.ws
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.send_json(frame)

    async def replay_live(self, after_seq: int | None = None) -> None:
        """Replay the buffered live events for the in-flight turn to the current socket.

        Called when a socket (re)attaches while a turn is running, so a client that dropped
        mid-run catches up to the LIVE stream (every progress event it missed, in order) and
        then keeps receiving new events as they happen. A no-op when no turn is active or the
        buffer is empty. Sent verbatim (same envelope as the live frames) so the client reuses
        its existing renderers.

        ``after_seq`` is the client's resume cursor: when given, only frames with a strictly
        greater seq are replayed, so a client that kept its rendered transcript (a cached DOM
        pane) is patched with ONLY the tail it missed — no duplicate rendering, no full
        rebuild. ``None`` replays the whole buffer (the original behavior, for a client that
        starts from a fresh/rebuilt transcript).

        Approval gates are deliberately SKIPPED here: they're stateful (an
        ``approval_request`` in the buffer may already be decided, or may be the one currently
        blocking the turn), so re-surfacing them is owned solely by :meth:`reemit_pending`,
        which re-sends only the still-undecided ones. Replaying them from the buffer too would
        double-render the pending card and resurrect resolved ones as live, clickable cards.
        """
        ws = self.ws
        if ws is None:
            return
        for frame in list(self._buffer):
            if frame.get("type") == events.APPROVAL_REQUEST:
                continue
            if after_seq is not None and frame.get("seq", 0) <= after_seq:
                continue
            with contextlib.suppress(Exception):
                await ws.send_json(frame)

    async def request_approval(self, kind: str, payload: dict[str, Any]) -> bool:
        """Surface an Approve/Reject gate and block until the user (now or after reconnecting)
        decides. If no socket is attached the emit is a no-op and the turn simply pauses — the
        card is re-surfaced via :meth:`reemit_pending` when a socket reattaches."""
        rid = uuid.uuid4().hex[:8]
        tool_call_id = self.session.ctx.current_tool_call_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[rid] = {"future": fut, "kind": kind, "payload": payload, "tool_call_id": tool_call_id}
        # Persist the still-undecided gate so it survives a chat switch / pane eviction / channel
        # eviction: it can be replayed in its transcript position from session state on reconnect
        # (via _history_items), independent of whether this in-memory Channel is still alive.
        self.session.record_in_flight_approval({
            "tool_call_id": tool_call_id, "request_id": rid, "kind": kind, "payload": payload,
        })
        self.session.persist()
        await self.emit(events.APPROVAL_REQUEST, {"request_id": rid, "kind": kind, "payload": payload})
        try:
            approved = bool(await fut)
        finally:
            self.pending.pop(rid, None)
            # Resolved OR cancelled: the gate is no longer pending, so drop it from the in-flight
            # set and persist the cleared state right here (a decided gate is additionally recorded
            # below as a durable approval). Persisting in finally — not only on the resolved path —
            # keeps the on-disk snapshot correct even when the future is CANCELLED (the lines below
            # never run), so a resumed chat never re-surfaces a gate that's already gone.
            self.session.clear_in_flight_approval(rid)
            self.session.persist()
        # Persist the decision (and only a real decision — a cancelled future never reaches
        # here) so the resolved card replays in the transcript on resume.
        self.session.record_approval({
            "tool_call_id": tool_call_id, "request_id": rid,
            "kind": kind, "payload": payload, "approved": approved,
        })
        self.session.persist()
        return approved

    def restore_pending(self, in_flight: list[dict[str, Any]]) -> None:
        """Repopulate ``self.pending`` from a session's persisted in-flight approval gates.

        A freshly-created Channel (e.g. after the previous one was evicted, or on reconnect to a
        chat whose turn is parked at a gate) starts with an empty ``pending`` dict, so
        :meth:`reemit_pending` would have nothing to re-surface and :meth:`resolve` could not
        accept the user's decision — the live approval card would be silently lost. Reconstruct a
        COMPATIBLE entry (``request_id``/``kind``/``payload``/``tool_call_id`` come from the
        persisted gate) with a FRESH future so a later approve/reject can resolve it.

        The reconstructed future has no ``request_approval`` coroutine awaiting it (that coroutine
        belonged to the evicted Channel / the original turn task, which may be gone). So entries
        restored here are tagged ``restored: True``; :meth:`resolve` recognises that tag and does
        the bookkeeping ``request_approval`` would otherwise do on resolution (clear the in-flight
        gate + record the decision + persist), instead of relying on an awaiter that isn't there.
        Idempotent — a gate already live in ``pending`` (the original Channel was reused) is left
        untouched so its real awaited future is preserved.
        """
        loop = asyncio.get_event_loop()
        for entry in in_flight or []:
            rid = entry.get("request_id")
            if not rid or rid in self.pending:
                continue
            self.pending[rid] = {
                "future": loop.create_future(),
                "kind": entry.get("kind"),
                "payload": entry.get("payload"),
                "tool_call_id": entry.get("tool_call_id"),
                "restored": True,
            }

    async def reemit_pending(self) -> None:
        """Re-surface every still-undecided approval to the freshly-attached socket."""
        for rid, p in list(self.pending.items()):
            await self.emit(events.APPROVAL_REQUEST, {
                "request_id": rid, "kind": p["kind"], "payload": p["payload"],
            })

    def resolve(self, rid: str | None, approved: bool) -> bool:
        """Fulfil a pending approval from a client ``approval`` message. Idempotent."""
        if not rid:
            return False
        p = self.pending.get(rid)
        if not p or p["future"].done():
            return False
        approved = bool(approved)
        p["future"].set_result(approved)
        if p.get("restored"):
            # No ``request_approval`` coroutine is awaiting this future (it was reconstructed on a
            # fresh Channel after the original turn/Channel was gone), so its resolution bookkeeping
            # — drop the gate from the in-flight set, record the decision for history replay, and
            # persist — must happen here instead. Then drop it from pending so it can't re-surface.
            self.pending.pop(rid, None)
            self.session.clear_in_flight_approval(rid)
            self.session.record_approval({
                "tool_call_id": p.get("tool_call_id"), "request_id": rid,
                "kind": p.get("kind"), "payload": p.get("payload"), "approved": approved,
            })
            self.session.persist()
        return True
