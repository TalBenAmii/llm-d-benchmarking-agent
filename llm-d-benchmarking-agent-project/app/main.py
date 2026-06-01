"""FastAPI app: serves the chat UI and hosts the agent over a WebSocket.

The backend is the security + secrets boundary. The browser only ever exchanges chat
text, structured events, and Approve/Reject decisions — never API keys, never raw commands.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app.agent.channel import Channel
from app.agent.loop import AgentLoop
from app.agent.session import SessionManager
from app.config import get_settings
from app.security.auth import RateLimiter, check_http_auth, rate_limit, websocket_authorized
from app.llm.provider import get_provider
from app.observability import instrument
from app.observability.logctx import bind as log_bind
from app.observability.logctx import new_corr_id
from app.observability.logging import setup_logging
from app.observability.metrics import render_prometheus
from app.security.allowlist import Allowlist
from app.security.runner import CommandRunner
from app.storage.history import HistoryStore, available_metrics, trend
from app.storage.retention import readiness, run_gc, self_check

# Prometheus text exposition content type (v0.0.4); scrapers and Grafana expect exactly this.
_PROM_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

log = logging.getLogger("app.main")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # Initialize structured logging ONCE, before anything downstream logs (Phase 11).
    setup_logging(level=settings.log_level, log_format=settings.log_format)
    log.info("startup", extra={"log_format": settings.log_format, "provider": settings.llm_provider})
    app.state.settings = settings
    # API trust (Phase 12): a misconfigured auth toggle (enabled but no token) would silently
    # reject every request — fail loudly at startup instead, per the project's fail-loud rule.
    if settings.auth_enabled and not settings.auth_token:
        raise RuntimeError("AUTH_ENABLED is set but AUTH_TOKEN is empty — refusing to start")
    # Build the rate limiter ONCE (shared process-wide bucket) so the per-request dependency
    # doesn't reconstruct it. Off by default -> a no-op limiter.
    app.state.rate_limiter = RateLimiter.from_settings(settings)
    if settings.auth_enabled:
        log.info("auth.enabled")
    if settings.rate_limit_enabled:
        log.info("ratelimit.enabled", extra={"rps": settings.rate_limit_rps, "burst": settings.rate_limit_burst})
    app.state.allowlist = Allowlist.from_file(settings.allowlist_path)
    app.state.runner = CommandRunner(settings.repo_paths, extra_env=settings.extra_subprocess_env)
    # Cross-session cap on concurrent heavy runs (None = unlimited).
    app.state.run_semaphore = (
        asyncio.Semaphore(settings.max_concurrent_runs) if settings.max_concurrent_runs > 0 else None
    )
    # In-flight turns kept alive after their socket drops (background benchmark runs), and
    # the turn currently running per session (prevents two connections double-running one chat).
    app.state.background_tasks = set()
    app.state.running = {}
    # Per-session Channel: routes a running turn's events + approval gates to whatever socket
    # is currently attached, so a turn (incl. one parked at an approval) survives reconnects.
    app.state.channels = {}
    app.state.sessions = SessionManager(
        settings, app.state.allowlist, app.state.runner, run_semaphore=app.state.run_semaphore
    )
    # Build the provider tolerantly: a missing key shouldn't crash the server.
    try:
        app.state.provider = get_provider(settings)
        app.state.provider_error = None
    except Exception as exc:  # noqa: BLE001
        app.state.provider = None
        app.state.provider_error = str(exc)
    # Workspace lifecycle (Phase 18): run the startup configuration self-check (structured
    # pass/fail folded into /readyz) and a one-shot retention GC over scratch. Both honor their
    # toggles + the DATA caps; GC never prunes a session that is currently live/running.
    app.state.self_check = self_check(settings)
    if not app.state.self_check.ok:
        log.warning("selfcheck.failed", extra={"reasons": app.state.self_check.reasons})
    if settings.retention_gc_on_startup:
        try:
            gc = run_gc(settings, active_session_ids=_active_session_ids(app))
            log.info("retention.gc", extra={
                "removed": gc.total_removed, "reclaimed_bytes": gc.total_reclaimed_bytes,
            })
        except Exception as exc:  # noqa: BLE001 — GC must never block startup
            log.warning("retention.gc.failed", extra={"error": str(exc)})
    yield


def _active_session_ids(app: FastAPI) -> set[str]:
    """Sessions the retention GC must NOT prune: any held in memory by the SessionManager plus
    any with a turn currently running (background benchmark) — the active-run safety (Phase 18)."""
    sessions = getattr(app.state, "sessions", None)
    ids: set[str] = sessions.active_ids() if sessions is not None else set()
    ids |= set(getattr(app.state, "running", {}) or {})
    return ids


def install_cors(target: FastAPI, origins: list[str]) -> None:
    """Wire CORS (Phase 12) onto ``target`` — but ONLY when ``origins`` is non-empty, so the
    default (empty CORS_ALLOW_ORIGINS) keeps today's behavior: no CORS middleware, no CORS
    headers on responses. Factored out so the wiring can be exercised on a throwaway app in a
    test without reloading this module (a reload would rebind the shared ``app`` and leak)."""
    if origins:
        target.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )


# Auth (Phase 12) guards EVERY HTTP route via an app-level dependency: a single registration
# point (thin code) rather than per-route annotations that could be forgotten. It's a no-op
# when AUTH_ENABLED is False (the default), so the API is open exactly as today.
app = FastAPI(
    title="llm-d Benchmarking Assistant",
    lifespan=lifespan,
    dependencies=[Depends(check_http_auth)],
)
install_cors(app, get_settings().cors_origins_list)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(get_settings().ui_dir / "index.html")


@app.get("/healthz")
async def healthz() -> JSONResponse:
    s = get_settings()
    cat = app.state.sessions.create().ctx.catalog()  # cheap; reflects live repo
    return JSONResponse({
        "ok": True,
        "provider": s.llm_provider,
        "provider_ready": app.state.provider is not None,
        "provider_error": app.state.provider_error,
        "repos_present": cat.get("present"),
        "specs": cat.get("specs", [])[:5],
    })


@app.get("/readyz")
async def readyz() -> JSONResponse:
    """Readiness probe (Phase 16 seam): reports whether the startup configuration self-check
    passed (workspace writable, provider coherent, repos resolvable, auth coherent). Returns 200
    when ready, 503 when not, with the STRUCTURED self-check reasons so an operator/orchestrator
    can see *why*. Phase 16 (if/when it lands its own /readyz) can compose this contribution.
    Liveness stays on /healthz; this is the readiness gate."""
    contrib = readiness(get_settings())
    code = status.HTTP_200_OK if contrib.get("ready") else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(contrib, status_code=code)


@app.get("/metrics")
async def metrics() -> PlainTextResponse:
    """Prometheus scrape endpoint: agent + orchestrator metrics in the text exposition format.
    Point a Prometheus scrape at this path (see deploy/observability/prometheus-scrape.yaml)
    and import deploy/observability/grafana-dashboard.json to visualize them."""
    return PlainTextResponse(render_prometheus(instrument.REGISTRY), media_type=_PROM_CONTENT_TYPE)


# The /api/* routes are the HTTP message-intake surface the browser drives, so the rate
# limiter (Phase 12) guards them: an empty bucket -> 429. /healthz + /metrics are deliberately
# NOT throttled (liveness probes / Prometheus scrapes must never be rate-limited away). The
# limiter is a no-op when RATE_LIMIT_ENABLED is False (the default).
@app.get("/api/sessions", dependencies=[Depends(rate_limit)])
async def list_sessions() -> JSONResponse:
    """Recent chats for the sidebar (summaries only, newest first)."""
    return JSONResponse({"sessions": app.state.sessions.list()})


@app.delete("/api/sessions/{sid}", dependencies=[Depends(rate_limit)])
async def delete_session(sid: str) -> JSONResponse:
    if not app.state.sessions.delete(sid):
        raise HTTPException(status_code=404, detail="session not found")
    # Tear down any live turn (e.g. one parked at an approval gate) + its channel so deleting
    # a session doesn't leak a forever-blocked task.
    task = app.state.running.pop(sid, None)
    if task is not None and not task.done():
        task.cancel()
    app.state.channels.pop(sid, None)
    return JSONResponse({"deleted": True, "id": sid})


def _history_store() -> HistoryStore:
    """The cross-session result store, rooted at the same shared workspace the agent's
    ``result_history`` tool writes to (so the UI browser sees what the agent stored)."""
    return HistoryStore(get_settings().resolved_workspace_dir)


def _history_record_view(rec) -> dict[str, Any]:
    return {
        "id": rec.id, "stored_at": rec.stored_at, "label": rec.label, "tags": rec.tags,
        "model": rec.model, "run_uid": rec.run_uid, "spec": rec.spec,
        "harness": rec.harness, "workload": rec.workload, "namespace": rec.namespace,
    }


@app.get("/api/history", dependencies=[Depends(rate_limit)])
async def list_history(tag: str | None = None, model: str | None = None) -> JSONResponse:
    """Stored historical results for the results-browser (newest first, summaries only)."""
    records = _history_store().list(tag=tag, model=model)
    return JSONResponse({
        "records": [_history_record_view(r) for r in records],
        "metrics": available_metrics(),
    })


@app.get("/api/history/trend", dependencies=[Depends(rate_limit)])
async def history_trend(metric: str, tag: str | None = None, model: str | None = None) -> JSONResponse:
    """Time-series of one metric across stored results, for the trends view. Facts only —
    the value series + the metric's better-direction; no regression verdict (that's the agent)."""
    records = _history_store().list(tag=tag, model=model)
    return JSONResponse(trend(records, metric))


def _history_items(session) -> list[dict[str, Any]]:
    """Render-friendly transcript for replaying a resumed chat in the UI.

    The stored ``messages`` are in LLM wire-format; flatten them into the same
    shape the live event stream produces so the client can reuse its renderers.
    Decided approval gates (kept off the LLM stream, in ``session.approvals``) are
    interleaved right after the tool call they belong to, so the resolved ✓/✗ cards
    show up in their original place.
    """
    approvals_by_tc: dict[str, list[dict[str, Any]]] = {}
    for a in getattr(session, "approvals", []) or []:
        approvals_by_tc.setdefault(a.get("tool_call_id"), []).append(a)

    items: list[dict[str, Any]] = []
    for m in session.messages:
        role = m.get("role")
        if role == "user":
            items.append({"role": "user", "text": m.get("content") or ""})
        elif role == "assistant":
            if m.get("content"):
                items.append({"role": "assistant", "text": m["content"]})
            for tc in m.get("tool_calls") or []:
                items.append({"role": "tool_call", "name": tc.get("name"), "input": tc.get("input")})
                for a in approvals_by_tc.get(tc.get("id"), []):
                    items.append({"role": "approval_decision", "kind": a.get("kind"),
                                  "payload": a.get("payload"), "approved": a.get("approved")})
    return items


app.mount("/static", StaticFiles(directory=str(get_settings().ui_dir)), name="static")


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    # Auth (Phase 12): the app-level HTTP dependency does NOT cover WebSockets, so guard the
    # handshake here. Accept first so the client receives a clean 1008 (policy-violation) close
    # rather than a bare network drop; closed immediately when the token is missing/bad. No-op
    # when AUTH_ENABLED is False (the default) -> /ws is open exactly as today.
    await websocket.accept()
    if not websocket_authorized(websocket, get_settings()):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    # ``/ws?session=<id>`` reattaches to a saved chat (page reload / sidebar click);
    # an unknown or missing id just mints a fresh session.
    requested = websocket.query_params.get("session")
    session = app.state.sessions.get_or_load(requested) if requested else None
    resumed = session is not None
    if session is None:
        session = app.state.sessions.create()
    loop = AgentLoop(app.state.provider) if app.state.provider else None

    # A per-session Channel decouples the running turn from this specific socket: events go to
    # whatever socket is currently attached, and a turn parked at an approval gate stays parked
    # (rather than auto-rejecting) when the socket drops — re-surfacing when the chat reopens.
    channel = app.state.channels.get(session.id)
    if channel is None:
        channel = Channel(session)
        app.state.channels[session.id] = channel
    channel.ws = websocket  # last connection wins (single active tab, Claude-web style)

    busy = {"value": False}

    async def run_turn(text: str) -> None:
        busy["value"] = True
        try:
            if loop is None:
                # No LLM, but still record the turn so the chat persists / resumes.
                session.messages.append({"role": "user", "content": text})
                session.persist()
                await channel.emit("error", {"message": f"LLM provider not configured: {app.state.provider_error}"})
                await channel.emit("done", {})
                return
            await loop.run_turn(session, text, emit=channel.emit, request_approval=channel.request_approval)
        except Exception as exc:  # noqa: BLE001
            await channel.emit("error", {"message": f"agent error: {exc}"})
            await channel.emit("done", {})
        finally:
            busy["value"] = False
            if app.state.running.get(session.id) is asyncio.current_task():
                app.state.running.pop(session.id, None)
            # Forget the channel once its turn ends with nothing attached/pending, so a long-
            # lived server doesn't accumulate one channel per chat ever opened.
            if channel.ws is None and not channel.pending:
                app.state.channels.pop(session.id, None)

    _running_task = app.state.running.get(session.id)
    await channel.emit("ready", {
        "session_id": session.id,
        "resumed": resumed,
        "running": bool(_running_task and not _running_task.done()),
    })
    if resumed:
        await channel.emit("history", {"items": _history_items(session), "commands": session.commands})
    # Re-surface any still-undecided approval (a gate the turn parked on while you were away),
    # AFTER history so the live card lands below the replayed transcript.
    if channel.pending:
        await channel.reemit_pending()
    turn_task: asyncio.Task | None = None

    try:
        while True:
            msg = await websocket.receive_json()
            mtype = msg.get("type")
            if mtype == "user_message":
                existing = app.state.running.get(session.id)
                if busy["value"] or (existing is not None and not existing.done()):
                    await channel.emit("error", {"message": "still working on the previous request — please wait."})
                    continue
                # Mint a fresh correlation id at the WS boundary (one per connection/turn) and
                # bind it before creating the turn task: asyncio.create_task snapshots the
                # current contextvars, so the corr_id + session_id ride into the loop, every
                # tool dispatch, and the command runner automatically (Phase 11).
                with log_bind(corr_id=new_corr_id(), session_id=session.id):
                    turn_task = asyncio.create_task(run_turn(msg.get("text", "")))
                app.state.running[session.id] = turn_task
            elif mtype == "approval":
                channel.resolve(msg.get("request_id"), bool(msg.get("approved")))
            elif mtype == "ping":
                await channel.emit("pong", {})
    except WebSocketDisconnect:
        pass
    finally:
        # Detach this socket unless a newer connection already took over — the identity guard
        # is what makes the close->reconnect race safe. Pending approvals are NOT rejected: the
        # turn stays parked and re-surfaces when the chat is reopened.
        if channel.ws is websocket:
            channel.ws = None
        # Don't kill an in-flight (already-approved) run on disconnect — let it finish in the
        # background so a benchmark survives navigating away and several can run in parallel
        # across chats. A turn parked at an approval gate likewise survives (it holds no
        # concurrency slot) and resumes when you reopen the chat.
        if turn_task and not turn_task.done():
            app.state.background_tasks.add(turn_task)
            turn_task.add_done_callback(app.state.background_tasks.discard)
        running = app.state.running.get(session.id)
        if channel.ws is None and not channel.pending and (running is None or running.done()):
            app.state.channels.pop(session.id, None)
