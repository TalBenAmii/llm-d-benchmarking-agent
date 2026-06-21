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
import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("app.agent.lifecycle")


async def _await_quietly(task: asyncio.Task) -> None:
    """Wait for ``task`` to finish and ABSORB its outcome (the expected ``CancelledError`` from a
    successful cancel, or any other exception it raised) WITHOUT re-raising. This lets the caller
    await a cancelled task purely to observe that it has unwound — and freed its semaphore slot —
    without that task's own ``CancelledError`` cancelling the caller.

    ``asyncio.wait`` is used deliberately: unlike ``await task`` it does not surface the task's
    exception, and unlike ``asyncio.shield`` it does not protect the task from the cancel we just
    issued. If the enclosing ``asyncio.wait_for`` times out it cancels THIS waiter (which
    ``asyncio.wait`` lets propagate) — NOT the underlying task, which keeps unwinding."""
    await asyncio.wait({task})


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
        rely on the concurrency slot being freed on return. Returns True ONLY when a live run was
        cancelled AND its task has finished unwinding (so the slot is provably released); False if
        there was no active run (idempotent) OR — the rare bad case — the task refused to unwind
        within ``timeout`` so we cannot honestly claim the slot was freed.

        Awaiting the task here is what makes the spec's acceptance hold deterministically: the
        semaphore slot is released as the ``CancelledError`` unwinds the ``async with`` inside the
        runner call, and a ``True`` return guarantees that has happened (bounded by ``timeout``).
        We never report success on a still-running task (no false-positive slot-release)."""
        handle = self._runs.get(session_id)
        if handle is None or handle.task.done():
            return False
        handle.cancelled = True
        log.info("run.cancel", extra={"session_id": session_id})
        return await self._cancel_handle(handle, timeout=timeout)

    @staticmethod
    async def _cancel_handle(handle: RunHandle, *, timeout: float) -> bool:
        """Cancel the task and AWAIT it directly so the slot is provably released on return.

        Returns True ONLY once the task has actually finished unwinding (``task.done()``). We do
        NOT shield the task (we WANT the cancellation to take, and shielding the cancel-awaited
        task is what made this racy) and we do NOT swallow a timeout into a false-positive: a
        ``True`` from this method is a real guarantee that the ``async with run_semaphore`` in
        ``ToolContext.run_command`` has unwound and the slot is free.

        A well-behaved turn task re-raises ``CancelledError`` from its own ``finally`` and is done
        almost immediately. If a task is misbehaving (catches the cancel, or is genuinely slow to
        unwind), we re-issue ``cancel()`` and keep awaiting in bounded slices until it is done or
        the overall budget runs out — never blocking forever, but never lying about the outcome.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(timeout, 0.0)
        # First pass uses the whole remaining budget; if the task ignored the cancel we loop and
        # re-cancel with progressively smaller slices until the deadline.
        while not handle.task.done():
            handle.task.cancel()
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                # Await the task itself (no shield): the slot is released as CancelledError
                # unwinds the `async with run_semaphore`, and `await` returns once that's done.
                await asyncio.wait_for(_await_quietly(handle.task), timeout=remaining)
            except TimeoutError:
                # The task didn't finish unwinding within the slice; loop to re-cancel/await
                # until the overall deadline. wait_for cancelled the wrapper, not handle.task.
                continue
        return handle.task.done()

    async def shutdown(self, *, timeout: float = 5.0) -> dict[str, Any]:
        """Graceful-shutdown handler (Phase 16): cancel EVERY in-flight turn so a SIGTERM
        doesn't orphan K8s Jobs / leak subprocesses. Returns a structured summary so a caller
        (or a test) can assert what happened. Plain coroutine — a test invokes it DIRECTLY; no
        OS signal required. Idempotent: a second call finds nothing left to cancel.

        Cancelling a handle AWAITS its unwind, and that await yields the event loop — so another
        coroutine can REGISTER a fresh in-flight run mid-sweep (the steer backstop in
        ``run_turn``'s ``finally`` does exactly this: when one turn is being cancelled, a sibling
        turn finishing normally spawns a follow-up turn and ``register``s it). A single snapshot
        taken once at the top would never see that late arrival, so it would survive SIGTERM and
        orphan its Job/subprocess — the very thing this handler exists to prevent. So we re-sweep:
        keep cancelling whatever is active until a pass finds nothing left. The set strictly
        shrinks each pass (a cancelled handle's task is done and won't re-appear active), and a
        backstop only re-spawns from a turn ending NORMALLY — which can't happen once every live
        turn is cancelled — so this terminates. ``_seen`` guards against re-counting a session
        whose handle was replaced mid-sweep."""
        cancelled: list[str] = []
        seen: set[int] = set()  # id() of handles already cancelled, so we don't double-count
        passes = 0
        while True:
            handles = [h for h in self.active_handles() if id(h) not in seen]
            if not handles:
                break
            passes += 1
            log.info("run.shutdown.begin", extra={"in_flight": len(handles), "pass": passes})
            for handle in handles:
                seen.add(id(handle))
                handle.cancelled = True
                await self._cancel_handle(handle, timeout=timeout)
                cancelled.append(handle.session_id)
        # Anything that completed (cancelled or on its own) during the sweep is forgotten now so a
        # re-entrant call is a clean no-op. Sweep every still-tracked entry, not just the names we
        # cancelled, so a handle replaced mid-sweep (same session id) doesn't leave a stale done
        # task behind.
        for sid, h in list(self._runs.items()):
            if h.task.done():
                self._runs.pop(sid, None)
        log.info("run.shutdown.end", extra={"cancelled": len(cancelled)})
        return {"cancelled": cancelled, "count": len(cancelled)}
