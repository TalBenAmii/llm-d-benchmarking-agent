"""FastAPI app: serves the chat UI and hosts the agent over a WebSocket.

The backend is the security + secrets boundary. The browser only ever exchanges chat
text, structured events, and Approve/Reject decisions — never API keys, never raw commands.
"""
from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.agent.loop import AgentLoop
from app.agent.session import SessionManager
from app.config import get_settings
from app.llm.provider import get_provider
from app.security.allowlist import Allowlist
from app.security.runner import CommandRunner

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    app.state.allowlist = Allowlist.from_file(settings.allowlist_path)
    app.state.runner = CommandRunner(settings.repo_paths, extra_env=settings.extra_subprocess_env)
    app.state.sessions = SessionManager(settings, app.state.allowlist, app.state.runner)
    # Build the provider tolerantly: a missing key shouldn't crash the server.
    try:
        app.state.provider = get_provider(settings)
        app.state.provider_error = None
    except Exception as exc:  # noqa: BLE001
        app.state.provider = None
        app.state.provider_error = str(exc)
    yield


app = FastAPI(title="llm-d Benchmarking Assistant", lifespan=lifespan)


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


@app.get("/api/sessions")
async def list_sessions() -> JSONResponse:
    """Recent chats for the sidebar (summaries only, newest first)."""
    return JSONResponse({"sessions": app.state.sessions.list()})


@app.delete("/api/sessions/{sid}")
async def delete_session(sid: str) -> JSONResponse:
    if not app.state.sessions.delete(sid):
        raise HTTPException(status_code=404, detail="session not found")
    return JSONResponse({"deleted": True, "id": sid})


def _history_items(session) -> list[dict[str, Any]]:
    """Render-friendly transcript for replaying a resumed chat in the UI.

    The stored ``messages`` are in LLM wire-format; flatten them into the same
    shape the live event stream produces so the client can reuse its renderers.
    """
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
    return items


app.mount("/static", StaticFiles(directory=str(get_settings().ui_dir)), name="static")


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()
    # ``/ws?session=<id>`` reattaches to a saved chat (page reload / sidebar click);
    # an unknown or missing id just mints a fresh session.
    requested = websocket.query_params.get("session")
    session = app.state.sessions.get_or_load(requested) if requested else None
    resumed = session is not None
    if session is None:
        session = app.state.sessions.create()
    loop = AgentLoop(app.state.provider) if app.state.provider else None

    pending: dict[str, asyncio.Future] = {}
    busy = {"value": False}

    async def emit(event_type: str, payload: dict[str, Any]) -> None:
        # Record the executed-command trail on the session so a resumed chat can replay it
        # in the command/debug view (kept out of the LLM message stream).
        if event_type == "command":
            session.record_command(payload)
        with contextlib.suppress(Exception):
            await websocket.send_json({"type": event_type, "data": payload})

    async def request_approval(kind: str, payload: dict[str, Any]) -> bool:
        rid = uuid.uuid4().hex[:8]
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        pending[rid] = fut
        await emit("approval_request", {"request_id": rid, "kind": kind, "payload": payload})
        try:
            return bool(await fut)
        finally:
            pending.pop(rid, None)

    async def run_turn(text: str) -> None:
        busy["value"] = True
        try:
            if loop is None:
                # No LLM, but still record the turn so the chat persists / resumes.
                session.messages.append({"role": "user", "content": text})
                session.persist()
                await emit("error", {"message": f"LLM provider not configured: {app.state.provider_error}"})
                await emit("done", {})
                return
            await loop.run_turn(session, text, emit=emit, request_approval=request_approval)
        except Exception as exc:  # noqa: BLE001
            await emit("error", {"message": f"agent error: {exc}"})
            await emit("done", {})
        finally:
            busy["value"] = False

    await emit("ready", {"session_id": session.id, "resumed": resumed})
    if resumed:
        await emit("history", {"items": _history_items(session), "commands": session.commands})
    turn_task: asyncio.Task | None = None

    try:
        while True:
            msg = await websocket.receive_json()
            mtype = msg.get("type")
            if mtype == "user_message":
                if busy["value"]:
                    await emit("error", {"message": "still working on the previous request — please wait."})
                    continue
                turn_task = asyncio.create_task(run_turn(msg.get("text", "")))
            elif mtype == "approval":
                rid = msg.get("request_id")
                fut = pending.get(rid)
                if fut and not fut.done():
                    fut.set_result(bool(msg.get("approved")))
            elif mtype == "ping":
                await emit("pong", {})
    except WebSocketDisconnect:
        pass
    finally:
        for fut in pending.values():
            if not fut.done():
                fut.set_result(False)
        if turn_task and not turn_task.done():
            turn_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await turn_task
