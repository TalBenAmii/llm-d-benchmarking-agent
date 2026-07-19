"""FastAPI app: serves the chat UI and hosts the agent over a WebSocket.

The backend is the security + secrets boundary. The browser only ever exchanges chat
text, structured events, and Approve/Reject decisions — never API keys, never raw commands.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import signal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response

from app.agent import events as ws_events
from app.agent.cards import build_welcome, load_suggestions
from app.agent.channel import Channel
from app.agent.engine import SdkNativeEngine
from app.agent.engine import steer as engine_steer
from app.agent.lifecycle import RunRegistry
from app.agent.session import SessionManager
from app.agent.transcript import history_items as _history_items
from app.agent.ws_schemas import (
    ApprovalIn,
    CancelIn,
    PingIn,
    SetAutoApproveIn,
    SetModelIn,
    UserMessageIn,
    ValidationError,
    outbound,
    parse_inbound,
)
from app.config import get_settings
from app.llm.model_catalog import AGENT_SDK_PROVIDERS, valid_selection
from app.observability import metrics as instrument
from app.observability.logging import bind as log_bind
from app.observability.logging import new_corr_id, setup_logging
from app.observability.metrics import render_prometheus
from app.orchestrator.controller import BenchmarkOrchestrator
from app.orchestrator.kube import RealKubeClient
from app.packaging.report_card import render_report_card
from app.packaging.shared_chat import render_shared_chat
from app.security.policy import CommandPolicy
from app.security.runner import CommandRunner
from app.storage.history import HistoryStore, available_metrics, trend
from app.storage.retention import readiness, run_gc, self_check
from app.storage.share import ShareStore, is_valid_token
from app.tools.context import ToolContext
from app.tools.run.manage_runs import serialize_status
from app.tools.setup.probe import probe_environment
from app.web import (
    RevalidateStaticFiles,
    install_cors,
    provider_view,
    resolve_artifact,
    resolve_bundle,
)
from app.web import first_validation_message as _first_validation_message
from app.web import history_record_view as _history_record_view
from app.web import redact_share_items as _redact_share_items

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
    # The policy still governs the DEDICATED command tools (execute_llmdbenchmark, probes,
    # orchestrator) via ctx.run_command/ctx.run_readonly. The agent's ad-hoc `run_shell` tool runs
    # arbitrary `bash -lc` and does NOT consult it (human approval still gates mutating commands).
    app.state.policy = CommandPolicy.from_file(settings.command_policy_path)
    # Always the REAL runner — even under SIMULATE=1. SIMULATE no longer swaps in a runner that
    # empties EVERY command (which left the agent blind: read-only greps/probes returned nothing).
    # Instead the caller-gate (CommandExecutor / run_shell) no-ops only MUTATING commands, so
    # READ-ONLY probes/greps run for real and the agent gathers genuine context while nothing is
    # deployed or benchmarked. (Tests/eval inject SimRunner directly for full hermetic isolation.)
    app.state.runner = CommandRunner(settings.repo_paths, extra_env=settings.extra_subprocess_env)
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
        settings, app.state.policy, app.state.runner,
        run_semaphore=app.state.run_semaphore, runs=app.state.runs,
    )
    # SDK-native engine: no provider object to build. An unsupported LLM_PROVIDER is a clear
    # readiness failure (/readyz provider_coherent) + a per-turn error, never a crash.
    app.state.provider_supported = (
        (settings.llm_provider or "claude-agent-sdk").lower() in AGENT_SDK_PROVIDERS)
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
    return summary


def _active_session_ids(app: FastAPI) -> set[str]:
    """Sessions the retention GC must NOT prune: any held in memory by the SessionManager plus
    any with a turn currently running (background benchmark) — the active-run safety (Phase 18)."""
    sessions = getattr(app.state, "sessions", None)
    ids: set[str] = sessions.active_ids() if sessions is not None else set()
    ids |= set(getattr(app.state, "running", {}) or {})
    return ids


app = FastAPI(
    title="llm-d Benchmarking Assistant",
    lifespan=lifespan,
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
    — workspace writable, provider configured, repos present, runner ok (the policy
    loads; Phase 16 splits this from /healthz liveness). Returns 200 when ready, 503 when not,
    with the STRUCTURED self-check reasons so an operator/orchestrator can see *why*. Liveness
    stays on the minimal /healthz; this is the readiness gate a K8s readinessProbe / load
    balancer should poll."""
    contrib = readiness(get_settings())
    code = status.HTTP_200_OK if contrib.get("ready") else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(contrib, status_code=code)


@app.get("/metrics")
async def metrics() -> PlainTextResponse:
    """Prometheus scrape endpoint: agent + orchestrator metrics in the text exposition format.
    Point a Prometheus scrape at this path (see deploy/observability/prometheus-scrape.yaml)
    and import deploy/observability/grafana-dashboard.json to visualize them."""
    return PlainTextResponse(render_prometheus(instrument.REGISTRY), media_type=_PROM_CONTENT_TYPE)


@app.get("/api/sessions")
async def list_sessions() -> JSONResponse:
    """Recent chats for the sidebar (summaries only, newest first)."""
    return JSONResponse({"sessions": app.state.sessions.list()})


@app.get("/api/provider")
async def provider_info() -> JSONResponse:
    """The active LLM provider + model for the header badge — plus whether the provider
    actually built at startup, so the UI can show "LLM not configured" instead of leaving
    the failure to surface at the first chat message. No secrets, no account identity.
    ``getattr``: same no-lifespan defense as graceful_shutdown (a bare read would 500)."""
    return JSONResponse(provider_view(get_settings()))


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


@app.delete("/api/sessions/{sid}")
async def delete_session(sid: str) -> JSONResponse:
    if not app.state.sessions.delete(sid):
        raise HTTPException(status_code=404, detail="session not found")
    _teardown_session_runtime(sid)
    return JSONResponse({"deleted": True, "id": sid})


@app.delete("/api/namespaces/{namespace}")
async def delete_namespace(namespace: str) -> JSONResponse:
    """Delete a whole sidebar folder — every chat in one namespace at once. The literal
    ``no_namespace`` sentinel removes the chats that have no namespace set."""
    deleted = app.state.sessions.delete_namespace(namespace)
    if not deleted:
        raise HTTPException(status_code=404, detail="no chats in that namespace")
    for sid in deleted:
        _teardown_session_runtime(sid)
    return JSONResponse({"deleted": deleted, "count": len(deleted)})


@app.get("/api/jobs")
async def list_orchestrated_jobs(
    namespace: str, session_id: str | None = None, sweep_id: str | None = None,
) -> JSONResponse:
    """Read-only REST mirror of the orchestrator's run state for non-chat clients (proposal G1):
    list the agent-managed benchmark Jobs in ``namespace``, classified FRESH from the cluster
    (``BenchmarkOrchestrator.reconstruct`` — the cluster is the source of truth, nothing is held
    locally). This is the SAME state ``manage_orchestrated_runs(action='list')`` returns, exposed
    as a plain HTTP GET so a programmatic client can poll run state without driving the chat LLM.

    READ-ONLY by design: it runs an policy-allowed ``kubectl get jobs`` and mutates nothing —
    submitting and stopping runs stay approval-gated through the chat tool, keeping the
    agent-first surface intact. Scope with ``session_id`` and/or ``sweep_id`` (query params), or
    omit both to span the namespace. Degrades to an empty, honest result when no cluster is
    reachable instead of 500-ing."""
    ctx = ToolContext(
        settings=app.state.settings,
        policy=app.state.policy,
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


@app.get("/api/history")
async def list_history(tag: str | None = None, model: str | None = None) -> JSONResponse:
    """Stored historical results for the results-browser (newest first, summaries only)."""
    records = _history_store().list(tag=tag, model=model)
    return JSONResponse({
        "records": [_history_record_view(r) for r in records],
        "metrics": available_metrics(),
    })


@app.get("/api/history/trend")
async def history_trend(metric: str, tag: str | None = None, model: str | None = None) -> JSONResponse:
    """Time-series of one metric across stored results, for the trends view. Facts only —
    the value series + the metric's better-direction; no regression verdict (that's the agent)."""
    records = _history_store().list(tag=tag, model=model)
    return JSONResponse(trend(records, metric))


# Per-run chart images (e.g. the latency/throughput PNGs inference-perf renders into a
# session's analysis/ dir) live under the gitignored workspace, which the /static mount does
# NOT serve. This read-only route exposes them so the UI can show a run's charts inline next to
# its summary. Hardened (image suffixes only + INSIDE the named session dir — see
# app.web.resolve_artifact). The chart paths come from locate_and_parse_report's `charts` field.
@app.get("/api/sessions/{sid}/artifact")
async def session_artifact(sid: str, path: str) -> FileResponse:
    """Serve one image artifact from a session's workspace dir (read-only, image-only)."""
    # Resolve the workspace HERE (so a test monkeypatching app.main.get_settings still steers
    # which dir is served) and hand the pure resolver the already-resolved sessions root.
    sessions_root = (get_settings().resolved_workspace_dir / "sessions").resolve()
    candidate, media_type = resolve_artifact(sessions_root, sid, path)
    return FileResponse(candidate, media_type=media_type)


def _resolve_bundle(sid: str, bundle_id: str) -> dict[str, Any]:
    """Resolve one provenance bundle's JSON for the routes below — a thin wrapper that resolves
    the workspace via the module-level ``get_settings`` (kept here so a test monkeypatching
    ``app.main.get_settings`` still steers which dir is read, exactly as before) and delegates the
    path-traversal hardening to ``app.web.resolve_bundle``."""
    sessions_root = (get_settings().resolved_workspace_dir / "sessions").resolve()
    return resolve_bundle(sessions_root, sid, bundle_id)


@app.get("/api/sessions/{sid}/bundle/{bundle_id}")
async def session_bundle(sid: str, bundle_id: str) -> JSONResponse:
    """One provenance bundle's JSON (for the UI's Reproduce / Export affordances)."""
    return JSONResponse(_resolve_bundle(sid, bundle_id))


@app.get("/api/sessions/{sid}/bundle/{bundle_id}/report-card.html")
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
# Minting/revoking a share (POST/DELETE below) is a normal route on this single-user in-cluster
# service; VIEWING a share (GET) is deliberately public — the unguessable token IS the credential,
# so a share link works for its recipient with no app session at all. A share is an IMMUTABLE
# snapshot of the chat's transcript taken at share time (see app.storage.share.ShareStore), so
# continuing or deleting the chat never changes or breaks the link.
# ---------------------------------------------------------------------------
def _share_store() -> ShareStore:
    """The conversation-share store, rooted at the same shared workspace as sessions/history."""
    return ShareStore(get_settings().resolved_workspace_dir)


# The PUBLIC-share path-redaction (constants + ``_redact_share_items``) lives in app.web;
# imported above. Applied below before a snapshot is frozen so the unauthenticated link never
# leaks the host path layout / owning session id those internal paths embed.


def _inline_share_chart_artifacts(
    items: list[dict[str, Any]], *, sessions_root: Path
) -> list[dict[str, Any]]:
    """Make a public share's report charts self-contained: replace each chart's session-relative
    ``{session_id, path}`` reference with an inline ``data:`` URI of the PNG bytes, so the shared
    JSON AND its offline ``page.html`` export render charts from the snapshot ITSELF — never from
    ``/api/sessions/<id>/artifact`` (no live session dir, no real session id in a public URL, and
    the charts survive deletion of the source session). Reuses ``resolve_artifact``'s path-traversal
    + image-suffix hardening; a chart whose file is missing/unreadable is dropped so a gone artifact
    can never fail the share. Returns NEW item dicts — the live session is never mutated."""
    out: list[dict[str, Any]] = []
    for it in items:
        result = it.get("result") if it.get("role") == "tool_result" else None
        if not isinstance(result, dict):
            out.append(it)
            continue
        charts = result.get("charts")
        if not isinstance(charts, list) or not charts:
            out.append(it)
            continue
        inlined: list[dict[str, Any]] = []
        for c in charts:
            if not (isinstance(c, dict)
                    and isinstance(c.get("session_id"), str)
                    and isinstance(c.get("path"), str)):
                continue
            try:
                candidate, media_type = resolve_artifact(sessions_root, c["session_id"], c["path"])
                data = candidate.read_bytes()
            except (HTTPException, OSError):
                continue      # missing / unreadable / bad path → drop this chart, never crash
            chart = {k: v for k, v in c.items() if k not in ("session_id", "path")}
            chart["src"] = f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}"
            inlined.append(chart)
        out.append({**it, "result": {**result, "charts": inlined}})
    return out


@app.post("/api/sessions/{sid}/share")
async def create_share(sid: str) -> JSONResponse:
    """Mint a read-only public link for a chat — an immutable snapshot of its transcript NOW.

    Snapshots the same render-friendly transcript the UI replays on resume, but drops any
    still-PENDING approval gate (that is live, clickable session state, not transcript — a public
    snapshot must never carry an actionable gate). 404 for an unknown chat; 400 when there is
    nothing to share yet (a brand-new, empty chat)."""
    session = app.state.sessions.get_or_load(sid)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    items = [it for it in _history_items(session) if it.get("role") != "approval_request"]
    # A chat is shareable only once it holds REAL conversation. Auto-run items are the pre-turn
    # read-only env probes (docker info, kind get clusters, kubectl get nodes, …) that EVERY new
    # session runs before the user has said anything — counting them let a brand-new, never-used
    # chat mint a public link to an otherwise-empty transcript. Gate on conversation items only;
    # the snapshot itself still carries the full trail below (parity with the resume/debug view).
    if not any(not (it.get("role") == "command" and it.get("auto_run")) for it in items):
        raise HTTPException(status_code=400, detail="nothing to share yet")
    # Two ordered mint passes. FIRST inline each report chart as a self-contained data: URI: this
    # needs the chart's REAL session_id to resolve the PNG off disk, then DROPS that id (so the
    # public snapshot and its offline page.html export render from the snapshot itself — no live
    # session dir, no session id in a URL, and charts survive deletion of the source session). Done
    # at mint so both the share and export inherit it.
    sessions_root = (get_settings().resolved_workspace_dir / "sessions").resolve()
    items = _inline_share_chart_artifacts(items, sessions_root=sessions_root)
    # THEN redact: a public share is UNAUTHENTICATED, so a single recursive scrub masks every
    # remaining server-internal absolute path + the owning session id (command trails, tool-call
    # inputs, and the NESTED report_path under runs[]/reports[]) to placeholders before the snapshot
    # is frozen — the inlined data: URIs carry none. Redaction runs LAST so it can't clobber the
    # chart session_id the inlining pass depends on.
    items = _redact_share_items(
        items, workspace_root=get_settings().resolved_workspace_dir, session_id=session.id,
        home=str(Path.home()))
    token = _share_store().create(
        items=items,
        title=session.title or "Shared conversation",
        created_at=session.created_at,
        source_session_id=session.id,
        usage={
            "input": session.total_input_tokens,
            "output": session.total_output_tokens,
            "cache_read": session.total_cache_read_tokens,
            "cache_write": session.total_cache_write_tokens,
            "total": session.session_total,
            # Context-window occupancy at share time — the "N ctx" meter the owner saw, frozen so
            # the shared/exported viewer can show the session's full token picture.
            "context": session.last_context_tokens,
        },
    )
    # Absolute public URL when SHARE_BASE_URL is set (shareable off-host); otherwise a relative
    # path the browser resolves against its own origin (see app.config.Settings.share_base_url).
    base = get_settings().share_link_base
    url = f"{base}/share/{token}" if base else f"/share/{token}"
    return JSONResponse({"token": token, "url": url})


@app.get("/api/share/{token}")
async def read_share(token: str) -> JSONResponse:
    """PUBLIC read-only transcript of a shared conversation — the unguessable token is the
    credential. 404 for a malformed/unknown/revoked token. Returns only the snapshot fields the
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


@app.get("/api/share/{token}/page.html")
async def read_share_page(token: str) -> Response:
    """PUBLIC self-contained, offline ``.html`` export of a shared conversation — the SPA + the
    frozen snapshot inlined into ONE dependency-free file (no external assets, no network on open).
    Host it anywhere, or open it from disk: the agent is never involved. 404 for a
    malformed/unknown/revoked token."""
    data = _share_store().read(token)
    if data is None:
        raise HTTPException(status_code=404, detail="shared conversation not found")
    html_doc = render_shared_chat(data, ui_dir=get_settings().ui_dir)
    return Response(
        content=html_doc,
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="shared-chat-{token}.html"'},
    )


@app.delete("/api/share/{token}")
def revoke_share(token: str) -> JSONResponse:
    """Revoke a share link: delete its snapshot so the link stops working. 404 if the snapshot
    is already gone."""
    # Reject a malformed token BEFORE it becomes a filesystem path — the delete store path guards
    # internally too; this keeps the cheap shape-check at the HTTP boundary.
    if not is_valid_token(token):
        raise HTTPException(status_code=404, detail="shared conversation not found")
    if not _share_store().delete(token):
        raise HTTPException(status_code=404, detail="shared conversation not found")
    return JSONResponse({"deleted": True, "token": token})


@app.get("/share/{token}")
async def share_page(token: str) -> FileResponse:
    """PUBLIC read-only viewer PAGE for a shared conversation. Serves the SPA shell exactly like
    ``/`` — the client (app.js) detects the ``/share/<token>`` path, fetches ``/api/share/<token>``,
    and renders the snapshot read-only (no WebSocket, no composer, no sidebar). We don't 404 here
    on an unknown token: the page itself shows a friendly "link not found / revoked" state from the
    JSON route, which keeps the SPA the single source of that messaging."""
    return FileResponse(get_settings().ui_dir / "index.html")


app.mount("/static", RevalidateStaticFiles(directory=str(get_settings().ui_dir)), name="static")


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()
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
    # Hermetic-test seam (the engine's own transport_factory, surfaced app-wide): tests
    # install a FakeTransport factory on app.state; unset (production) → the real CLI.
    loop = SdkNativeEngine(
        transport_factory=getattr(app.state, "sdk_transport_factory", None))

    def _queue_steer(text: str) -> None:
        """Queue a mid-turn user message for the running turn: onto the LiveTurn's steer queue
        (delivered as a follow-up query after the current ResultMessage — mid-turn query() is
        silently dropped by the CLI). Falls back to the legacy ``ctx.steer_messages`` list when
        no live turn is registered (a race with turn start/end; the engine drains that list
        too, and the finally backstop catches a turn that just ended)."""
        if engine_steer(session.id, text):
            return
        session.ctx.steer_messages.append(text)

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
        # without ever proposing a gate is bounded by the engine's MAX_TURNS.
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
            if not getattr(app.state, "provider_supported", True):
                # Unsupported LLM_PROVIDER (readiness already failed): record the turn so the
                # chat persists, surface a clear error, never crash.
                session.messages.append({"role": "user", "content": text})
                session.persist()
                await channel.emit("error", {"message": (
                    "unsupported LLM_PROVIDER: the SDK-native engine runs on the Claude "
                    "Agent SDK (set LLM_PROVIDER=claude-agent-sdk)")})
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
        extra LLM round-trip (engine.py injects the snapshot as a synthetic message). Wiring the
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
        # connect/reload before the next turn refreshes it. No limit: the model can change per-chat.
        "context_window": {
            "tokens": session.last_context_tokens,
        },
        # Per-session auto-approve state (persisted) so the UI toggle reflects THIS chat on
        # connect/reload/chat-switch. Defaults False; the client seeds the button from this.
        "auto_approve": session.auto_approve,
        # Per-session model/effort override (the picker), echoed RAW (may be null). A warm chat keeps
        # this ephemeral in-memory pick across reconnect, so a client with cleared/divergent
        # localStorage must adopt what THIS chat will actually run rather than show its own default.
        "model_override": session.model_override,
        "effort_override": session.effort_override,
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
                        _queue_steer(msg.text)
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
                    # start a concurrent turn on the same chat — queue the text so the running turn
                    # injects it as a real user message (engine.steer(), with ctx.steer_messages as
                    # the between-turns fallback). The agent thus receives it as soon as it finishes
                    # the current step and adapts, instead of it being dropped with "please wait".
                    # No echo frame: the UI already rendered the user's bubble optimistically, exactly
                    # as for a normal send (parity with the start-of-turn path below).
                    _queue_steer(msg.text)
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
            elif isinstance(msg, SetAutoApproveIn):
                # Toggle this chat's per-session auto-approve of command gates (the UI button).
                # Server-authoritative + persisted so it survives reconnect and re-seeds the
                # button via the `ready` frame. A live in-flight gate is unaffected (it already
                # parked); the toggle takes effect on the next command gate.
                session.auto_approve = msg.enabled
                session.persist()
            elif isinstance(msg, SetModelIn):
                # Switch this chat's Anthropic model + reasoning effort (the UI model picker).
                # Validate against RUNTIME truth: only the switchable agent-SDK provider has a
                # served catalog, and the id + effort must be in it (effort None for a no-effort
                # model like Haiku). On invalid → the same structured protocol `error` a malformed
                # frame gets, socket kept alive, PRIOR selection unchanged. On valid → store as
                # per-session, ephemeral state; it takes effect at the NEXT run_turn (never mid-turn,
                # never mutating the global provider). Not persisted to disk, but it survives reconnect
                # to the still-warm session (the `ready` frame re-echoes it); only a server restart /
                # eviction drops it back to the default.
                settings = get_settings()
                switchable = (settings.llm_provider or "claude-agent-sdk").lower() in AGENT_SDK_PROVIDERS
                info = (valid_selection(msg.model, msg.effort, settings.agent_sdk_model)
                        if switchable else None)
                if info is None:
                    await websocket.send_json(outbound(ws_events.ERROR, {
                        "message": f"unavailable model selection: {msg.model!r}"
                                   + (f" (effort {msg.effort!r})" if msg.effort else ""),
                        "kind": "protocol_error",
                    }))
                else:
                    session.model_override = info.id
                    session.effort_override = msg.effort
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
