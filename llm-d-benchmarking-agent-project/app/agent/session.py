"""Per-session conversation state and a disk-backed session manager.

Durable facts live in the cluster + workspace (the cluster is the source of truth), so a
session is mostly the conversation transcript plus the per-session ToolContext.

Each session's transcript is snapshotted to ``<workspace>/sessions/<id>/state.json`` so a
returning browser can reattach to a prior chat (WebSocket ``/ws?session=<id>``) and so the
UI can list recent chats in a sidebar. The manager can therefore reload a session from
disk, list all saved sessions, and delete one.
"""
from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import Settings
from app.security.allowlist import Allowlist
from app.security.runner import CommandRunner
from app.tools.context import ToolContext

# Session ids are uuid4 hex prefixes, but the id can arrive from the browser (the
# ``?session=`` query param), so validate it before building a filesystem path from it.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TITLE_MAX = 60


def _is_valid_id(sid: str | None) -> bool:
    return isinstance(sid, str) and bool(_ID_RE.match(sid))


def derive_title(messages: list[dict[str, Any]]) -> str:
    """A short, human title from the first user message (Claude-web style)."""
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "user":
            text = " ".join(str(m.get("content") or "").split())
            if text:
                return text[:_TITLE_MAX] + ("…" if len(text) > _TITLE_MAX else "")
    return "New chat"


# Keep the executed-command trail bounded so a long session's snapshot stays small.
_COMMANDS_MAX = 500


@dataclass
class Session:
    id: str
    ctx: ToolContext
    messages: list[dict[str, Any]] = field(default_factory=list)
    approved_plan: dict[str, Any] | None = None
    # Chronological trail of every command actually executed this session (read-only probes
    # included). Not part of the LLM message stream — purely for the UI's command/debug view,
    # replayed on resume. Bounded to the most recent _COMMANDS_MAX entries.
    commands: list[dict[str, Any]] = field(default_factory=list)
    # Decided approval gates (Approve/Reject of a command or a session plan), keyed to the
    # tool call they belong to. Not part of the LLM message stream — recorded so a resumed
    # chat can replay the approval cards + their ✓/✗ outcome in the transcript.
    approvals: list[dict[str, Any]] = field(default_factory=list)
    title: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def record_command(self, payload: dict[str, Any]) -> None:
        self.commands.append(payload)
        if len(self.commands) > _COMMANDS_MAX:
            del self.commands[: len(self.commands) - _COMMANDS_MAX]

    def record_approval(self, entry: dict[str, Any]) -> None:
        self.approvals.append(entry)
        if len(self.approvals) > _COMMANDS_MAX:
            del self.approvals[: len(self.approvals) - _COMMANDS_MAX]

    def persist(self) -> None:
        """Best-effort transcript snapshot for resumability/debugging."""
        try:
            self.ctx.workspace.mkdir(parents=True, exist_ok=True)
            if not self.title:
                self.title = derive_title(self.messages)
            self.updated_at = time.time()
            (self.ctx.workspace / "state.json").write_text(
                json.dumps(
                    {
                        "id": self.id,
                        "title": self.title,
                        "created_at": self.created_at,
                        "updated_at": self.updated_at,
                        "messages": self.messages,
                        "approved_plan": self.approved_plan,
                        "commands": self.commands[-_COMMANDS_MAX:],
                        "approvals": self.approvals[-_COMMANDS_MAX:],
                    },
                    indent=2,
                )
            )
        except OSError:
            pass


class SessionManager:
    def __init__(self, settings: Settings, allowlist: Allowlist, runner: CommandRunner,
                 run_semaphore=None, runs=None):
        self._settings = settings
        self._allowlist = allowlist
        self._runner = runner
        # Shared cap on concurrent heavy runs across every session (None = unlimited).
        self._run_semaphore = run_semaphore
        # Shared in-flight-run registry (Phase 16) so every session's ToolContext can drive the
        # cancel tool against any still-running background turn. None when lifecycle is unwired.
        self._runs = runs
        self._sessions: dict[str, Session] = {}

    @property
    def _root(self) -> Path:
        return self._settings.resolved_workspace_dir / "sessions"

    def _ctx_for(self, sid: str) -> ToolContext:
        return ToolContext(
            settings=self._settings,
            allowlist=self._allowlist,
            runner=self._runner,
            workspace=self._root / sid,
            run_semaphore=self._run_semaphore,
            runs=self._runs,
            session_id=sid,
        )

    def create(self) -> Session:
        sid = uuid.uuid4().hex[:12]
        session = Session(id=sid, ctx=self._ctx_for(sid))
        self._sessions[sid] = session
        return session

    def get(self, sid: str | None) -> Session | None:
        return self._sessions.get(sid) if sid else None

    def active_ids(self) -> set[str]:
        """Ids of sessions currently held in memory (loaded/live). Retention GC treats these
        as active and never prunes their on-disk scratch (Phase 18 active-run safety)."""
        return set(self._sessions)

    def load(self, sid: str | None) -> Session | None:
        """Reconstruct a session from its on-disk snapshot, or None if absent."""
        if not _is_valid_id(sid):
            return None
        try:
            data = json.loads((self._root / sid / "state.json").read_text())
        except (OSError, json.JSONDecodeError):
            return None
        session = Session(
            id=data.get("id", sid),
            ctx=self._ctx_for(data.get("id", sid)),
            messages=data.get("messages", []),
            approved_plan=data.get("approved_plan"),
            commands=data.get("commands", []),
            approvals=data.get("approvals", []),
            title=data.get("title", ""),
            created_at=data.get("created_at") or time.time(),
            updated_at=data.get("updated_at") or time.time(),
        )
        self._sessions[session.id] = session
        return session

    def get_or_load(self, sid: str | None) -> Session | None:
        """In-memory session if present, else rehydrated from disk."""
        return self.get(sid) or self.load(sid)

    def list(self) -> list[dict[str, Any]]:
        """Summaries of saved chats (no message bodies), newest first."""
        out: list[dict[str, Any]] = []
        if not self._root.exists():
            return out
        for d in self._root.iterdir():
            try:
                data = json.loads((d / "state.json").read_text())
            except (OSError, json.JSONDecodeError):
                continue  # not a saved session (or corrupt) — skip
            messages = data.get("messages", [])
            if not messages:
                continue  # never-used session (e.g. a throwaway healthz probe)
            out.append(
                {
                    "id": data.get("id", d.name),
                    "title": data.get("title") or derive_title(messages),
                    "created_at": data.get("created_at"),
                    "updated_at": data.get("updated_at"),
                    "message_count": len(messages),
                }
            )
        out.sort(key=lambda s: s.get("updated_at") or 0, reverse=True)
        return out

    def delete(self, sid: str | None) -> bool:
        """Forget a session and remove its workspace. True if it existed."""
        if not _is_valid_id(sid):
            return False
        self._sessions.pop(sid, None)
        d = self._root / sid
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
            return True
        return False
