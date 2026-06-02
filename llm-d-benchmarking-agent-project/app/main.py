"""FastAPI app: serves the chat UI and hosts the agent over a WebSocket.

The backend is the security + secrets boundary. The browser only ever exchanges chat
text, structured events, and Approve/Reject decisions — never API keys, never raw commands.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import signal
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app.agent import events as ws_events
from app.agent.channel import Channel
from app.agent.lifecycle import RunRegistry
from app.agent.loop import AgentLoop
from app.agent.session import SessionManager
from app.agent.suggestions import load_suggestions
from app.agent.ws_schemas import (
    ApprovalIn,
    CancelIn,
    PingIn,
    UserMessageIn,
    ValidationError,
    outbound,
    parse_inbound,
)
from app.config import get_settings
from app.llm.provider import get_provider
from app.observability import instrument
from app.observability.logctx import bind as log_bind
from app.observability.logctx import new_corr_id
from app.observability.logging import setup_logging
from app.observability.metrics import render_prometheus
from app.security.allowlist import Allowlist
from app.security.auth import RateLimiter, check_http_auth, rate_limit, websocket_authorized
from app.security.runner import CommandRunner, SimRunner
from app.storage.history import HistoryStore, available_metrics, trend
from app.storage.retention import readiness, run_gc, self_check
from app.tools.probe import probe_environment

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
    runner_cls = SimRunner if settings.simulate else CommandRunner
    app.state.runner = runner_cls(settings.repo_paths, extra_env=settings.extra_subprocess_env)
    # Cross-session cap on concurrent heavy runs (None = unlimited).
    app.state.run_semaphore = (
        asyncio.Semaphore(settings.max_concurrent_runs) if settings.max_concurrent_runs > 0 else None
    )
    # In-flight turns kept alive after their socket drops (background benchmark runs), and
    # the turn currently running per session (prevents two connections double-running one chat).
    app.state.background_tasks = set()
    app.state.running = {}
    # Run lifecycle (Phase 16): the registry of in-flight turn tasks the cancel tool / cancel
    # control message / graceful-shutdown handler operate on. Cancelling a registered task frees
    # the concurrency slot it holds and reaps its subprocess (no orphaned Jobs/subprocesses).
    app.state.runs = RunRegistry()
    # Per-session Channel: routes a running turn's events + approval gates to whatever socket
    # is currently attached, so a turn (incl. one parked at an approval) survives reconnects.
    app.state.channels = {}
    app.state.sessions = SessionManager(
        settings, app.state.allowlist, app.state.runner,
        run_semaphore=app.state.run_semaphore, runs=app.state.runs,
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
    # Graceful shutdown (Phase 16): on SIGTERM (the orchestrator's stop signal) cancel every
    # in-flight turn so we don't orphan K8s Jobs / leak subprocesses. We register an asyncio
    # signal handler that schedules the SAME shutdown coroutine the lifespan teardown runs (and
    # which a test can call directly — see graceful_shutdown). Best-effort: a loop that doesn't
    # support add_signal_handler (e.g. on Windows / inside some test harnesses) is tolerated.
    with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(
            signal.SIGTERM, lambda: loop.create_task(graceful_shutdown(app))
        )
    try:
        yield
    finally:
        # Lifespan teardown (uvicorn's normal stop path also lands here): cancel any in-flight
        # turns so nothing is orphaned even when SIGTERM wasn't the trigger.
        with contextlib.suppress(Exception):
            await graceful_shutdown(app)


async def graceful_shutdown(app: FastAPI) -> dict[str, Any]:
    """Cancel all in-flight runs on shutdown (Phase 16). A PLAIN coroutine so a test can invoke
    it DIRECTLY — no real OS signal required. Cancelling each run frees its concurrency slot and
    reaps its subprocess, so a SIGTERM stops the server WITHOUT orphaning Jobs/subprocesses.
    Returns the structured summary from the registry (which sessions were cancelled)."""
    runs: RunRegistry | None = getattr(app.state, "runs", None)
    if runs is None:
        return {"cancelled": [], "count": 0}
    summary = await runs.shutdown()
    log.info("shutdown.runs_cancelled", extra={"count": summary["count"]})
    return summary


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


# Auth (Phase 12) guards HTTP routes via an app-level dependency: a single registration
# point (thin code) rather than per-route annotations that could be forgotten. It's a no-op
# when AUTH_ENABLED is False (the default), so the API is open exactly as today. The
# liveness/readiness probes (/healthz, /readyz) are exempted inside check_http_auth so K8s
# probes — which can't carry a Bearer token — are never locked out.
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
    """Liveness probe (Phase 16): keep this MINIMAL — it answers only 'is the process up and
    serving?'. It must NOT depend on the repos, the provider, or any heavy work (a liveness probe
    that fails on a degraded-but-alive dependency would get the pod needlessly restarted). All
    per-component readiness (provider/repos/runner/workspace) lives on /readyz."""
    return JSONResponse({"ok": True})


@app.get("/readyz")
async def readyz() -> JSONResponse:
    """Readiness probe: reports per-component readiness from the startup configuration self-check
    — workspace writable, provider configured, repos present, runner ok (the allowlist policy
    loads), auth coherent (Phase 16 splits this from /healthz liveness and adds the runner_ok
    component). Returns 200 when ready, 503 when not, with the STRUCTURED self-check reasons so an
    operator/orchestrator can see *why*. Liveness stays on the minimal /healthz; this is the
    readiness gate a K8s readinessProbe / load balancer should poll."""
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


def _teardown_session_runtime(sid: str) -> None:
    """Tear down a deleted session's live turn (e.g. one parked at an approval gate) + its
    channel so deleting it doesn't leak a forever-blocked task. Cancelling via the registry
    also frees any concurrency slot the turn holds and reaps its subprocess (Phase 16)."""
    task = app.state.running.pop(sid, None)
    if task is not None:
        if not task.done():
            task.cancel()
        app.state.runs.forget(sid, task)
    app.state.channels.pop(sid, None)


@app.delete("/api/sessions/{sid}", dependencies=[Depends(rate_limit)])
async def delete_session(sid: str) -> JSONResponse:
    if not app.state.sessions.delete(sid):
        raise HTTPException(status_code=404, detail="session not found")
    _teardown_session_runtime(sid)
    return JSONResponse({"deleted": True, "id": sid})


@app.delete("/api/namespaces/{namespace}", dependencies=[Depends(rate_limit)])
async def delete_namespace(namespace: str) -> JSONResponse:
    """Delete a whole sidebar folder — every chat in one namespace at once. The literal
    ``no_namespace`` sentinel removes the chats that have no namespace set."""
    deleted = app.state.sessions.delete_namespace(namespace)
    if not deleted:
        raise HTTPException(status_code=404, detail="no chats in that namespace")
    for sid in deleted:
        _teardown_session_runtime(sid)
    return JSONResponse({"deleted": deleted, "count": len(deleted)})


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


# Per-run chart images (e.g. the latency/throughput PNGs inference-perf renders into a
# session's analysis/ dir) live under the gitignored workspace, which the /static mount does
# NOT serve. This read-only route exposes them so the UI can show a run's charts inline next to
# its summary. Hardened: image suffixes only, and the resolved path must stay INSIDE the named
# session dir (defeats ../ traversal). Auth-gated by the app-level dependency; rate-limited like
# the rest of /api. The chart paths come from locate_and_parse_report's `charts` field.
_ARTIFACT_SUFFIXES = frozenset({".png", ".svg", ".jpg", ".jpeg", ".webp"})
_ARTIFACT_MEDIA = {
    ".png": "image/png", ".svg": "image/svg+xml", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".webp": "image/webp",
}


@app.get("/api/sessions/{sid}/artifact", dependencies=[Depends(rate_limit)])
async def session_artifact(sid: str, path: str) -> FileResponse:
    """Serve one image artifact from a session's workspace dir (read-only, image-only)."""
    sessions_root = (get_settings().resolved_workspace_dir / "sessions").resolve()
    base = (sessions_root / sid).resolve()
    candidate = (base / path).resolve()
    # `base` must be a real session dir directly under sessions_root, and `candidate` must not
    # escape it — together these reject ../ traversal in either `sid` or `path`.
    if base.parent != sessions_root or not base.is_dir() or not candidate.is_relative_to(base):
        raise HTTPException(status_code=404, detail="artifact not found")
    suffix = candidate.suffix.lower()
    if suffix not in _ARTIFACT_SUFFIXES or not candidate.is_file():
        raise HTTPException(status_code=404, detail="artifact not found")
    return FileResponse(candidate, media_type=_ARTIFACT_MEDIA[suffix])


app.mount("/static", StaticFiles(directory=str(get_settings().ui_dir)), name="static")


def _first_validation_message(exc: ValidationError) -> str:
    """A short, human-readable reason from a Pydantic validation error for the protocol
    `error` event — the field path + message of the first error, without leaking internals."""
    errs = exc.errors()
    if not errs:
        return "invalid frame"
    e = errs[0]
    loc = ".".join(str(p) for p in e.get("loc", ())) or "frame"
    return f"{loc}: {e.get('msg', 'invalid')}"


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
        # Open a fresh live-event buffer for this turn (Phase 15): every event emitted below is
        # appended to the channel's bounded ring buffer so a client that reconnects mid-turn can
        # replay what it missed and catch up to the LIVE stream.
        channel.begin_turn()
        try:
            if loop is None:
                # No LLM, but still record the turn so the chat persists / resumes.
                session.messages.append({"role": "user", "content": text})
                session.persist()
                await channel.emit("error", {"message": f"LLM provider not configured: {app.state.provider_error}"})
                await channel.emit("done", {})
                return
            await loop.run_turn(session, text, emit=channel.emit, request_approval=channel.request_approval)
        except asyncio.CancelledError:
            # The turn was cancelled (Phase 16: cancel tool / control message / graceful
            # shutdown). The concurrency slot is already released as this unwinds the runner's
            # `async with run_semaphore`. Announce it (best-effort — a socket may be attached)
            # and persist so a resumed chat shows the run ended, then re-raise so the task is
            # marked cancelled and the registry's awaiter returns.
            with contextlib.suppress(Exception):
                session.persist()
                await channel.emit("cancelled", {"message": "run cancelled"})
                await channel.emit("done", {})
            raise
        except Exception as exc:  # noqa: BLE001
            await channel.emit("error", {"message": f"agent error: {exc}"})
            await channel.emit("done", {})
        finally:
            channel.end_turn()
            busy["value"] = False
            if app.state.running.get(session.id) is asyncio.current_task():
                app.state.running.pop(session.id, None)
            # Deregister this turn from the lifecycle registry (Phase 16) on normal completion;
            # forget() is a no-op if a newer turn already replaced this session's handle.
            app.state.runs.forget(session.id, asyncio.current_task())
            # Forget the channel once its turn ends with nothing attached/pending, so a long-
            # lived server doesn't accumulate one channel per chat ever opened.
            if channel.ws is None and not channel.pending:
                app.state.channels.pop(session.id, None)

    async def _prewarm_env(s, ch) -> None:
        """Background read-only environment pre-probe for a brand-new chat (W2).

        Runs the same read-only probe the agent would otherwise call on its first turn, BEFORE
        the user even sends a message, so the first turn starts environment-aware without an
        extra LLM round-trip (loop.py injects the snapshot as a synthetic message). Wiring the
        probe's auto-run `command` events through the channel keeps them in the executed-command
        trail. Best-effort: any failure is swallowed (the agent just probes itself next turn)."""
        try:
            s.ctx.emit = ch.emit
            s.env_snapshot = await probe_environment(s.ctx, checks=[
                "container_runtime", "repos", "tools", "venv",
                "kind_clusters", "kube_context", "cluster_info", "namespaces",
            ])
        except Exception:  # noqa: BLE001 — pre-probe is best-effort; never break connect
            pass

    _running_task = app.state.running.get(session.id)
    await channel.emit("ready", {
        "session_id": session.id,
        "resumed": resumed,
        "running": bool(_running_task and not _running_task.done()),
        # Persisted token tally so the header chip is correct immediately on connect/reload,
        # before any new turn (token-tracking feature).
        "usage": {
            "input": session.total_input_tokens,
            "output": session.total_output_tokens,
            "cache_read": session.total_cache_read_tokens,
            "total": session.session_total,
        },
    })
    if resumed:
        await channel.emit("history", {"items": _history_items(session), "commands": session.commands})
    else:
        # Brand-new chat: surface the start-of-chat suggestion chips (W1) right after `ready`, and
        # kick off a NON-blocking read-only environment pre-probe (W2) so the first turn starts
        # environment-aware without an extra LLM round-trip. Neither blocks input-enable: the
        # probe runs in the background and its snapshot is consumed on the first turn if ready.
        chips = load_suggestions(get_settings())
        if chips:
            await channel.emit(ws_events.SUGGESTIONS, {"chips": chips})
        if loop is not None:
            asyncio.create_task(_prewarm_env(session, channel))
    # If a turn is still running on this session (the socket dropped mid-run), replay the
    # buffered LIVE events for that turn so a reconnecting client catches up to the live stream
    # — the events it missed, in order — then continues live, rather than waiting blind for the
    # final result (resolves the Phase-2 deferral). AFTER history so it lands below the replayed
    # transcript. Guarded by turn_active so we never replay a stale prior turn's tail.
    if channel.turn_active:
        await channel.replay_live()
    # Re-surface any still-undecided approval (a gate the turn parked on while you were away),
    # AFTER the live replay so the live card lands at the bottom.
    if channel.pending:
        await channel.reemit_pending()
    turn_task: asyncio.Task | None = None

    try:
        while True:
            # Decode the inbound frame ourselves (rather than websocket.receive_json) so we can
            # guard the JSON-decode layer too: a non-JSON text frame, or a binary frame in a
            # text protocol, must be rejected with a structured `error` and the socket KEPT
            # ALIVE — never crash the handler. receive_json() would raise json.JSONDecodeError
            # (or KeyError on a binary frame) straight out of the loop and tear down the socket.
            message = await websocket.receive()
            # A control frame announcing disconnect surfaces here as a websocket.disconnect
            # message; raise so the outer `except WebSocketDisconnect` handles teardown.
            if message["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(message.get("code", 1000), message.get("reason"))
            text = message.get("text")
            if text is None:
                # No text payload (e.g. a binary frame): can't be a protocol frame.
                await websocket.send_json(outbound(ws_events.ERROR, {
                    "message": "malformed message: expected a JSON text frame",
                    "kind": "protocol_error",
                }))
                continue
            try:
                raw = json.loads(text)
            except (json.JSONDecodeError, ValueError) as exc:
                # Non-JSON text frame — the most basic malformed frame. Reject structurally and
                # keep the connection alive so a hostile/buggy client cannot crash the handler.
                await websocket.send_json(outbound(ws_events.ERROR, {
                    "message": "malformed message: invalid JSON: " + str(exc),
                    "kind": "protocol_error",
                }))
                continue
            # Validate the inbound frame against the WS wire protocol (Phase 15). A malformed
            # frame (non-dict, unknown/missing type, wrong field shape, extra fields) is
            # rejected with a structured `error` event and the socket is KEPT ALIVE — a bad or
            # hostile frame must never crash the handler or silently no-op. The validated,
            # typed message then drives the handler below.
            try:
                msg = parse_inbound(raw)
            except ValidationError as exc:
                # Send a structured, client-actionable error; do NOT close the connection.
                await websocket.send_json(outbound(ws_events.ERROR, {
                    "message": "malformed message: " + _first_validation_message(exc),
                    "kind": "protocol_error",
                }))
                continue
            if isinstance(msg, UserMessageIn):
                existing = app.state.running.get(session.id)
                if busy["value"] or (existing is not None and not existing.done()):
                    await channel.emit("error", {"message": "still working on the previous request — please wait."})
                    continue
                # Mint a fresh correlation id at the WS boundary (one per connection/turn) and
                # bind it before creating the turn task: asyncio.create_task snapshots the
                # current contextvars, so the corr_id + session_id ride into the loop, every
                # tool dispatch, and the command runner automatically (Phase 11).
                with log_bind(corr_id=new_corr_id(), session_id=session.id):
                    turn_task = asyncio.create_task(run_turn(msg.text))
                app.state.running[session.id] = turn_task
                # Register the turn in the lifecycle registry (Phase 16) so it can be cancelled
                # (freeing its concurrency slot) by the cancel tool / cancel message / shutdown.
                app.state.runs.register(session.id, turn_task)
            elif isinstance(msg, ApprovalIn):
                channel.resolve(msg.request_id, msg.approved)
            elif isinstance(msg, CancelIn):
                # Cancel THIS chat's own in-flight run from the client (the user clicked Stop).
                # Frees the concurrency slot + reaps the subprocess. Idempotent — a no-op if
                # nothing is running. The turn's own `cancelled`/`done` events announce the stop.
                await app.state.runs.cancel(session.id)
            elif isinstance(msg, PingIn):
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
