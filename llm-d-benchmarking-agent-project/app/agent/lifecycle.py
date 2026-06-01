"""Run lifecycle (Phase 16) — cancel, reattach, and graceful shutdown for in-flight turns.

A "run" here is one in-flight agent *turn* task: the background coroutine that drives the
LLM loop and (once an action is approved) holds a slot of the cross-session concurrency-cap
semaphore while a heavy command executes. Phase 2 left two deferrals this module closes:

  * an ABANDONED run (the client navigated away mid-benchmark) keeps holding its concurrency
    slot until the command's own timeout — there was no way to cancel it and free the slot;
  * on server shutdown the in-flight turns were simply orphaned (their K8s Jobs / subprocesses
    leaked) rather than being cancelled or cleanly detached.

The fix is pure MECHANISM (thin code): a registry that holds each session's running turn task
and can cancel it. Cancelling the task is exactly what frees the semaphore slot — asyncio
unwinds the ``async with run_semaphore`` inside ``ToolContext.run_command`` as the
``CancelledError`` propagates, and the runner (see ``app/security/runner.py``) reaps the child
process group on that same ``CancelledError`` so nothing is orphaned. The JUDGMENT about *when*
a user should cancel a run lives in ``knowledge/run_lifecycle.md``, never here.

This module embeds no decision logic in ``if/elif`` branches: it tracks tasks and cancels them.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("app.agent.lifecycle")


@dataclass
class RunHandle:
    """Bookkeeping for one in-flight turn: its task plus the metadata the UI/agent surfaces."""
    session_id: str
    task: asyncio.Task
    cancelled: bool = False

    @property
    def active(self) -> bool:
        return not self.task.done()


@dataclass
class RunRegistry:
    """Tracks the in-flight turn per session and cancels them — the single mechanism behind
    the cancel tool, the ``cancel`` control message, and the graceful-shutdown handler.

    There is at most ONE running turn per session (the /ws handler already rejects a second
    concurrent turn on the same chat), so the registry is keyed by session id. Cancelling a
    turn's task releases any concurrency-cap semaphore slot it holds (the ``async with`` in
    ``ToolContext.run_command`` unwinds on ``CancelledError``) and the runner reaps the child
    process group — so a cancelled run frees its slot AND orphans nothing.
    """
    _runs: dict[str, RunHandle] = field(default_factory=dict)

    # ---- registration ------------------------------------------------------
    def register(self, session_id: str, task: asyncio.Task) -> RunHandle:
        """Record (or replace) the in-flight turn for a session. The task self-deregisters via
        :meth:`forget` from its own ``finally`` when it completes normally."""
        handle = RunHandle(session_id=session_id, task=task)
        self._runs[session_id] = handle
        return handle

    def forget(self, session_id: str, task: asyncio.Task) -> None:
        """Drop a session's handle, but only if it still refers to THIS task (a newer turn may
        have already replaced it). Called from the turn's ``finally`` on normal completion."""
        handle = self._runs.get(session_id)
        if handle is not None and handle.task is task:
            del self._runs[session_id]

    # ---- queries -----------------------------------------------------------
    def get(self, session_id: str) -> RunHandle | None:
        return self._runs.get(session_id)

    def is_running(self, session_id: str) -> bool:
        handle = self._runs.get(session_id)
        return handle is not None and handle.active

    def active_session_ids(self) -> set[str]:
        return {sid for sid, h in self._runs.items() if h.active}

    def active_handles(self) -> list[RunHandle]:
        return [h for h in self._runs.values() if h.active]

    # ---- cancel ------------------------------------------------------------
    async def cancel(self, session_id: str, *, timeout: float = 5.0) -> bool:
        """Cancel the in-flight turn for ``session_id`` and AWAIT its unwind so the caller can
        rely on the concurrency slot being freed on return. Returns True if a live run was
        cancelled, False if there was no active run for that session (idempotent).

        Awaiting the task here is what makes the spec's acceptance hold deterministically: the
        semaphore slot is released as the ``CancelledError`` unwinds the ``async with`` inside
        the runner call, and we don't return until that has happened (bounded by ``timeout``)."""
        handle = self._runs.get(session_id)
        if handle is None or handle.task.done():
            return False
        handle.cancelled = True
        log.info("run.cancel", extra={"session_id": session_id})
        return await self._cancel_handle(handle, timeout=timeout)

    @staticmethod
    async def _cancel_handle(handle: RunHandle, *, timeout: float) -> bool:
        handle.task.cancel()
        # Await the task so we KNOW the slot has been released by the time we return. A
        # well-behaved turn task swallows CancelledError in its own finally and completes; if it
        # somehow doesn't unwind in time we still return (best-effort, never block shutdown).
        with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception):
            await asyncio.wait_for(asyncio.shield(handle.task), timeout=timeout)
        return True

    async def shutdown(self, *, timeout: float = 5.0) -> dict[str, Any]:
        """Graceful-shutdown handler (Phase 16): cancel EVERY in-flight turn so a SIGTERM
        doesn't orphan K8s Jobs / leak subprocesses. Returns a structured summary so a caller
        (or a test) can assert what happened. Plain coroutine — a test invokes it DIRECTLY; no
        OS signal required. Idempotent: a second call finds nothing left to cancel."""
        handles = self.active_handles()
        cancelled: list[str] = []
        log.info("run.shutdown.begin", extra={"in_flight": len(handles)})
        for handle in handles:
            handle.cancelled = True
            await self._cancel_handle(handle, timeout=timeout)
            cancelled.append(handle.session_id)
        # Anything that completed on its own during the sweep is forgotten lazily; clear the
        # cancelled ones now so a re-entrant call is a clean no-op.
        for sid in cancelled:
            h = self._runs.get(sid)
            if h is not None and h.task.done():
                self._runs.pop(sid, None)
        log.info("run.shutdown.end", extra={"cancelled": len(cancelled)})
        return {"cancelled": cancelled, "count": len(cancelled)}
