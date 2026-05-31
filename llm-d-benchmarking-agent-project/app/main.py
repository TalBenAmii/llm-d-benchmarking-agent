"""FastAPI app: serves the chat UI and hosts the agent over a WebSocket.

The backend is the security + secrets boundary. The browser only ever exchanges chat
text, structured events, and Approve/Reject decisions — never API keys, never raw commands.
"""
from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
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


app.mount("/static", StaticFiles(directory=str(get_settings().ui_dir)), name="static")


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()
    session = app.state.sessions.create()
    loop = AgentLoop(app.state.provider) if app.state.provider else None

    pending: dict[str, asyncio.Future] = {}
    busy = {"value": False}

    async def emit(event_type: str, payload: dict[str, Any]) -> None:
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
                await emit("error", {"message": f"LLM provider not configured: {app.state.provider_error}"})
                await emit("done", {})
                return
            await loop.run_turn(session, text, emit=emit, request_approval=request_approval)
        except Exception as exc:  # noqa: BLE001
            await emit("error", {"message": f"agent error: {exc}"})
            await emit("done", {})
        finally:
            busy["value"] = False

    await emit("ready", {"session_id": session.id})
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
