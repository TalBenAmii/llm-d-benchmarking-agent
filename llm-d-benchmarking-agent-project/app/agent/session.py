"""Per-session conversation state and a simple in-memory session manager.

Durable facts live in the cluster + workspace (the cluster is the source of truth), so a
session is mostly the conversation transcript plus the per-session ToolContext.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import Settings
from app.security.allowlist import Allowlist
from app.security.runner import CommandRunner
from app.tools.context import ToolContext


@dataclass
class Session:
    id: str
    ctx: ToolContext
    messages: list[dict[str, Any]] = field(default_factory=list)
    approved_plan: dict[str, Any] | None = None

    def persist(self) -> None:
        """Best-effort transcript snapshot for resumability/debugging."""
        try:
            self.ctx.workspace.mkdir(parents=True, exist_ok=True)
            (self.ctx.workspace / "state.json").write_text(
                json.dumps({"id": self.id, "messages": self.messages, "approved_plan": self.approved_plan}, indent=2)
            )
        except OSError:
            pass


class SessionManager:
    def __init__(self, settings: Settings, allowlist: Allowlist, runner: CommandRunner):
        self._settings = settings
        self._allowlist = allowlist
        self._runner = runner
        self._sessions: dict[str, Session] = {}

    def create(self) -> Session:
        sid = uuid.uuid4().hex[:12]
        workspace = self._settings.resolved_workspace_dir / "sessions" / sid
        ctx = ToolContext(
            settings=self._settings,
            allowlist=self._allowlist,
            runner=self._runner,
            workspace=workspace,
        )
        session = Session(id=sid, ctx=ctx)
        self._sessions[sid] = session
        return session

    def get(self, sid: str) -> Session | None:
        return self._sessions.get(sid)
