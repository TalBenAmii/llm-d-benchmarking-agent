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
"""
from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any

from app.agent import events
from app.agent.session import Session


class Channel:
    def __init__(self, session: Session):
        self.session = session
        self.ws: Any = None  # the currently-attached WebSocket, or None
        # request_id -> {future, kind, payload, tool_call_id}
        self.pending: dict[str, dict[str, Any]] = {}

    async def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        # Record the executed-command trail on the session so a resumed chat can replay it in
        # the command/debug view (kept out of the LLM message stream) — even if no socket is
        # currently attached to receive the live event.
        if event_type == events.COMMAND:
            self.session.record_command(payload)
        ws = self.ws
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.send_json({"type": event_type, "data": payload})

    async def request_approval(self, kind: str, payload: dict[str, Any]) -> bool:
        """Surface an Approve/Reject gate and block until the user (now or after reconnecting)
        decides. If no socket is attached the emit is a no-op and the turn simply pauses — the
        card is re-surfaced via :meth:`reemit_pending` when a socket reattaches."""
        rid = uuid.uuid4().hex[:8]
        tool_call_id = self.session.ctx.current_tool_call_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[rid] = {"future": fut, "kind": kind, "payload": payload, "tool_call_id": tool_call_id}
        await self.emit(events.APPROVAL_REQUEST, {"request_id": rid, "kind": kind, "payload": payload})
        try:
            approved = bool(await fut)
        finally:
            self.pending.pop(rid, None)
        # Persist the decision (and only a real decision — a cancelled future never reaches
        # here) so the resolved card replays in the transcript on resume.
        self.session.record_approval({
            "tool_call_id": tool_call_id, "request_id": rid,
            "kind": kind, "payload": payload, "approved": approved,
        })
        self.session.persist()
        return approved

    async def reemit_pending(self) -> None:
        """Re-surface every still-undecided approval to the freshly-attached socket."""
        for rid, p in list(self.pending.items()):
            await self.emit(events.APPROVAL_REQUEST, {
                "request_id": rid, "kind": p["kind"], "payload": p["payload"],
            })

    def resolve(self, rid: str | None, approved: bool) -> bool:
        """Fulfil a pending approval from a client ``approval`` message. Idempotent."""
        p = self.pending.get(rid) if rid else None
        if p and not p["future"].done():
            p["future"].set_result(bool(approved))
            return True
        return False
