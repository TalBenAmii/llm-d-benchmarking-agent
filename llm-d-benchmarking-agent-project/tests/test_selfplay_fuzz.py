"""Self-play fuzz / property harness for the agent's HTTP + WebSocket surface.

GOAL (todo item): "Make the agent interact with and view the application and randomly
'play with it' to find bugs." This is a deterministic, *seedable* randomized harness that
drives the real FastAPI app (``app.main:app``) exactly like a user would — over the real
``/ws`` WebSocket and the real ``/api/sessions`` + ``/api/namespaces`` HTTP routes — under
``SIMULATE=1`` with a scripted :class:`FuzzProvider` (no network, no cluster, no live LLM).

It is a PROPERTY test, not an example test: rather than asserting one scripted outcome, it
generates a random sequence of *valid-but-arbitrary* user operations (new chat, send a
message that triggers a scripted multi-tool turn, approve/reject a gate, cancel, disconnect
mid-turn, reconnect at a random point, switch between several concurrent chats, create/delete
namespaces, ping, send a malformed frame) and after EVERY action re-checks a set of
INVARIANTS. A bug in connection-resume / approval-persistence / state-isolation surfaces as a
failing seed with a printed action trace, reproducible by re-running that exact seed.

Why these substitutions (and only these):
  * ``SimRunner`` (``SIMULATE=1``) → every *mutating command* becomes a synthetic no-op, so a
    standup/run never touches a cluster, yet the agent loop still runs end-to-end. The upfront
    ``propose_session_plan`` approval gate is STILL gated (it is not a command), which is what
    gives us approve/reject/cancel paths to fuzz.
  * a tmp-dir-backed ``SessionManager`` so each fuzz run starts from an empty, isolated session
    store on disk — the "two sessions never share state" + "reload-from-disk matches history"
    invariants are then crisp and independent of any leftover chats.
  * ``FuzzProvider`` — a scripted LLM that, per turn, deterministically (seeded) plays EITHER a
    read-only turn or a turn that calls ``propose_session_plan`` then a mutating
    ``execute_llmdbenchmark`` standup (a real registry tool, valid against the real allowlist),
    so approval gates actually fire under the real loop.

Everything else is the REAL app: the real ``/ws`` handler, the real ``Channel`` (resume
buffer + pending-approval restore), the real ``SessionManager`` persistence, the real
inbound-frame validation, the real agent loop + tool dispatch + allowlist.
"""
from __future__ import annotations

import json
import os
import random
from contextlib import suppress
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.agent.session import NO_NAMESPACE, SessionManager
from app.config import Settings, get_settings
from app.llm.provider import AssistantTurn, ToolCall
from app.security.allowlist import Allowlist
from app.security.runner import SimRunner

# The bench repo must be present (the agent loop reads the live catalog for plan validation).
# In a worktree this is satisfied via REPOS_DIR (see tests/CLAUDE.md); skip cleanly otherwise.
pytestmark = pytest.mark.skipif(
    not get_settings().bench_repo.is_dir(), reason="bench repo not present"
)

# Background frames the env pre-probe / resource poller can stream onto a connection; they're
# benign noise when we're hunting for a specific protocol frame (mirrors test_ws.py).
_BACKGROUND_TYPES = {"command", "resource_stats", "output"}

# A bound on how many frames we drain per read so a missing terminal frame can't hang the test.
_DRAIN_CAP = 200


# --------------------------------------------------------------------------------------------
# Scripted provider whose turns are seeded read-only OR mutating (forces approval gates).
# --------------------------------------------------------------------------------------------

class FuzzProvider:
    """A scripted LLM. Each ``chat`` call returns the next turn for that session.

    Per session we cycle a deterministic 2-step "mutating" script (a session plan, then a
    mutating standup, then a closing text) OR a 1-step read-only script (just text). Which
    kind a given ``user_message`` gets is decided by the *driver* (seeded), which primes the
    script via :meth:`prime`. ``cache_key`` is the session id, so concurrent chats don't share
    a cursor.
    """

    # A valid mutating turn against the REAL registry + allowlist: propose a plan (gated), then
    # a standup (mutating command → no-op under SimRunner, but still drives the gate machinery).
    @staticmethod
    def _mutating_script(tag: str) -> list[AssistantTurn]:
        return [
            AssistantTurn(text="Here is the plan.", tool_calls=[ToolCall(
                f"plan-{tag}", "propose_session_plan", {
                    "use_case_summary": "tiny chat", "spec": "cicd/kind",
                    "namespace": "llmd-quickstart", "harness": "inference-perf",
                    "workload": "sanity_random.yaml", "expected_steps": ["standup"],
                })]),
            AssistantTurn(text="Standing up.", tool_calls=[ToolCall(
                f"standup-{tag}", "execute_llmdbenchmark", {
                    "subcommand": "standup", "spec": "cicd/kind",
                    "namespace": "llmd-quickstart", "flags": {"skip_smoketest": True},
                })]),
            AssistantTurn(text="All set.", tool_calls=[]),
        ]

    @staticmethod
    def _readonly_script(tag: str) -> list[AssistantTurn]:
        return [AssistantTurn(text=f"Read-only reply {tag}.", tool_calls=[])]

    def __init__(self) -> None:
        # cache_key (session id) -> queue of remaining AssistantTurns for the in-flight turn.
        self._queues: dict[str, list[AssistantTurn]] = {}
        self._counter = 0

    def prime(self, session_id: str, *, mutating: bool) -> None:
        """Queue the script for the NEXT user turn of ``session_id``."""
        self._counter += 1
        tag = str(self._counter)
        self._queues[session_id] = (
            self._mutating_script(tag) if mutating else self._readonly_script(tag)
        )

    async def chat(self, *, system, messages, tools, cache_key=None) -> AssistantTurn:
        q = self._queues.get(cache_key or "")
        if not q:
            # Script exhausted (or an unprimed background turn) → end the turn cleanly.
            return AssistantTurn(text="", tool_calls=[])
        return q.pop(0)


# --------------------------------------------------------------------------------------------
# Test app wiring: real `app`, but an isolated tmp workspace + SimRunner + FuzzProvider.
# --------------------------------------------------------------------------------------------

def _install_isolated_state(app, tmp_path) -> FuzzProvider:
    """Repoint the live app at an isolated, simulate-mode, empty session store + fuzz provider.

    The ``/ws`` handler and the ``/api/*`` routes read ``app.state.{sessions,runner,channels,
    running,provider}`` — swapping these (after TestClient startup) gives each fuzz run a clean,
    hermetic backend without reimporting the module. SimRunner makes every mutating command a
    no-op, so nothing here can touch a real cluster.
    """
    settings = Settings(
        _env_file=None,
        simulate=True,
        repos_dir=get_settings().repos_dir,   # real bench repo (catalog) via REPOS_DIR
        workspace_dir=tmp_path / "ws",        # isolated, empty session store on disk
        default_session_namespace=None,       # let plans/fuzz drive namespaces
    )
    allowlist = Allowlist.from_file(settings.allowlist_path)
    runner = SimRunner(settings.repo_paths, extra_env=settings.extra_subprocess_env)
    app.state.settings = settings
    app.state.allowlist = allowlist
    app.state.runner = runner
    app.state.channels = {}
    app.state.running = {}
    app.state.sessions = SessionManager(settings, allowlist, runner)
    provider = FuzzProvider()
    app.state.provider = provider
    app.state.provider_error = None
    return provider


def _read_protocol(ws, *, until: set[str] | None = None) -> list[dict[str, Any]]:
    """Drain frames until a frame whose type is in ``until`` (or the buffer is exhausted).

    Returns ALL frames seen (including background noise) so the caller can assert over them.
    Bounded by ``_DRAIN_CAP`` so a never-arriving terminal frame fails loudly, not hangs.
    """
    seen: list[dict[str, Any]] = []
    for _ in range(_DRAIN_CAP):
        ev = ws.receive_json()
        seen.append(ev)
        if until is not None and ev["type"] in until:
            return seen
    return seen


def _answer_any_approval(ws, frames: list[dict[str, Any]], *, approve: bool) -> int:
    """Answer every approval_request found in ``frames``; return how many we answered."""
    answered = 0
    for ev in frames:
        if ev["type"] == "approval_request":
            ws.send_json({
                "type": "approval", "request_id": ev["data"]["request_id"], "approved": approve,
            })
            answered += 1
    return answered


# --------------------------------------------------------------------------------------------
# Invariants — each returns a list of human-readable violations (empty == healthy).
# --------------------------------------------------------------------------------------------

_PREPROBE_MARKERS = ("environment pre-probe", "live catalog snapshot")


def _is_synthetic_leak(text: str | None) -> bool:
    return bool(text) and any(m in text for m in _PREPROBE_MARKERS)


def _check_no_synthetic_in_history(items: list[dict[str, Any]]) -> list[str]:
    """The env pre-probe / catalog synthetic snapshot must NEVER render as a user message."""
    problems: list[str] = []
    for it in items:
        if it.get("role") == "user" and _is_synthetic_leak(it.get("text")):
            problems.append(f"synthetic pre-probe leaked into history as a user message: {it!r}")
    return problems


def _read_persisted(app, sid: str) -> dict[str, Any] | None:
    """Read a session's on-disk ``state.json`` DIRECTLY (newest persisted snapshot).

    Deliberately NOT via ``SessionManager.load()``: that method has a registry side effect —
    it overwrites ``self._sessions[sid]`` with a fresh disk-loaded instance, which would EVICT
    the live in-memory session an open ``/ws`` connection's turn is still writing to (the
    handler captured its ``session`` reference at connect time). Calling it from an invariant
    would corrupt the very state under test. Reading the file is side-effect-free and tests
    exactly the persisted-consistency property we want.
    """
    root = app.state.sessions._root  # noqa: SLF001 — test introspection of the on-disk root
    try:
        return json.loads((root / sid / "state.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _check_session_invariants(app, sid: str) -> list[str]:
    """Per-session structural invariants over the PERSISTED session + its title/preview."""
    problems: list[str] = []
    s = app.state.sessions.get(sid)
    if s is None:
        return problems  # deleted out from under us — nothing to check
    # The sidebar TITLE must never be a synthetic pre-probe snapshot.
    if _is_synthetic_leak(s.title):
        problems.append(f"session {sid} title leaked synthetic pre-probe text: {s.title!r}")
    # in_flight_approvals are unique by request_id (no duplicate parked gates).
    rids = [a.get("request_id") for a in s.in_flight_approvals]
    if len(rids) != len(set(rids)):
        problems.append(f"session {sid} has duplicate in_flight_approvals: {rids}")
    # Persisted-state consistency: the on-disk transcript must be a CONSISTENT PREFIX of the
    # in-memory one — disk and memory agree on their common prefix, and disk never holds a
    # message the live object lacks. (The live object can be momentarily one un-persisted line
    # ahead while a turn runs; that's fine. Disk being AHEAD of memory, or disagreeing on a
    # shared index, signals a duplicate/stale session instance — a real state bug.)
    data = _read_persisted(app, sid)
    if data is None:
        if s.messages:
            problems.append(f"session {sid} has messages but no on-disk state.json")
    else:
        disk = [m.get("content") for m in data.get("messages", [])]
        mem = [m.get("content") for m in s.messages]
        if len(disk) > len(mem):
            problems.append(
                f"session {sid}: on-disk transcript ({len(disk)} msgs) is AHEAD of in-memory "
                f"({len(mem)} msgs) — stale/duplicate session instance"
            )
        elif disk != mem[: len(disk)]:
            problems.append(
                f"session {sid}: on-disk transcript diverges from the in-memory prefix"
            )
        # The persisted title must never be a synthetic snapshot either.
        if _is_synthetic_leak(data.get("title")):
            problems.append(f"session {sid}: persisted title leaked synthetic text: {data.get('title')!r}")
    return problems


def _check_isolation(app, sids: list[str]) -> list[str]:
    """State isolation: two distinct sessions must never share approval/command identity."""
    problems: list[str] = []
    seen_rid_owner: dict[str, str] = {}
    for sid in sids:
        s = app.state.sessions.get(sid)
        if s is None:
            continue
        for a in list(s.approvals) + list(s.in_flight_approvals):
            rid = a.get("request_id")
            if rid is None:
                continue
            if rid in seen_rid_owner and seen_rid_owner[rid] != sid:
                problems.append(
                    f"approval request_id {rid} shared across sessions "
                    f"{seen_rid_owner[rid]} and {sid} (state leak)"
                )
            seen_rid_owner[rid] = sid
    return problems


# --------------------------------------------------------------------------------------------
# The self-play driver.
# --------------------------------------------------------------------------------------------

class _Player:
    """Holds the live test state for one fuzz run and applies randomized actions.

    A "chat" here is a logical session the driver tracks (its id once minted). At most one
    ``/ws`` connection is open at a time (the app is "single active tab" — last connection
    wins), matching how a browser actually drives it; we model multiple chats by switching the
    single connection between sessions, plus brief reconnects.
    """

    def __init__(self, app, client: TestClient, provider: FuzzProvider, rng: random.Random):
        self.app = app
        self.client = client
        self.provider = provider
        self.rng = rng
        self.session_ids: list[str] = []          # all chats we've created (may be deleted)
        self.namespaces: set[str] = set()          # namespaces we've created chats under
        self.trace: list[str] = []                 # the action log (printed on failure)
        # The currently-attached ws context manager + its session id, or (None, None).
        self._ws = None
        self._cur_sid: str | None = None

    # --- connection management -----------------------------------------------------------
    def _open(self, sid: str | None, after_seq: int | None = None) -> dict[str, Any]:
        """Open a ``/ws`` connection and drain EXACTLY the handshake the handler is contracted
        to send after ``ready`` — no more, no less, so a later read never blocks on a frame that
        will not come.

        The handshake is determined by the ``ready`` payload itself (the handler's own gates):
          * brand-new chat (``resumed=False``)         → ``welcome`` then ``suggestions``;
          * full resume (``resumed=True, incremental=False``) → ``history``;
          * incremental resume (``incremental=True``)  → NOTHING guaranteed (just the live tail).
        We block ONLY for those guaranteed frames; any pending-approval / live-replay frames that
        follow are left for the calling action to drain (it knows whether to expect them).
        """
        self._close()
        q = ""
        if sid is not None:
            q = f"?session={sid}"
            if after_seq is not None:
                q += f"&after_seq={after_seq}"
        cm = self.client.websocket_connect(f"/ws{q}")
        ws = cm.__enter__()
        self._ws, self._ws_cm = ws, cm
        ready = _read_protocol(ws, until={"ready"})[-1]
        assert ready["type"] == "ready", f"first frame was not ready: {ready}"
        self._cur_sid = ready["data"]["session_id"]
        if sid is None:
            self.session_ids.append(self._cur_sid)
        resumed = ready["data"]["resumed"]
        incremental = ready["data"].get("resume", {}).get("incremental", False)
        if not resumed:
            self._invariant_frames(_read_protocol(ws, until={"suggestions"}))
        elif not incremental:
            self._invariant_frames(_read_protocol(ws, until={"history"}))
        # incremental: no guaranteed handshake frame → don't block here.
        return ready

    def _close(self) -> None:
        if self._ws is not None:
            with suppress(Exception):
                self._ws_cm.__exit__(None, None, None)
        self._ws, self._ws_cm, self._cur_sid = None, None, None

    # --- invariant hooks ------------------------------------------------------------------
    def _invariant_frames(self, frames: list[dict[str, Any]]) -> None:
        """Cheap per-frame checks: history frames must not leak the synthetic snapshot, and
        no frame may be an unexpected server error/500-shaped failure."""
        for ev in frames:
            if ev["type"] == "history":
                self._fail_if(_check_no_synthetic_in_history(ev["data"].get("items", [])))
            if ev["type"] == "error":
                kind = ev["data"].get("kind")
                # Protocol errors are EXPECTED for the malformed-frame action; anything else
                # (an agent/LLM error) is a real failure to surface.
                if kind != "protocol_error":
                    self._fail_if([f"unexpected server error frame: {ev['data']}"])

    def _pump(self, until: set[str], *, answer_gates: bool = True) -> list[dict[str, Any]]:
        """Drain frames until one whose type is in ``until``, running invariants on each frame
        and — crucially — IMMEDIATELY answering any ``approval_request`` it consumes.

        A gate's live card is a one-shot frame on the wire; if an action drained it WITHOUT
        answering, the server-side gate would stay parked while its card is gone from the stream,
        and a later read would block forever waiting for a re-emit that never comes. So every
        consumed approval card is resolved here (seeded approve/reject), keeping the server's
        parked-gate set and the client's view in lock-step. Returns all frames seen.
        """
        seen: list[dict[str, Any]] = []
        for _ in range(_DRAIN_CAP):
            ev = self._ws.receive_json()
            seen.append(ev)
            self._invariant_frames([ev])
            if answer_gates and ev["type"] == "approval_request":
                approve = self.rng.random() < 0.6
                self._ws.send_json({
                    "type": "approval", "request_id": ev["data"]["request_id"], "approved": approve,
                })
            if ev["type"] in until:
                break
        return seen

    def check_all(self) -> None:
        problems: list[str] = []
        for sid in self.session_ids:
            problems += _check_session_invariants(self.app, sid)
        problems += _check_isolation(self.app, self.session_ids)
        self._fail_if(problems)

    def _fail_if(self, problems: list[str]) -> None:
        if problems:
            raise AssertionError(
                "INVARIANT VIOLATION\n  seed actions:\n    "
                + "\n    ".join(self.trace)
                + "\n  problems:\n    " + "\n    ".join(problems)
            )

    # --- busy / parked-gate helpers ------------------------------------------------------
    def _is_busy(self, sid: str | None) -> bool:
        """True if ``sid`` has an in-flight turn (a live task, or a turn parked at a gate).

        Sending a ``user_message`` into a busy chat draws only a 'still working' error and no
        terminal frame, so the driver must not start a turn (or block reading for one) on it.
        """
        if sid is None:
            return False
        task = self.app.state.running.get(sid)
        if task is not None and not task.done():
            return True
        s = self.app.state.sessions.get(sid)
        return bool(s and s.in_flight_approvals)

    def _resolve_current_parked_gate(self) -> None:
        """If the current session is parked at a gate, pump to ``done`` — ``_pump`` answers the
        re-emitted approval card en route — so the background turn finishes and the connection is
        left idle + clean for the next action."""
        if self._ws is None or not self._is_busy(self._cur_sid):
            return
        self.trace.append("  resolve_parked")
        self._pump(until={"done"})

    # --- actions --------------------------------------------------------------------------
    def act_new_chat(self) -> None:
        self.trace.append("new_chat")
        self._open(None)

    def act_send_message(self) -> None:
        if self._ws is None:
            self.act_new_chat()
        sid = self._cur_sid
        assert sid is not None
        # Don't start a turn on a chat that's already busy (parked/running): resolve it first.
        if self._is_busy(sid):
            self._resolve_current_parked_gate()
            return
        mutating = self.rng.random() < 0.5
        self.provider.prime(sid, mutating=mutating)
        self.trace.append(f"send_message(sid={sid[:6]}, mutating={mutating})")
        self._ws.send_json({"type": "user_message", "text": "benchmark a tiny chat model"})
        # Pump the WHOLE turn to `done`: a mutating turn parks at the plan gate, which `_pump`
        # answers (seeded approve/reject); on approve the standup (a SimRunner no-op) runs to
        # done, on reject the loop feeds the refusal back and the closing turn ends — either way
        # we reach `done`, with no parked gate left dangling.
        self._pump(until={"done"})

    def act_reconnect_midturn(self) -> None:
        """Start a mutating turn, drop the socket at the parked gate, reconnect, and assert the
        pending approval is RE-EMITTED (BUG-G regression) — then resolve it."""
        self.act_new_chat()
        sid = self._cur_sid
        assert sid is not None
        self.provider.prime(sid, mutating=True)
        self.trace.append(f"reconnect_midturn(sid={sid[:6]})")
        # Capture the last seq before dropping, for an incremental-resume reconnect. Read raw
        # (no auto-answer) — we must reach the gate WITHOUT resolving it so it stays parked.
        last_seq = 0
        self._ws.send_json({"type": "user_message", "text": "benchmark a tiny chat model"})
        frames = _read_protocol(self._ws, until={"approval_request"})
        for ev in frames:
            if "seq" in ev:
                last_seq = max(last_seq, ev["seq"])
        self._invariant_frames(frames)
        if not any(f["type"] == "approval_request" for f in frames):
            return  # turn finished without parking (shouldn't happen for a mutating script)
        # Drop WITHOUT answering — the parked gate must survive on session.in_flight_approvals.
        self._close()
        s = self.app.state.sessions.get(sid)
        self._fail_if([] if s and s.in_flight_approvals
                      else [f"parked gate not persisted on session {sid}"])
        # Reconnect — sometimes incrementally (after_seq), sometimes a full rebuild.
        use_cursor = self.rng.random() < 0.5
        ready = self._open(sid, after_seq=last_seq if use_cursor else None)
        assert ready["data"]["resumed"] is True
        # The pending approval MUST re-surface live (re-emit) — drain raw until we see it (the
        # BUG-G regression assertion); we do not auto-answer so we can assert on the card itself.
        post = _read_protocol(self._ws, until={"approval_request"})
        self._invariant_frames(post)
        self._fail_if([] if any(f["type"] == "approval_request" for f in post)
                      else [f"pending approval NOT re-emitted on reconnect to {sid}"])
        # Resolve it (seeded) — the gate must clear from in_flight_approvals + be recorded. The
        # SAME parked turn then continues live to `done`; pump the rest so the socket ends clean.
        approve = self.rng.random() < 0.6
        _answer_any_approval(self._ws, post[-1:], approve=approve)
        self.trace.append(f"  resolve(approve={approve})")
        self._pump(until={"done"})
        s = self.app.state.sessions.get(sid)
        self._fail_if([] if s and not s.in_flight_approvals
                      else [f"gate not cleared after resolve on {sid}: {s.in_flight_approvals if s else None}"])
        self._fail_if([] if s and any(a.get("approved") == approve for a in s.approvals)
                      else [f"decision not recorded after resolve on {sid}"])

    def act_switch_chat(self) -> None:
        if not self.session_ids:
            self.act_new_chat()
            return
        sid = self.rng.choice(self.session_ids)
        self.trace.append(f"switch_chat(sid={sid[:6]})")
        # If the chosen chat was deleted, the handler mints a fresh one — tolerate that.
        with suppress(Exception):
            self._open(sid)
            # Switching to a chat parked at a gate re-surfaces its approval; resolve it so the
            # background turn finishes and the connection is left idle for the next action.
            self._resolve_current_parked_gate()

    def act_cancel(self) -> None:
        if self._ws is None:
            return
        self.trace.append("cancel")
        self._ws.send_json({"type": "cancel"})  # idempotent no-op if nothing running
        self._ws.send_json({"type": "ping"})
        self._invariant_frames(_read_protocol(self._ws, until={"pong"}))

    def act_ping(self) -> None:
        if self._ws is None:
            return
        # A non-turn action must not consume a parked chat's re-emitted approval card without
        # answering it (that would orphan the gate and block a later read); resolve it first.
        self._resolve_current_parked_gate()
        self.trace.append("ping")
        self._ws.send_json({"type": "ping"})
        frames = _read_protocol(self._ws, until={"pong"})
        self._fail_if([] if frames and frames[-1]["type"] == "pong"
                      else ["ping did not get a pong"])

    def act_malformed(self) -> None:
        """A malformed frame must be rejected with a protocol error and the socket must SURVIVE
        (a following ping still gets a pong)."""
        if self._ws is None:
            return
        self._resolve_current_parked_gate()
        self.trace.append("malformed")
        kind = self.rng.choice(["bad_type", "non_json", "bare_list", "missing_field", "binary"])
        if kind == "bad_type":
            self._ws.send_json({"type": "totally_bogus", "x": 1})
        elif kind == "non_json":
            self._ws.send_text("not json {{{")
        elif kind == "bare_list":
            self._ws.send_json(["not", "an", "object"])
        elif kind == "missing_field":
            self._ws.send_json({"type": "approval", "approved": True})  # no request_id
        else:
            self._ws.send_bytes(b"\x00\x01\x02")
        frames = _read_protocol(self._ws, until={"error"})
        self._fail_if(
            [] if frames and frames[-1]["type"] == "error"
            and frames[-1]["data"].get("kind") == "protocol_error"
            else [f"malformed {kind} frame not rejected as protocol_error: {frames[-1:]}"]
        )
        # Socket must still be usable.
        self.act_ping()

    def act_list_namespaces(self) -> None:
        """Exercise the sidebar-folder surface: list sessions (must always 200) and record the
        namespace folders the server reports, so delete_namespace can target REAL folders. A
        chat's folder is ``no_namespace`` until a plan is approved, then the plan's namespace
        (``llmd-quickstart`` for our mutating script)."""
        self.trace.append("list_sessions")
        resp = self.client.get("/api/sessions")
        self._fail_if([] if resp.status_code == 200 else [f"/api/sessions returned {resp.status_code}"])
        if resp.status_code == 200:
            for s in resp.json().get("sessions", []):
                self.namespaces.add(s.get("namespace") or NO_NAMESPACE)

    def act_delete_namespace(self) -> None:
        """Delete a whole sidebar folder (every chat in one namespace). Targets a REAL folder the
        server reports when possible, so the live-session teardown path is exercised (deleting a
        folder must tear down each chat's runtime — a turn parked in the background included)."""
        choices = sorted(self.namespaces) + [NO_NAMESPACE, "llmd-quickstart"]
        ns = self.rng.choice(choices)
        self.trace.append(f"delete_namespace({ns})")
        # If the currently-attached chat lives in this folder, close first (delete tears down its
        # runtime; reading on a torn-down socket would block).
        cur = self.app.state.sessions.get(self._cur_sid) if self._cur_sid else None
        if cur is not None and (cur.namespace or NO_NAMESPACE) == ns:
            self._close()
        # Must NEVER 500; 404 (no chats in that folder) is a legitimate outcome.
        resp = self.client.delete(f"/api/namespaces/{ns}")
        self._fail_if(
            [] if resp.status_code in (200, 404)
            else [f"DELETE /api/namespaces/{ns} returned {resp.status_code}"]
        )

    def act_delete_session(self) -> None:
        if not self.session_ids:
            return
        sid = self.rng.choice(self.session_ids)
        self.trace.append(f"delete_session(sid={sid[:6]})")
        # If we're currently attached to it, close first (deleting tears down its runtime).
        if self._cur_sid == sid:
            self._close()
        resp = self.client.delete(f"/api/sessions/{sid}")
        self._fail_if(
            [] if resp.status_code in (200, 404)
            else [f"DELETE /api/sessions/{sid} returned {resp.status_code}"]
        )

    # --- the dispatch table ---------------------------------------------------------------
    def step(self) -> None:
        actions = [
            (self.act_new_chat, 3),
            (self.act_send_message, 6),
            (self.act_reconnect_midturn, 3),
            (self.act_switch_chat, 4),
            (self.act_cancel, 2),
            (self.act_ping, 2),
            (self.act_malformed, 2),
            (self.act_list_namespaces, 1),
            (self.act_delete_namespace, 1),
            (self.act_delete_session, 1),
        ]
        pool: list[Any] = []
        for fn, weight in actions:
            pool += [fn] * weight
        self.rng.choice(pool)()
        # After every action: full invariant sweep (the property under test).
        self.check_all()

    def finish(self) -> None:
        self._close()


# --------------------------------------------------------------------------------------------
# The parametrized property test.
# --------------------------------------------------------------------------------------------

# Fixed seeds → reproducible. A failure prints its seed; re-run that seed to reproduce exactly.
_SEEDS = [1, 7, 13, 16, 42, 101, 777, 2024]
_ACTIONS_PER_RUN = 24  # ~10-40 band; kept modest so the whole parametrization stays a few seconds


@pytest.mark.parametrize("seed", _SEEDS)
def test_selfplay_fuzz(seed: int, tmp_path) -> None:
    """Drive the real app with a seeded random action sequence; assert invariants after each.

    Deterministic: identical seed → identical action sequence → identical assertions. No wall
    clock, no real randomness, no network/cluster/LLM. A violation raises with the seed's full
    action trace so the failing sequence is reproducible.
    """
    from app.main import app

    rng = random.Random(seed)
    with TestClient(app) as client:
        provider = _install_isolated_state(app, tmp_path)
        player = _Player(app, client, provider, rng)
        try:
            for _ in range(_ACTIONS_PER_RUN):
                player.step()
        finally:
            player.finish()


@pytest.mark.skipif(
    os.environ.get("FUZZ_SOAK") != "1", reason="opt-in soak: set FUZZ_SOAK=1 to run"
)
def test_selfplay_fuzz_soak(tmp_path) -> None:
    """Opt-in longer soak (40 actions × more seeds). Behind a skip so the default suite stays
    fast; set FUZZ_SOAK=1 when you want a deeper pass."""
    from app.main import app

    for seed in range(20):
        rng = random.Random(seed)
        with TestClient(app) as client:
            provider = _install_isolated_state(app, tmp_path / f"s{seed}")
            player = _Player(app, client, provider, rng)
            try:
                for _ in range(40):
                    player.step()
            finally:
                player.finish()
