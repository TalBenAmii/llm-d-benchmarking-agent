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
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from app.agent import events as ws_events
from app.agent.channel import Channel
from app.agent.lifecycle import RunRegistry
from app.agent.loop import CARD_RESULT_TOOLS, AgentLoop
from app.agent.results_card import build_results_card
from app.agent.session import SessionManager
from app.agent.suggestions import load_suggestions
from app.agent.welcome import build_welcome
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
from app.orchestrator.controller import BenchmarkOrchestrator
from app.orchestrator.kube import RealKubeClient
from app.packaging import gist_publish
from app.packaging.report_card import render_report_card
from app.packaging.shared_chat import render_shared_chat
from app.security.allowlist import Allowlist
from app.security.auth import RateLimiter, check_http_auth, rate_limit, websocket_authorized
from app.security.runner import CommandRunner, SimRunner
from app.storage.history import HistoryStore, available_metrics, trend
from app.storage.provenance import BundleStore
from app.storage.retention import readiness, run_gc, self_check
from app.storage.share import ShareStore, is_valid_token
from app.tools import report_locate
from app.tools.context import ToolContext
from app.tools.json_tail import find_last_json
from app.tools.manage_runs import serialize_status
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
    # Unrestricted-tools mode (opt-in): the `run_shell` tool runs arbitrary `bash -lc` commands,
    # bypassing the allowlist (human approval is still enforced for mutating/unknown commands).
    # Announce it loudly at startup so it can never be on by accident.
    if settings.unrestricted_tools:
        log.warning(
            "unrestricted_tools.enabled — the command allowlist is BYPASSED for the run_shell "
            "tool (arbitrary `bash -lc`); mutating/unknown commands still require user approval"
        )
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
    # Cancel the background tasks NOT held in the run registry: the read-only environment
    # pre-probe (`_prewarm_env`, BUG-033) kicked off on a brand-new chat connect. It runs real
    # subprocesses (`kubectl get nodes`, `kind get clusters`, …) via the runner but is tracked
    # only in `background_tasks`, never `runs.register` — so `runs.shutdown()` above does NOT
    # reach it. Left uncancelled, a SIGTERM that lands mid-probe ORPHANS its child process group
    # (the very leak this handler exists to prevent). Cancelling unwinds the runner's
    # `CancelledError` path, which SIGKILLs the child's process group, and we AWAIT the unwind so
    # shutdown doesn't return before the reap completes. Done BEFORE the provider close below —
    # the pre-probe uses the runner, not the provider, so order vs. aclose() is independent, but
    # we keep all subprocess-reaping cancellation in one phase ahead of connection teardown.
    background = getattr(app.state, "background_tasks", None)
    pending = [t for t in list(background) if not t.done()] if background else []
    if pending:
        for t in pending:
            t.cancel()
        log.info("shutdown.background_cancelled", extra={"count": len(pending)})
        # Absorb each task's outcome (the expected CancelledError, or any error it raised) so a
        # misbehaving probe can't abort the rest of teardown — the provider close below must run.
        with contextlib.suppress(Exception):
            await asyncio.gather(*pending, return_exceptions=True)
    # Disconnect any prewarmed spare LLM connection (the Agent SDK provider keeps one warm
    # subprocess for the next turn) so SIGTERM leaves nothing connected. Best-effort + duck-typed:
    # only the Agent SDK provider implements aclose(); other providers have nothing to close.
    provider = getattr(app.state, "provider", None)
    closer = getattr(provider, "aclose", None)
    if closer is not None:
        with contextlib.suppress(Exception):
            await closer()
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


@app.get("/api/jobs", dependencies=[Depends(rate_limit)])
async def list_orchestrated_jobs(
    namespace: str, session_id: str | None = None, sweep_id: str | None = None,
) -> JSONResponse:
    """Read-only REST mirror of the orchestrator's run state for non-chat clients (proposal G1):
    list the agent-managed benchmark Jobs in ``namespace``, classified FRESH from the cluster
    (``BenchmarkOrchestrator.reconstruct`` — the cluster is the source of truth, nothing is held
    locally). This is the SAME state ``manage_orchestrated_runs(action='list')`` returns, exposed
    as a plain HTTP GET so a programmatic client can poll run state without driving the chat LLM.

    READ-ONLY by design: it runs an allowlisted ``kubectl get jobs`` and mutates nothing —
    submitting and stopping runs stay approval-gated through the chat tool, keeping the
    agent-first surface intact. Scope with ``session_id`` and/or ``sweep_id`` (query params), or
    omit both to span the namespace. Degrades to an empty, honest result when no cluster is
    reachable instead of 500-ing."""
    ctx = ToolContext(
        settings=app.state.settings,
        allowlist=app.state.allowlist,
        runner=app.state.runner,
        workspace=app.state.settings.resolved_workspace_dir,
    )
    orch = BenchmarkOrchestrator(RealKubeClient(ctx), ctx.workspace)
    try:
        statuses = await orch.reconstruct(namespace=namespace, session_id=session_id, sweep_id=sweep_id)
    except Exception as exc:  # noqa: BLE001 — a read mirror must report "no cluster" softly, not 500
        return JSONResponse({
            "namespace": namespace, "session_id": session_id, "sweep_id": sweep_id,
            "runs": [], "n": 0, "available": False,
            "note": f"could not read cluster jobs: {exc}",
        })
    return JSONResponse({
        "namespace": namespace, "session_id": session_id, "sweep_id": sweep_id,
        "runs": [serialize_status(s) for s in statuses], "n": len(statuses),
        "n_active": sum(1 for s in statuses if not s.terminal),
        "n_terminal": sum(1 for s in statuses if s.terminal),
        "available": True,
    })


def _history_store() -> HistoryStore:
    """The cross-session result store, rooted at the same shared workspace the agent's
    ``result_history`` tool writes to (so the UI browser sees what the agent stored)."""
    return HistoryStore(get_settings().resolved_workspace_dir)


def _history_record_view(rec) -> dict[str, Any]:
    return {
        "id": rec.id, "stored_at": rec.stored_at, "label": rec.label, "tags": rec.tags,
        "model": rec.model, "run_uid": rec.run_uid, "spec": rec.spec,
        "harness": rec.harness, "workload": rec.workload, "namespace": rec.namespace,
        # Reproducibility: when this record has a provenance bundle, surface its id (+ the
        # owning session id) so the sidebar can offer Reproduce / Export report-card.
        "bundle_id": getattr(rec, "bundle_id", None),
        "session_id": getattr(rec, "session_id", None),
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
    show up in their original place. Still-PENDING gates (``session.in_flight_approvals``
    — the turn is parked on them) are interleaved the same way as live, clickable
    ``approval_request`` cards, so an in-flight gate survives a chat switch / pane
    eviction and is restored in its transcript position even on a full history rebuild
    (the in-memory Channel's ``reemit_pending`` is then de-duped on the client by
    request_id, so the card never double-renders).

    Executed commands (``session.commands``) are interleaved the same way — as ``command``
    items right after the tool call that ran them (matched on ``tool_call_id``) — so the
    debug view's inline command trail is restored in its original transcript position on
    resume. Pre-turn probe commands carry no owning tool call (``tool_call_id`` is None):
    they ran before the first message, so they lead the transcript. Any command whose tool
    call is no longer in the replayed messages (e.g. compacted away) is appended at the end
    so the trail is never silently truncated. (Sessions persisted before commands carried a
    ``tool_call_id`` degrade gracefully — every command keys to None and leads instead.)
    """
    # The transcript + every trail list below is reconstructed from on-disk JSON with NO
    # per-element type check (SessionManager.load), so a corrupt / hand-edited / forward-
    # incompatible state.json can carry a NON-DICT element (a torn string, a scalar). Every walk
    # here does ``x.get(...)``, which would escape as an uncaught AttributeError/TypeError and
    # 500 the share route (and tear down the WS history emit on reconnect, bricking that chat) —
    # the same one-corrupt-file blast radius as BUG-011/020-023. ``_dicts`` filters each source
    # to its dict elements up front so a malformed row is simply skipped, not fatal for the whole
    # transcript. (``derive_title``/``SessionManager.list`` already guard ``isinstance(m, dict)``;
    # this brings the resume/share render to parity.)
    def _dicts(seq: Any) -> list[dict[str, Any]]:
        return [x for x in (seq or []) if isinstance(x, dict)] if isinstance(seq, (list, tuple)) else []

    approvals_by_tc: dict[str, list[dict[str, Any]]] = {}
    for a in _dicts(getattr(session, "approvals", [])):
        approvals_by_tc.setdefault(a.get("tool_call_id"), []).append(a)
    pending_by_tc: dict[str, list[dict[str, Any]]] = {}
    for p in _dicts(getattr(session, "in_flight_approvals", [])):
        pending_by_tc.setdefault(p.get("tool_call_id"), []).append(p)
    commands_by_tc: dict[str | None, list[dict[str, Any]]] = {}
    for c in _dicts(getattr(session, "commands", [])):
        commands_by_tc.setdefault(c.get("tool_call_id"), []).append(c)
    card_results_by_tc: dict[str | None, list[dict[str, Any]]] = {}
    for cr in _dicts(getattr(session, "card_results", [])):
        card_results_by_tc.setdefault(cr.get("tool_call_id"), []).append(cr)
    messages = _dicts(getattr(session, "messages", []))
    # Defensive fallback source for the report/analysis CARDS: the LLM-facing ``tool_results``
    # already carry each card tool's result in ``messages``. When ``card_results`` has NO entry
    # for a card-rendering tool call (e.g. the run predated the persist-card-results fix, or the
    # server that ran it wasn't restarted onto that fix), we re-derive a ``tool_result`` item from
    # this message copy so the rich card + its clickable charts still replay instead of degrading
    # to bare metric tiles. Keyed by tool_call_id; the value is the (possibly clamped) result.
    tool_results_by_tc: dict[str | None, dict[str, Any]] = {}
    for m in messages:
        if m.get("role") != "tool_results":
            continue
        for r in _dicts(m.get("results")):
            tool_results_by_tc[r.get("tool_call_id")] = r

    def _command_item(c: dict[str, Any]) -> dict[str, Any]:
        return {"role": "command", "text": c.get("text"), "argv": c.get("argv"),
                "mode": c.get("mode"), "auto_run": c.get("auto_run"),
                "simulated": c.get("simulated")}

    items: list[dict[str, Any]] = []
    rendered_tcs: set[str | None] = set()
    # Pre-turn probe commands ran before any tool call (and before the first message) — lead with them.
    for c in commands_by_tc.get(None, []):
        items.append(_command_item(c))
    rendered_tcs.add(None)
    for m in messages:
        role = m.get("role")
        if role == "user":
            # System-injected user messages are agent-only context the human never typed — skip
            # them so they don't render as a user bubble on resume (mirrors derive_title()'s skip).
            # Two complementary tags: the ``synthetic`` flag (environment pre-probe snapshot) and
            # the bracket-tag convention ("[live catalog snapshot …]", "[environment pre-probe …]")
            # used by the once-per-session catalog injection, which is not synthetic-flagged.
            if m.get("synthetic"):
                continue
            content = m.get("content") or ""
            if isinstance(content, str) and content.startswith("["):
                continue
            items.append({"role": "user", "text": content})
        elif role == "assistant":
            if m.get("content"):
                items.append({"role": "assistant", "text": m["content"]})
            for tc in _dicts(m.get("tool_calls")):
                tc_id = tc.get("id")
                # The UI badges a replayed tool row READ-ONLY/MUTATING; derive it from the modes of the
                # commands that ran under this call (old sessions without tool_call ids → read-only).
                tc_mutating = any(
                    (c.get("mode") or "read_only") != "read_only"
                    for c in commands_by_tc.get(tc_id, [])
                )
                # The persisted wall-clock run time → the replayed action row shows the SAME
                # duration badge a live run does (None when absent on a pre-feature snapshot, or
                # when a corrupt snapshot stored a non-dict tool_durations).
                durations = getattr(session, "tool_durations", None)
                tc_dur = durations.get(tc_id) if isinstance(durations, dict) else None
                items.append({"role": "tool_call", "name": tc.get("name"),
                              "input": tc.get("input"), "mutating": tc_mutating,
                              "duration_s": tc_dur})
                for a in approvals_by_tc.get(tc_id, []):
                    items.append({"role": "approval_decision", "kind": a.get("kind"),
                                  "payload": a.get("payload"), "approved": a.get("approved")})
                for p in pending_by_tc.get(tc_id, []):
                    items.append({"role": "approval_request", "request_id": p.get("request_id"),
                                  "kind": p.get("kind"), "payload": p.get("payload")})
                # The commands this tool call ran fire after its approval (mirroring live order).
                for c in commands_by_tc.get(tc_id, []):
                    items.append(_command_item(c))
                # Then its renderable result — the report summary + clickable charts, etc. — so
                # the rich card is replayed in place (live order: tool_result, then results_card).
                crs = card_results_by_tc.get(tc_id, [])
                if crs:
                    for cr in crs:
                        items.extend(_card_result_items(cr))
                # Fallback: no persisted card result for a card-rendering tool call → re-derive
                # the card (+ its charts) from the tool_result kept in ``messages`` so it doesn't
                # degrade to bare tiles (see ``tool_results_by_tc`` above).
                elif tc.get("name") in CARD_RESULT_TOOLS:
                    fb = _fallback_card_items(
                        tc.get("name"), tool_results_by_tc.get(tc_id), session)
                    items.extend(fb)
                rendered_tcs.add(tc_id)
    # Don't lose commands whose owning tool call fell out of the replayed messages (compaction).
    for tc_id, cmds in commands_by_tc.items():
        if tc_id in rendered_tcs:
            continue
        for c in cmds:
            items.append(_command_item(c))
    # Likewise for card results whose owning tool call was compacted away — append at the end so
    # the report card is never silently dropped from a long, compacted transcript.
    for tc_id, crs in card_results_by_tc.items():
        if tc_id in rendered_tcs:
            continue
        for cr in crs:
            items.extend(_card_result_items(cr))
    return items


def _card_result_items(cr: dict[str, Any]) -> list[dict[str, Any]]:
    """Render items for one persisted card result: the ``tool_result`` (drives the report /
    analysis / env / etc. card) plus, when the analyzer produced one, the deterministic
    ``results_card`` — re-derived from the same result, exactly as the live stream emits it."""
    name, result = cr.get("name"), cr.get("result")
    out: list[dict[str, Any]] = [{"role": "tool_result", "name": name, "result": result}]
    card = build_results_card(name or "", result)
    if card is not None:
        out.append({"role": "results_card", "card": card})
    return out


def _fallback_card_items(name: str | None, tr: dict[str, Any] | None, session) -> list[dict[str, Any]]:
    """Re-derive a card-rendering tool's replay items from the LLM-facing ``tool_result`` kept in
    ``messages`` when no entry exists in ``session.card_results`` (e.g. the run predated the
    persist-card-results fix, or its server wasn't restarted onto it). The message copy carries
    the same result the live stream rendered from — so the rich card replays instead of degrading
    to bare metric tiles. For a report whose stored copy lost its ``charts`` (budget-clamped on a
    huge result), the charts are re-discovered from the run's workspace via the report_path, so the
    clickable thumbnails survive. Returns [] when there is no usable result to render."""
    if not tr:
        return []
    content = tr.get("content")
    result = find_last_json(content, "{") if isinstance(content, str) else content
    if not isinstance(result, dict):
        return []
    # A report card with no charts in the stored copy → re-discover them from disk (the PNGs the
    # harness rendered still live under the per-session workspace). Pure mechanism, no judgment.
    if name == "locate_and_parse_report" and not result.get("charts") and result.get("report_path"):
        try:
            charts = report_locate._discover_charts(
                Path(result["report_path"]), session.ctx.workspace.parent)
        except (OSError, ValueError, AttributeError):
            charts = []
        if charts:
            result = {**result, "charts": charts}
    return _card_result_items({"name": name, "result": result})


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
    try:
        base = (sessions_root / sid).resolve()
        candidate = (base / path).resolve()
        # `base` must be a real session dir directly under sessions_root, and `candidate` must not
        # escape it — together these reject ../ traversal in either `sid` or `path`.
        if base.parent != sessions_root or not base.is_dir() or not candidate.is_relative_to(base):
            raise HTTPException(status_code=404, detail="artifact not found")
        suffix = candidate.suffix.lower()
        if suffix not in _ARTIFACT_SUFFIXES or not candidate.is_file():
            raise HTTPException(status_code=404, detail="artifact not found")
    except (OSError, ValueError):
        # An over-long `sid`/`path` component (ENAMETOOLONG → OSError) or an embedded NUL byte
        # (`%00` → ValueError "embedded null byte") during resolution must read as a clean 404 —
        # never a 500. (HTTPException is neither, so the explicit 404s above propagate untouched.)
        raise HTTPException(status_code=404, detail="artifact not found") from None
    return FileResponse(candidate, media_type=_ARTIFACT_MEDIA[suffix])


class _RevalidateStaticFiles(StaticFiles):
    """Serve the UI assets with ``Cache-Control: no-cache`` so a browser reload ALWAYS picks up the
    latest ``app.js`` / ``styles.css``.

    The UI is a single-page app: it fetches ``/static/app.js`` once and never re-fetches it on
    in-app navigation (new chat, new run). With the default static headers a browser will happily
    keep serving a cached copy, so a shipped UI change stays invisible until a manual hard-refresh —
    a real, repeated source of "I can't see the new button" confusion. ``no-cache`` does not disable
    caching; it forces the browser to REVALIDATE every load (a cheap conditional request that still
    returns 304 when nothing changed), so the first reload after a deploy gets the new bytes."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        # Only tag real file responses (200/206/304); leave 404s etc. alone.
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


def _resolve_bundle(sid: str, bundle_id: str) -> dict[str, Any]:
    """Locate one provenance bundle JSON under a session's ``bundles/`` dir, reusing the SAME
    path-traversal hardening as ``session_artifact`` (``base.parent == sessions_root``,
    ``is_relative_to``) PLUS the BundleStore's own ``_safe_id`` guard on the bundle id. A 404 for
    a bad ``sid`` / ``bundle_id`` / missing bundle (never an info leak)."""
    sessions_root = (get_settings().resolved_workspace_dir / "sessions").resolve()
    try:
        base = (sessions_root / sid).resolve()
        if base.parent != sessions_root or not base.is_dir():
            raise HTTPException(status_code=404, detail="bundle not found")
    except (OSError, ValueError):
        # Over-long `sid` (ENAMETOOLONG → OSError) or an embedded NUL byte (`%00` → ValueError) →
        # clean 404, never a 500.
        raise HTTPException(status_code=404, detail="bundle not found") from None
    bundle = BundleStore(base).read(bundle_id)  # _safe_id inside rejects ../ and a/b ids
    if bundle is None:
        raise HTTPException(status_code=404, detail="bundle not found")
    # Defense in depth: the resolved file must stay inside the session's bundles dir.
    candidate = (base / "bundles" / f"{bundle_id}.json").resolve()
    if not candidate.is_relative_to(base):
        raise HTTPException(status_code=404, detail="bundle not found")
    return bundle


@app.get("/api/sessions/{sid}/bundle/{bundle_id}", dependencies=[Depends(rate_limit)])
async def session_bundle(sid: str, bundle_id: str) -> JSONResponse:
    """One provenance bundle's JSON (for the UI's Reproduce / Export affordances)."""
    return JSONResponse(_resolve_bundle(sid, bundle_id))


@app.get("/api/sessions/{sid}/bundle/{bundle_id}/report-card.html", dependencies=[Depends(rate_limit)])
async def session_bundle_report_card(sid: str, bundle_id: str) -> Response:
    """Download a self-contained, shareable HTML report card for a provenance bundle (no
    external assets). Same path-traversal hardening as the artifact route."""
    bundle = _resolve_bundle(sid, bundle_id)
    html_doc = render_report_card(bundle)
    return Response(
        content=html_doc,
        media_type="text/html",
        headers={"Content-Disposition": f'attachment; filename="report-card-{bundle_id}.html"'},
    )


# ---------------------------------------------------------------------------
# Share a chat via a read-only public link (ChatGPT-style).
#
# Minting/revoking a share is owner-only (the auth-gated POST/DELETE below); VIEWING a share is
# public — the GET routes are exempted from Bearer auth in app.security.auth (the unguessable
# token is the credential). A share is an IMMUTABLE snapshot of the chat's transcript taken at
# share time (see app.storage.share.ShareStore), so continuing or deleting the chat never changes
# or breaks the link.
# ---------------------------------------------------------------------------
def _share_store() -> ShareStore:
    """The conversation-share store, rooted at the same shared workspace as sessions/history."""
    return ShareStore(get_settings().resolved_workspace_dir)


# Fields on a card tool's ``result`` that carry server-internal absolute filesystem paths — the
# located report's path (``<sessions_root>/<session_id>/.../benchmark_report*.json``), and the
# directories a not-found probe searched. They drive nothing in the read-only viewer (the client
# renders ``summary``/``charts`` only; charts are already session-relative), yet a public share is
# UNAUTHENTICATED, so shipping them would disclose the host path layout AND the owning session id —
# the very id the snapshot deliberately withholds (see read_share / shared_chat._PUBLIC_FIELDS).
_SHARE_REDACT_RESULT_KEYS = ("report_path", "searched")


def _redact_share_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip server-internal absolute paths from a PUBLIC share snapshot's tool_result rows.

    The transcript replayed to the owner on resume legitimately carries the located report's
    absolute path; a public share must not. Returns NEW item dicts (the live session is never
    mutated) with the path-bearing keys removed from any ``tool_result`` result, leaving every
    render-relevant field (summary, charts, metrics) intact."""
    out: list[dict[str, Any]] = []
    for it in items:
        result = it.get("result") if it.get("role") == "tool_result" else None
        if isinstance(result, dict) and any(k in result for k in _SHARE_REDACT_RESULT_KEYS):
            scrubbed = {k: v for k, v in result.items() if k not in _SHARE_REDACT_RESULT_KEYS}
            out.append({**it, "result": scrubbed})
        else:
            out.append(it)
    return out


@app.post("/api/sessions/{sid}/share", dependencies=[Depends(rate_limit)])
async def create_share(sid: str) -> JSONResponse:
    """Mint a read-only public link for a chat — an immutable snapshot of its transcript NOW.

    Owner-only (auth-gated). Snapshots the same render-friendly transcript the UI replays on
    resume, but drops any still-PENDING approval gate (that is live, clickable session state, not
    transcript — a public snapshot must never carry an actionable gate). 404 for an unknown chat;
    400 when there is nothing to share yet (a brand-new, empty chat)."""
    session = app.state.sessions.get_or_load(sid)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    items = [it for it in _history_items(session) if it.get("role") != "approval_request"]
    # A public share is UNAUTHENTICATED: scrub server-internal absolute paths (the located
    # report's path, search roots) from card results before they're frozen into the snapshot, so
    # the link never leaks the host path layout or the owning session id those paths embed.
    items = _redact_share_items(items)
    if not items:
        raise HTTPException(status_code=400, detail="nothing to share yet")
    token = _share_store().create(
        items=items,
        title=session.title or "Shared conversation",
        created_at=session.created_at,
        source_session_id=session.id,
        usage={
            "input": session.total_input_tokens,
            "output": session.total_output_tokens,
            "cache_read": session.total_cache_read_tokens,
            "total": session.session_total,
        },
    )
    # Absolute public URL when SHARE_BASE_URL is set (shareable off-host); otherwise a relative
    # path the browser resolves against its own origin (see app.config.Settings.share_base_url).
    base = get_settings().share_link_base
    url = f"{base}/share/{token}" if base else f"/share/{token}"
    return JSONResponse({"token": token, "url": url})


@app.get("/api/share/{token}", dependencies=[Depends(rate_limit)])
async def read_share(token: str) -> JSONResponse:
    """PUBLIC read-only transcript of a shared conversation (no auth — the token is the
    credential). 404 for a malformed/unknown/revoked token. Returns only the snapshot fields the
    viewer needs — the owning session id is deliberately withheld."""
    data = _share_store().read(token)
    if data is None:
        raise HTTPException(status_code=404, detail="shared conversation not found")
    return JSONResponse({
        "title": data.get("title"),
        "created_at": data.get("created_at"),
        "shared_at": data.get("shared_at"),
        "items": data.get("items", []),
        "usage": data.get("usage"),
    })


@app.get("/api/share/{token}/page.html", dependencies=[Depends(rate_limit)])
async def read_share_page(token: str) -> Response:
    """PUBLIC self-contained, offline ``.html`` export of a shared conversation — the SPA + the
    frozen snapshot inlined into ONE dependency-free file (no external assets, no network on open).
    Host it anywhere, or open it from disk: the agent is never involved. This is the artifact the
    publish-a-public-link path puts on a static host. 404 for a malformed/unknown/revoked token."""
    data = _share_store().read(token)
    if data is None:
        raise HTTPException(status_code=404, detail="shared conversation not found")
    html_doc = render_shared_chat(data, ui_dir=get_settings().ui_dir)
    return Response(
        content=html_doc,
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="shared-chat-{token}.html"'},
    )


@app.post("/api/share/{token}/publish", dependencies=[Depends(rate_limit)])
def publish_share(token: str) -> JSONResponse:
    """Publish a shared conversation as a PUBLIC link WITHOUT exposing the agent: render its frozen
    snapshot to a self-contained ``.html`` and upload it as a SECRET (unlisted) GitHub gist via the
    user's ``gh`` CLI. Returns the public render URL the dialog shows by default. Owner-only
    (auth-gated like minting/revoking — it is NOT a public GET; it creates content on the user's
    GitHub account). 404 if the token isn't a live share; 503 (with a machine ``reason``) when ``gh``
    is missing or fails, so the dialog can explain it and fall back to the same-origin link. Declared
    sync on purpose: the blocking ``gh`` subprocess runs in Starlette's threadpool, off the loop."""
    settings = get_settings()
    data = _share_store().read(token)
    if data is None:
        raise HTTPException(status_code=404, detail="shared conversation not found")
    try:
        result = gist_publish.publish(
            token, workspace=settings.resolved_workspace_dir, ui_dir=settings.ui_dir, snapshot=data
        )
    except gist_publish.GistPublishError as exc:
        # Not a server fault — the publishing host (the user's gh/GitHub) is unavailable. Hand the
        # reason back so the dialog can say why and fall back to the local link.
        return JSONResponse(status_code=503, content={"detail": str(exc), "reason": exc.reason})
    return JSONResponse({
        "token": result.token,
        "gist_id": result.gist_id,
        "public_url": result.public_url,
        "fallback_url": result.fallback_url,
        "reused": result.reused,
    })


@app.delete("/api/share/{token}", dependencies=[Depends(rate_limit)])
def revoke_share(token: str) -> JSONResponse:
    """Revoke a share link: delete its snapshot AND, if one was published, its secret gist — so the
    public link dies together with the in-app link. Owner-only (auth-gated). 404 if the snapshot is
    already gone. Gist deletion is best-effort: a gh/network failure still revokes the snapshot (the
    gist mapping survives under ``<workspace>/shares`` to be cleaned later via the script). Sync for
    the same threadpool reason as publish."""
    # Reject a malformed token BEFORE it reaches the filesystem (gist-mapping lookup) or the ``gh``
    # subprocess argv — the read/delete store paths guard internally, but the gist-revoke runs first.
    if not is_valid_token(token):
        raise HTTPException(status_code=404, detail="shared conversation not found")
    settings = get_settings()
    gist_revoked = False
    if gist_publish.mapping_path(settings.resolved_workspace_dir, token).exists():
        try:
            gist_publish.revoke(token, workspace=settings.resolved_workspace_dir)
            gist_revoked = True
        except gist_publish.GistPublishError:
            gist_revoked = False  # best-effort; the snapshot deletion below still proceeds
    if not _share_store().delete(token):
        raise HTTPException(status_code=404, detail="shared conversation not found")
    return JSONResponse({"deleted": True, "token": token, "gist_revoked": gist_revoked})


@app.get("/share/{token}")
async def share_page(token: str) -> FileResponse:
    """PUBLIC read-only viewer PAGE for a shared conversation. Serves the SPA shell exactly like
    ``/`` — the client (app.js) detects the ``/share/<token>`` path, fetches ``/api/share/<token>``,
    and renders the snapshot read-only (no WebSocket, no composer, no sidebar). We don't 404 here
    on an unknown token: the page itself shows a friendly "link not found / revoked" state from the
    JSON route, which keeps the SPA the single source of that messaging."""
    return FileResponse(get_settings().ui_dir / "index.html")


app.mount("/static", _RevalidateStaticFiles(directory=str(get_settings().ui_dir)), name="static")


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
    # ``after_seq`` is the client's resume cursor (chat-switch state fix): when a client keeps a
    # cached transcript for this chat, it reconnects with the highest turn-event seq it has
    # rendered so we can replay ONLY the tail it missed onto that cached view instead of
    # resending the whole history (no flash, no duplicate rendering). Absent/garbage => full path.
    after_raw = websocket.query_params.get("after_seq")
    # ``str.isdigit()`` is broader than ``int()`` accepts (it's True for unicode digits like the
    # superscript ``²``, which ``int()`` then rejects with ValueError) — so require ASCII digits
    # too, else a crafted ``?after_seq=²`` would raise out of the handshake. Garbage => full path.
    after_seq = int(after_raw) if (after_raw and after_raw.isascii() and after_raw.isdigit()) else None
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
        # A fresh Channel starts with an empty `pending` dict, but the session may have been
        # parked at an approval gate when its previous Channel was evicted (or never had one — a
        # turn parked in the background after the socket dropped). Restore those still-undecided
        # gates from the persisted `session.in_flight_approvals` so `reemit_pending` can re-surface
        # the live approval card AND `resolve` can accept the user's decision — otherwise the card
        # is silently lost on chat-switch / reconnect.
        channel.restore_pending(session.in_flight_approvals)
        app.state.channels[session.id] = channel
    channel.ws = websocket  # last connection wins (single active tab, Claude-web style)
    # Seq head at the instant we attached, captured BEFORE the ready/history sends below. A turn
    # still running in the background can emit during those awaits; ``emit`` fans those frames out
    # LIVE to this just-attached socket AND buffers them, so the buffer replay further down must NOT
    # resend them (they'd double-render — the client only de-dupes approval cards). Capping the
    # replay at this cutoff sends only what the socket genuinely missed (BUG-032).
    replay_cutoff = channel.cur_seq
    # Snapshot the live buffer NOW, synchronously, before the eviction-prone ready/history awaits
    # below. The missed tail the reconnecting client must replay — (after_seq, replay_cutoff] — is
    # fully fixed at this instant; nothing emitted during those awaits belongs in it (live emits go
    # straight to this just-attached socket with seq > replay_cutoff). But the live ring is BOUNDED:
    # a chatty background turn that emits a burst during the awaits can EVICT the front of that
    # missed tail before replay_live reads the buffer, permanently dropping frames the client never
    # received live and never gets replayed (no de-dup, no later re-fetch). Pin the tail here so it
    # survives any such eviction.
    replay_snapshot = channel.buffered_events

    busy = {"value": False}

    async def run_turn(text: str) -> None:
        busy["value"] = True
        # Open a fresh live-event buffer for this turn (Phase 15): every event emitted below is
        # appended to the channel's bounded ring buffer so a client that reconnects mid-turn can
        # replay what it missed and catch up to the LIVE stream.
        channel.begin_turn()
        # Abandoned-turn guard (sim-1 00:40), reconciled with plan-survival. Two run states must
        # survive a disconnect: (1) once the user APPROVES a mutating run (SessionPlan or a gated
        # command) it finishes in the background — a benchmark you navigated away from still
        # completes (the WS-disconnect finally backgrounds it); and (2) a turn that is still only
        # thinking/probing must be allowed to reach its FIRST approval gate even with no socket
        # attached, because that gate IS the deliverable of a plan-proposal turn — once surfaced it
        # parks for free (awaiting the user's decision, holding no concurrency slot) and is persisted
        # to session.in_flight_approvals, so it re-surfaces when the chat reopens. The original guard
        # cut the turn off BEFORE that gate, so a user who switched chats during the pre-plan probe
        # never got a plan to approve on return — and the same bug recurred mid-session, before any
        # later gate (session 4dd131482da9: the probe step ran, then the turn was abandoned before
        # proposing the sweep). So should_continue() stays True while a socket is attached (incl. a
        # reconnect that took over the channel), OR an approval was granted this turn (state 1), OR
        # this turn has not yet surfaced its first approval gate (state 2 — let it reach the gate and
        # park). Once that first gate is open the in-tool park stops further stepping on its own; the
        # guard is still honored at step boundaries (never mid-tool), so a turn that keeps probing
        # without ever proposing a gate is bounded by MAX_STEPS.
        approved = {"value": False}
        gate_surfaced = {"value": False}
        # Set when this turn ends because the user CANCELLED it (clicked Stop). The steer backstop
        # in `finally` must NOT fire for a cancelled turn — otherwise a steer the user queued just
        # before stopping would resurrect the very run they asked to stop (a fresh follow-up turn).
        was_cancelled = {"value": False}

        async def _request_approval(kind: str, payload: dict) -> bool:
            # This turn has now reached a user-facing approval gate. Flip BEFORE awaiting so the
            # guard sees it the instant the gate opens (the await below parks the turn here until the
            # user decides — or indefinitely, for free, if they're away).
            gate_surfaced["value"] = True
            ok = await channel.request_approval(kind, payload)
            if ok:
                approved["value"] = True
            return ok

        def _should_continue() -> bool:
            return channel.ws is not None or approved["value"] or not gate_surfaced["value"]

        try:
            if loop is None:
                # No LLM, but still record the turn so the chat persists / resumes.
                session.messages.append({"role": "user", "content": text})
                session.persist()
                await channel.emit("error", {"message": f"LLM provider not configured: {app.state.provider_error}"})
                await channel.emit("done", {})
                return
            await loop.run_turn(
                session, text,
                emit=channel.emit,
                request_approval=_request_approval,
                should_continue=_should_continue,
            )
        except asyncio.CancelledError:
            # The turn was cancelled (Phase 16: cancel tool / control message / graceful
            # shutdown). The concurrency slot is already released as this unwinds the runner's
            # `async with run_semaphore`. Announce it (best-effort — a socket may be attached)
            # and persist so a resumed chat shows the run ended, then re-raise so the task is
            # marked cancelled and the registry's awaiter returns. Flag the cancel so the steer
            # backstop in `finally` does NOT spawn a follow-up turn (the user asked to STOP).
            was_cancelled["value"] = True
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
            # Steer backstop (residual race). A steer the user sent in the NARROW window between the
            # loop's last drain and `done` — i.e. during the closing awaits (provider-turn teardown /
            # the `done` emit) — is still queued and would otherwise hang until the user's next
            # message. Every steer sent DURING the turn is handled inside loop.run_turn and never
            # reaches here; this catches only that tail. If any remain — and a socket is still
            # attached to hear the reply — start a fresh follow-up turn now so the agent still
            # answers promptly. asyncio.create_task schedules it on the next tick, so this turn's
            # `busy=False`/registry-pop above settle first; the new turn re-arms both.
            #
            # EXCEPT when this turn was CANCELLED (the user clicked Stop): the backstop would then
            # resurrect the very run the user just stopped — a steer queued mid-turn (the cancel
            # raises CancelledError before the loop drained it) would start a fresh follow-up turn,
            # so the agent keeps working right after "Stop". Drop the leftover steer instead; the
            # cancel must win. (A still-undecided gate that the user steered-to-decline is handled
            # on the live socket, not here.)
            leftover = session.ctx.steer_messages
            if was_cancelled["value"]:
                session.ctx.steer_messages = []
                if channel.ws is None and not channel.pending:
                    app.state.channels.pop(session.id, None)
            elif leftover and loop is not None and channel.ws is not None:
                followup = "\n\n".join(leftover)
                session.ctx.steer_messages = []
                with log_bind(corr_id=new_corr_id(), session_id=session.id):
                    backstop = asyncio.create_task(run_turn(followup))
                app.state.running[session.id] = backstop
                app.state.runs.register(session.id, backstop)
            # Forget the channel once its turn ends with nothing attached/pending, so a long-
            # lived server doesn't accumulate one channel per chat ever opened.
            elif channel.ws is None and not channel.pending:
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
                "metrics_server",
            ])
        except Exception:  # noqa: BLE001 — pre-probe is best-effort; never break connect
            pass

    _running_task = app.state.running.get(session.id)
    # Resume decision (chat-switch state fix): if the client kept a cached transcript for this
    # chat (it sent ``after_seq``) and that cursor still sits within the retained live buffer,
    # patch its view with only the missed tail instead of resending the whole history. The check
    # is independent of ``turn_active`` so a chat that *finished* in the background while the user
    # was away still patches the buffered tail (incl. its ``done``) onto the cached view. A cursor
    # that has fallen off the buffer — or predates a new turn that cleared it — fails the window
    # test and falls back to a full history rebuild.
    incremental = (
        after_seq is not None
        and channel.min_buffered_seq is not None
        and channel.min_buffered_seq - 1 <= after_seq <= channel.cur_seq
    )
    await channel.emit("ready", {
        "session_id": session.id,
        "resumed": resumed,
        "running": bool(_running_task and not _running_task.done()),
        # Server-authoritative elapsed time of the in-flight turn (None when idle) so the client
        # seeds its "thinking seconds" from the TRUE start and keeps ticking across a chat switch.
        "running_elapsed_ms": channel.elapsed_ms,
        # Resume hint: tells the client whether we patched its cached view (incremental) or are
        # about to send a full history rebuild, plus the current cursor head.
        "resume": {"incremental": incremental, "cur_seq": channel.cur_seq},
        # Persisted token tally so the header chip is correct immediately on connect/reload,
        # before any new turn (token-tracking feature).
        "usage": {
            "input": session.total_input_tokens,
            "output": session.total_output_tokens,
            "cache_read": session.total_cache_read_tokens,
            "total": session.session_total,
        },
        # Last-known context-window occupancy (persisted), so the "context used" meter is right on
        # connect/reload before the next turn refreshes it. No limit: see loop.py (model can change).
        "context_window": {
            "tokens": session.last_context_tokens,
        },
    })
    if resumed and not incremental:
        # Commands are interleaved into `items` (as `command` entries in their original
        # transcript position) by _history_items — no separate flat `commands` list needed.
        await channel.emit("history", {"items": _history_items(session)})
    elif not resumed:
        # Brand-new chat: emit the DETERMINISTIC welcome card (B2) FIRST — a code-built greeting
        # that concisely offers the assistant's capabilities, consistent every time and with NO
        # LLM turn spent (its judgment text lives in knowledge/welcome.md). NOT shown on resume
        # (this branch is gated on `not resumed`, so a chat with history never re-greets). Then
        # surface the start-of-chat suggestion chips (W1) right after `ready`/`welcome`, and kick
        # off a NON-blocking read-only environment pre-probe (W2) so the first turn starts
        # environment-aware without an extra LLM round-trip. None of these block input-enable: the
        # probe runs in the background and its snapshot is consumed on the first turn if ready.
        welcome = build_welcome(session.ctx)
        if welcome is not None:
            await channel.emit(ws_events.WELCOME, welcome)
        chips = load_suggestions(get_settings())
        if chips:
            await channel.emit(ws_events.SUGGESTIONS, {"chips": chips})
        if loop is not None:
            # Track the task: a bare create_task is only weakly referenced by the loop, so between
            # the probe's subprocess awaits it could be GC'd and silently cancelled (BUG-033).
            prewarm = asyncio.create_task(_prewarm_env(session, channel))
            app.state.background_tasks.add(prewarm)
            prewarm.add_done_callback(app.state.background_tasks.discard)
    # Replay buffered LIVE events so a reconnecting client catches up to the live stream — the
    # events it missed, in order — rather than waiting blind for the final result. Two paths:
    #   • incremental: patch only the missed tail (seq > after_seq) onto the client's CACHED
    #     view; runs even when the turn already ended, so a finished-while-away chat still gets
    #     its buffered tail (incl. ``done``) appended in place — no flash, no full rebuild.
    #   • full: a turn is still running and the client is rebuilding from history; replay the
    #     whole buffer below the freshly-replayed transcript (the original behavior).
    if incremental:
        await channel.replay_live(after_seq, through_seq=replay_cutoff, frames=replay_snapshot)
    elif channel.turn_active:
        await channel.replay_live(through_seq=replay_cutoff, frames=replay_snapshot)
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
                turn_running = busy["value"] or (existing is not None and not existing.done())
                if channel.pending:
                    # The user typed a message INSTEAD of clicking Approve/Decline on an open
                    # gate. Treat it as: decline the pending action(s) AND steer with this text.
                    if turn_running:
                        # A live turn is parked at the gate. Hand the typed text to the loop (it
                        # drains it into the transcript right after the rejected tool result, so
                        # the same turn continues and the model responds to the steer — possibly
                        # re-proposing a fresh card), then reject the gate(s) to unpark the turn.
                        # Capture the steer text ONCE even if several gates are open.
                        session.ctx.steer_messages.append(msg.text)
                        for rid in list(channel.pending):
                            channel.resolve(rid, False)
                        continue
                    # No live turn owns the gate (e.g. a gate restored from disk after a restart,
                    # with no parked turn to resume). Just clear the stale card(s) as declined and
                    # fall through to start a fresh turn that handles this message normally.
                    for rid in list(channel.pending):
                        channel.resolve(rid, False)
                if turn_running:
                    # STEER (Claude-Code style): the user typed while a turn is mid-flight and NO
                    # approval gate is open (the gate case is handled above). Don't reject and don't
                    # start a concurrent turn on the same chat — queue the text so the running loop
                    # injects it as a real user turn at its NEXT step boundary (loop.py drains
                    # ctx.steer_messages). The agent thus receives the message as soon as it finishes
                    # the current step and adapts, instead of it being dropped with "please wait".
                    # No echo frame: the UI already rendered the user's bubble optimistically, exactly
                    # as for a normal send (parity with the start-of-turn path below).
                    session.ctx.steer_messages.append(msg.text)
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
