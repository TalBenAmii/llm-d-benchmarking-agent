"""Backend-streamed live resource stats for a running benchmark — MECHANISM ONLY, ZERO LLM cost.

While a benchmark runs, this polls the cluster's live CPU/memory for the run's pods via the
allowlisted, read-only ``kubectl top`` and emits a ``resource_stats`` event the UI renders in a
single in-place panel. It never enters the LLM message stream and never calls the model, so it
adds NO tokens. It is purely an async context manager wrapped around the run: enter to start
polling, exit to stop. No-op in simulate mode and when no emitter is wired (e.g. a bare unit
test or a non-UI caller). Best-effort throughout — a failing poll never breaks the benchmark.
"""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from app.orchestrator.job import LABEL_RUN
from app.tools.context import ToolContext
from app.tools.observe import _parse_top_table

# The event the poller streams (kept as a literal so this module stays leaf-level and free of an
# import cycle through app.agent.events; the UI and tests match the same string).
RESOURCE_STATS = "resource_stats"


@contextlib.asynccontextmanager
async def resource_stats_poller(
    ctx: ToolContext, *, namespace: str, run_id: str | None = None, interval: float = 5.0
) -> AsyncIterator[None]:
    """Stream live ``resource_stats`` for ``namespace`` (optionally scoped to one ``run_id``)
    for the duration of the ``async with`` block. No-op when no emitter is wired or in simulate
    mode, so callers can wrap unconditionally."""
    if ctx.emit is None or ctx.settings.simulate:
        yield
        return
    task = asyncio.create_task(_poll_loop(ctx, namespace, run_id, interval))
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


async def _poll_loop(
    ctx: ToolContext, namespace: str, run_id: str | None, interval: float
) -> None:
    # The context manager only spawns this task when ctx.emit is wired, so capture it once as a
    # non-None local (also keeps the calls below stable if ctx.emit is reassigned mid-run).
    emit = ctx.emit
    if emit is None:
        return
    announced_unavailable = False
    while True:
        try:
            argv = ["kubectl", "top", "pods", "-n", namespace]
            if run_id:
                argv += ["-l", f"{LABEL_RUN}={run_id}"]
            res = await ctx.run_readonly(argv, timeout=15.0, quiet=True)
            if res.exit_code != 0:
                # Metrics-server absent / not ready: say so ONCE, then stay silent (don't flood
                # the panel every tick). The agent and UI both treat this as a soft, read-only
                # "no live stats here" — nothing is wrong with the run itself.
                if not announced_unavailable:
                    announced_unavailable = True
                    await emit(RESOURCE_STATS, {
                        "available": False,
                        "note": "live resource stats unavailable (no metrics-server)",
                    })
            else:
                rows = _parse_top_table(res.output)
                announced_unavailable = False  # a good sample re-arms the one-shot note
                await emit(RESOURCE_STATS, {
                    "available": True, "namespace": namespace, "rows": rows,
                })
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a stat poll must NEVER kill the run
            pass
        await asyncio.sleep(interval)
