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
from typing import Any, TypeGuard

from app.config import Settings
from app.security.allowlist import Allowlist
from app.security.runner import CommandRunner
from app.tools.context import ToolContext

# Session ids are uuid4 hex prefixes, but the id can arrive from the browser (the
# ``?session=`` query param), so validate it before building a filesystem path from it.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TITLE_MAX = 60

# The sidebar groups chats into one folder per namespace; chats with no namespace land in a
# folder under this sentinel key. "no_namespace" can never collide with a real namespace
# because RFC1123 namespace names forbid underscores. The frontend uses the same literal.
NO_NAMESPACE = "no_namespace"


def _is_valid_id(sid: str | None) -> TypeGuard[str]:
    return isinstance(sid, str) and bool(_ID_RE.match(sid))


def derive_title(messages: list[dict[str, Any]]) -> str:
    """A short, human title from the first REAL user message the human actually typed
    (Claude-web style).

    System-injected user messages are skipped two complementary ways so they never leak into the
    chat title / sidebar folder: (1) messages tagged ``synthetic: True`` (the environment
    pre-probe snapshot the agent loop injects as agent-only context); and (2) any message whose
    text is bracket-tagged ("[environment pre-probe …]", "[live catalog …]"), which also covers
    the live-catalog snapshot injected as a synthetic conversation message. The title therefore
    comes from what the person typed, not the injected context."""
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "user" and not m.get("synthetic"):
            text = " ".join(str(m.get("content") or "").split())
            if text and not text.startswith("["):
                return text[:_TITLE_MAX] + ("…" if len(text) > _TITLE_MAX else "")
    return "New chat"


def _effective_namespace(data: dict[str, Any]) -> str | None:
    """The namespace a saved session belongs to. Falls back to the approved plan's namespace
    so sessions persisted before the ``namespace`` field existed still group correctly."""
    return data.get("namespace") or (data.get("approved_plan") or {}).get("namespace")


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
    # STILL-PENDING (undecided) approval gates the turn is currently parked on. The Channel
    # records each one here (and removes it once decided/cancelled) and persists it, so an
    # in-flight gate survives a chat switch / pane eviction / channel eviction and can be
    # replayed in its transcript position on reconnect — not just while the in-memory Channel
    # happens to be alive. Keyed to its tool call (like ``approvals``) for ordered replay.
    in_flight_approvals: list[dict[str, Any]] = field(default_factory=list)
    title: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    # The Kubernetes namespace this chat belongs to — the sidebar groups chats into one folder
    # per namespace. None until configured (an approved SessionPlan fills it; see loop.py), so the
    # chat sits in the UI's "no_namespace" folder until then.
    namespace: str | None = None
    # Cumulative REAL token usage across every LLM call in this session (the loop adds each
    # call's normalized Usage here). Persisted so the header chip is correct on reload.
    total_input_tokens: int = 0          # freshly-processed (non-cached) input
    total_output_tokens: int = 0         # generated tokens
    total_cache_read_tokens: int = 0     # input served from cache
    total_cache_write_tokens: int = 0    # input written to cache (Anthropic only)
    # One-shot flag: the live catalog snapshot has been injected as a synthetic conversation
    # message (see app/agent/loop.py). PERSISTED — the injected message itself lives in
    # ``messages`` and is reloaded with the transcript, so a resumed chat must NOT inject a
    # second copy. Defaults False so pre-feature state.json files (no catalog message yet) get
    # one injected on their next turn.
    catalog_injected: bool = False
    # RUNTIME-ONLY (deliberately NOT persisted): the read-only environment snapshot the /ws
    # handler pre-probes in the background on a brand-new session. Scoped to the live process —
    # a resumed chat re-probes fresh, so persisting it would be stale.
    env_snapshot: dict[str, Any] | None = None
    # One-shot flag: the environment pre-probe snapshot has been injected as a synthetic turn
    # message (see app/agent/loop.py). PERSISTED — the injected synthetic message itself lives in
    # ``messages`` and is reloaded with the transcript, so a resumed chat must NOT inject a second
    # copy. Critically, were this NOT persisted it would reset to False on resume, and any later
    # pre-probe (or a stale-but-set env_snapshot) would re-inject the snapshot mid-transcript —
    # leaking the "[environment pre-probe …]" text into the rendered chat + sidebar title. Defaults
    # False so pre-feature state.json files (no snapshot injected yet) behave as before.
    prewarmed: bool = False

    @property
    def session_total(self) -> int:
        """Every token billed this session (input + output + cache read + cache write)."""
        return (self.total_input_tokens + self.total_output_tokens
                + self.total_cache_read_tokens + self.total_cache_write_tokens)

    def record_command(self, payload: dict[str, Any]) -> None:
        self.commands.append(payload)
        if len(self.commands) > _COMMANDS_MAX:
            del self.commands[: len(self.commands) - _COMMANDS_MAX]

    def record_approval(self, entry: dict[str, Any]) -> None:
        self.approvals.append(entry)
        if len(self.approvals) > _COMMANDS_MAX:
            del self.approvals[: len(self.approvals) - _COMMANDS_MAX]

    def record_in_flight_approval(self, entry: dict[str, Any]) -> None:
        """Track a still-undecided approval gate (by ``request_id``). Idempotent — a re-emit of
        the same gate does not duplicate the entry."""
        rid = entry.get("request_id")
        if any(a.get("request_id") == rid for a in self.in_flight_approvals):
            return
        self.in_flight_approvals.append(entry)

    def clear_in_flight_approval(self, request_id: str | None) -> None:
        """Drop a pending gate once it is decided or cancelled. No-op if already absent."""
        self.in_flight_approvals = [
            a for a in self.in_flight_approvals if a.get("request_id") != request_id
        ]

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
                        "namespace": self.namespace,
                        "commands": self.commands[-_COMMANDS_MAX:],
                        "approvals": self.approvals[-_COMMANDS_MAX:],
                        "in_flight_approvals": self.in_flight_approvals,
                        "total_input_tokens": self.total_input_tokens,
                        "total_output_tokens": self.total_output_tokens,
                        "total_cache_read_tokens": self.total_cache_read_tokens,
                        "total_cache_write_tokens": self.total_cache_write_tokens,
                        "catalog_injected": self.catalog_injected,
                        "prewarmed": self.prewarmed,
                    },
                    indent=2,
                )
            )
        except OSError:
            pass


# `SessionManager.list` (a method) shadows the builtin `list` inside the class body, so
# `-> list[str]` there resolves to the method, not the type. Alias it out here at module scope,
# where `list` is still the builtin, and annotate with the alias.
_SessionIds = list[str]


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
        session = Session(id=sid, ctx=self._ctx_for(sid),
                          namespace=self._settings.default_session_namespace)
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
            namespace=data.get("namespace"),
            commands=data.get("commands", []),
            approvals=data.get("approvals", []),
            in_flight_approvals=data.get("in_flight_approvals", []),
            title=data.get("title", ""),
            created_at=data.get("created_at") or time.time(),
            updated_at=data.get("updated_at") or time.time(),
            # Default to 0 when absent so pre-token-tracking state.json files still load.
            total_input_tokens=data.get("total_input_tokens", 0),
            total_output_tokens=data.get("total_output_tokens", 0),
            total_cache_read_tokens=data.get("total_cache_read_tokens", 0),
            total_cache_write_tokens=data.get("total_cache_write_tokens", 0),
            # Default False: a pre-feature snapshot has no catalog message, so let the next turn
            # inject one. (Once injected + persisted, a reloaded chat sees True and skips it.)
            catalog_injected=data.get("catalog_injected", False),
            # Default False so older state files (no key) load — but a session that already
            # injected the env pre-probe snapshot persists True, so a resume never re-injects it.
            prewarmed=data.get("prewarmed", False),
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
                    "namespace": _effective_namespace(data),
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

    def delete_namespace(self, namespace: str | None) -> _SessionIds:
        """Delete every saved chat in one sidebar folder (a whole namespace at once).

        ``namespace`` is the folder key the UI shows; the literal ``NO_NAMESPACE`` sentinel
        removes the chats that have no namespace set. Returns the ids removed so the caller can
        tear down any live turn per deleted session (mirrors what ``delete`` is paired with)."""
        target = namespace or NO_NAMESPACE
        deleted: _SessionIds = []
        if not self._root.exists():
            return deleted
        for d in self._root.iterdir():
            try:
                data = json.loads((d / "state.json").read_text())
            except (OSError, json.JSONDecodeError):
                continue  # not a saved session (or corrupt) — skip
            if (_effective_namespace(data) or NO_NAMESPACE) != target:
                continue
            if self.delete(data.get("id", d.name)):
                deleted.append(data.get("id", d.name))
        return deleted
