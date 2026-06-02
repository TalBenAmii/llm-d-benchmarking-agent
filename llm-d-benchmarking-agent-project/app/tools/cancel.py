"""cancel_run tool (Phase 16): cancel a still-running background benchmark/turn so it stops
holding a concurrency-cap slot and its subprocess/Job is cleaned up.

This is pure MECHANISM: it asks the in-flight-run registry (``ToolContext.runs``) to cancel a
session's turn task. Cancelling the task releases any semaphore slot it holds (asyncio unwinds
the ``async with run_semaphore`` in ``ToolContext.run_command``) and the runner reaps the child
process group — so the freed slot AND the no-orphan guarantee both fall out of the one cancel.

The JUDGMENT about *when* to cancel a run (it's clearly stuck, the user changed their mind, a
slot is needed for a more important run) lives in ``knowledge/run_lifecycle.md`` — never here.
The tool refuses to cancel the very turn it is running inside (that would deadlock), so a run is
always cancelled from OUTSIDE itself (another chat's agent, or the user's cancel control message).
"""
from __future__ import annotations

from typing import Any

from app.tools.context import ToolContext, ToolError


async def cancel_run(
    ctx: ToolContext,
    *,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Cancel the in-flight run for ``session_id`` (a chat id surfaced in /api/sessions or the
    `ready` event). Frees its concurrency slot and cleans up the subprocess. Auto-runs (it stops
    work rather than starting any mutation), and is idempotent: cancelling a session with no live
    run reports ``cancelled=False`` rather than erroring."""
    if ctx.runs is None:
        raise ToolError("run cancellation is not available in this context")
    if not session_id or not isinstance(session_id, str):
        raise ToolError("session_id is required (the chat id of the run to cancel)")
    if ctx.session_id is not None and session_id == ctx.session_id:
        # Cancelling our own turn would cancel-then-await ourselves: a deadlock. A run is always
        # cancelled from outside itself.
        raise ToolError("cannot cancel the run you are calling this from; cancel a DIFFERENT "
                        "session's run, or stop this one with the cancel control instead")
    if not ctx.runs.is_running(session_id):
        return {"session_id": session_id, "cancelled": False,
                "note": "no in-flight run for that session (it may have already finished)"}
    cancelled = await ctx.runs.cancel(session_id)
    return {
        "session_id": session_id,
        "cancelled": bool(cancelled),
        "slot_released": bool(cancelled),
        "note": "the run was cancelled; its concurrency slot is freed and its subprocess "
                "cleaned up" if cancelled else "no in-flight run to cancel",
    }
