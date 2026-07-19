"""Reusable real-app driver — the shared mechanism behind both the deterministic self-play
fuzzer (``tests/platform/test_selfplay_fuzz.py``) and the LLM-driven exploratory bug-hunter
(``tests/eval/explorer.py``).

This module was **factored out** of ``tests/platform/test_selfplay_fuzz.py`` (the hard-won WS-handshake /
gate-resume / state-isolation invariants live here now). It is pure MECHANISM — no policy, no
LLM. The fuzzer imports it and selects actions with a seeded RNG; the bug-hunter imports the
SAME machinery and selects actions with an LLM (or the seeded RNG fallback). Because the driver
is byte-identical, the fuzzer's behavior is unchanged after the move.

What lives here:
  * :class:`SdkFuzzScripts` — a scripted-turn primer whose per-turn script is read-only OR
    mutating (forces real approval gates under the real engine), primed by the driver.
  * :func:`install_isolated_state` — repoints the live app at an isolated, ``SIMULATE=1``,
    empty session store + the scripted transport factory (no network / cluster / live LLM).
  * the INVARIANT BATTERY (:func:`check_no_synthetic_in_history`, :func:`check_session_invariants`,
    :func:`check_isolation`) — these ARE the deterministic bug ORACLE the bug-hunter uses; a hit
    is a real finding with no false positives.
  * :class:`Player` — connection management (handshake-exact ``_open``/``_close``), frame
    pumping (``_pump`` answers parked gates en route), and the full ACTION VOCABULARY
    (``act_new_chat`` … ``act_delete_session``). Each action returns nothing; ``check_all``
    runs the full invariant sweep.

Nothing here is a test; it spends NO quota. ``tests/platform/test_selfplay_fuzz.py`` re-exports the
public names below so its module-level references keep working unchanged.
"""
from __future__ import annotations

import json
import random
from contextlib import suppress
from typing import Any

from fastapi.testclient import TestClient

from app.agent.session import NO_NAMESPACE, SessionManager
from app.config import Settings, get_settings
from app.security.policy import CommandPolicy
from app.security.runner import SimRunner
from tests._scripted import AssistantTurn, ScriptedTransports, ToolCall

# Background frames the env pre-probe / resource poller can stream onto a connection; they're
# benign noise when we're hunting for a specific protocol frame (mirrors test_ws.py).
_BACKGROUND_TYPES = {"command", "resource_stats", "output"}

# A bound on how many frames we drain per read so a missing terminal frame can't hang the test.
_DRAIN_CAP = 200


# --------------------------------------------------------------------------------------------
# Scripted turns, seeded read-only OR mutating (forces approval gates).
# --------------------------------------------------------------------------------------------

# The shared per-turn scripts, valid against the REAL registry + policy: a plan (gated), then a
# mutating standup (a SimRunner no-op that still drives the gate machinery). SdkFuzzScripts
# renders them as FakeTransport wire turns for the engine — one source of truth for the fuzz
# behavior.
_PLAN_INPUT = {
    "use_case_summary": "tiny chat", "spec": "cicd/kind",
    "namespace": "llmd-quickstart", "harness": "inference-perf",
    "workload": "sanity_random.yaml", "expected_steps": ["standup"],
}
_STANDUP_INPUT = {
    "subcommand": "standup", "spec": "cicd/kind",
    "namespace": "llmd-quickstart", "flags": {"skip_smoketest": True},
}


class SdkFuzzScripts(ScriptedTransports):
    """The fuzzer's scripted-turn primer over the shared :class:`ScriptedTransports` FIFO.

    :meth:`prime` enqueues the NEXT user turn's script (FIFO — the driver is strictly
    sequential: one connection, prime-then-send); ``next_transport`` (inherited) is installed
    as ``app.state.sdk_transport_factory``, so the engine's connect-per-turn pops exactly one
    script per user turn. An unprimed turn gets a clean empty reply. The generous response
    timeout exists because a parked approval gate holds its tools/call control request open
    for as long as the fuzzer leaves the card unanswered (it may switch chats first)."""

    def __init__(self) -> None:
        super().__init__(response_timeout=300.0)
        self._counter = 0

    def prime(self, session_id: str, *, mutating: bool) -> None:
        """Queue the script for the NEXT user turn of ``session_id``."""
        self._counter += 1
        tag = str(self._counter)
        if mutating:
            self.add_turns(
                AssistantTurn(text="Here is the plan.", tool_calls=[
                    ToolCall(f"plan-{tag}", "propose_session_plan", _PLAN_INPUT)]),
                AssistantTurn(text="Standing up.", tool_calls=[
                    ToolCall(f"standup-{tag}", "execute_llmdbenchmark", _STANDUP_INPUT)]),
                AssistantTurn(text="All set."),
            )
        else:
            self.add_turns(AssistantTurn(text=f"Read-only reply {tag}."))


# --------------------------------------------------------------------------------------------
# Test app wiring: real `app`, but an isolated tmp workspace + SimRunner + scripted turns.
# --------------------------------------------------------------------------------------------

def install_isolated_state(app, tmp_path) -> SdkFuzzScripts:
    """Repoint the live app at an isolated, simulate-mode, empty session store + scripted turns.

    The ``/ws`` handler and the ``/api/*`` routes read ``app.state.{sessions,runner,channels,
    running}`` — swapping these (after TestClient startup) gives each fuzz run a clean,
    hermetic backend without reimporting the module. SimRunner makes every mutating command a
    no-op, so nothing here can touch a real cluster. The returned :class:`SdkFuzzScripts`
    primer feeds the engine via ``app.state.sdk_transport_factory`` (the /ws handler's
    hermetic seam).
    """
    settings = Settings(
        _env_file=None,
        simulate=True,
        repos_dir=get_settings().repos_dir,   # real bench repo (catalog) via REPOS_DIR
        workspace_dir=tmp_path / "ws",        # isolated, empty session store on disk
        default_session_namespace=None,       # let plans/fuzz drive namespaces
    )
    policy = CommandPolicy.from_file(settings.command_policy_path)
    runner = SimRunner(settings.repo_paths, extra_env=settings.extra_subprocess_env)
    app.state.settings = settings
    app.state.policy = policy
    app.state.runner = runner
    app.state.channels = {}
    app.state.running = {}
    app.state.sessions = SessionManager(settings, policy, runner)
    return SdkFuzzScripts().install(app)


def read_protocol(ws, *, until: set[str] | None = None) -> list[dict[str, Any]]:
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


def answer_any_approval(ws, frames: list[dict[str, Any]], *, approve: bool) -> int:
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
# These ARE the deterministic bug ORACLE: a non-empty return is a real finding (no false
# positives — they assert proven structural truths about the persisted/in-memory state).
# --------------------------------------------------------------------------------------------

_PREPROBE_MARKERS = ("environment pre-probe", "live catalog snapshot")


def _is_synthetic_leak(text: str | None) -> bool:
    return bool(text) and any(m in text for m in _PREPROBE_MARKERS)


def check_no_synthetic_in_history(items: list[dict[str, Any]]) -> list[str]:
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


def check_session_invariants(app, sid: str) -> list[str]:
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
        # auto_approve is server-authoritative + persisted SYNCHRONOUSLY on toggle (the handler
        # sets it then calls session.persist() before reading the next frame), so the on-disk flag
        # must match the in-memory one. A divergence means the persisted snapshot belongs to a
        # DIFFERENT session instance than the live one — a stale/duplicate-session state leak.
        if "auto_approve" in data and bool(data["auto_approve"]) != bool(s.auto_approve):
            problems.append(
                f"session {sid}: persisted auto_approve diverges from in-memory "
                f"({data['auto_approve']} vs {s.auto_approve})"
            )
    return problems


def check_isolation(app, sids: list[str]) -> list[str]:
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

class Player:
    """Holds the live test state for one fuzz run and applies randomized actions.

    A "chat" here is a logical session the driver tracks (its id once minted). At most one
    ``/ws`` connection is open at a time (the app is "single active tab" — last connection
    wins), matching how a browser actually drives it; we model multiple chats by switching the
    single connection between sessions, plus brief reconnects.
    """

    def __init__(self, app, client: TestClient, provider: SdkFuzzScripts,
                 rng: random.Random):
        self.app = app
        self.client = client
        self.provider = provider
        self.rng = rng
        self.session_ids: list[str] = []          # all chats we've created (may be deleted)
        self.namespaces: set[str] = set()          # namespaces we've created chats under
        self.trace: list[str] = []                 # the action log (printed on failure)
        # The most recent `ready` frame _open drained — lets a deterministic caller assert
        # WHICH resume path (incremental after_seq vs full rebuild) a reconnect actually took.
        self.last_ready: dict[str, Any] | None = None
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
        ready = read_protocol(ws, until={"ready"})[-1]
        assert ready["type"] == "ready", f"first frame was not ready: {ready}"
        self.last_ready = ready
        self._cur_sid = ready["data"]["session_id"]
        if sid is None:
            self.session_ids.append(self._cur_sid)
        resumed = ready["data"]["resumed"]
        incremental = ready["data"].get("resume", {}).get("incremental", False)
        if not resumed:
            self._invariant_frames(read_protocol(ws, until={"suggestions"}))
        elif not incremental:
            self._invariant_frames(read_protocol(ws, until={"history"}))
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
                self._fail_if(check_no_synthetic_in_history(ev["data"].get("items", [])))
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
            problems += check_session_invariants(self.app, sid)
        problems += check_isolation(self.app, self.session_ids)
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

        Sending a ``user_message`` into a busy chat now STEERS the in-flight turn (the text is
        queued and picked up at its next step) rather than starting a new turn — so it yields no
        fresh terminal frame of its own, and the driver must not start a turn (or block reading for
        one) on it.
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
        # done, on reject the engine feeds the refusal back and the closing turn ends — either way
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
        frames = read_protocol(self._ws, until={"approval_request"})
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
        post = read_protocol(self._ws, until={"approval_request"})
        self._invariant_frames(post)
        self._fail_if([] if any(f["type"] == "approval_request" for f in post)
                      else [f"pending approval NOT re-emitted on reconnect to {sid}"])
        # Resolve it (seeded) — the gate must clear from in_flight_approvals + be recorded. The
        # SAME parked turn then continues live to `done`; pump the rest so the socket ends clean.
        approve = self.rng.random() < 0.6
        answer_any_approval(self._ws, post[-1:], approve=approve)
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
        self._invariant_frames(read_protocol(self._ws, until={"pong"}))

    def act_ping(self) -> None:
        if self._ws is None:
            return
        # A non-turn action must not consume a parked chat's re-emitted approval card without
        # answering it (that would orphan the gate and block a later read); resolve it first.
        self._resolve_current_parked_gate()
        self.trace.append("ping")
        self._ws.send_json({"type": "ping"})
        frames = read_protocol(self._ws, until={"pong"})
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
        frames = read_protocol(self._ws, until={"error"})
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

    def act_set_auto_approve(self) -> None:
        """Toggle this chat's per-session auto-approve (the UI button → the ``set_auto_approve``
        frame the deterministic fuzzer never sent). It is server-authoritative + persisted on
        toggle, so after the round-trip the on-disk ``auto_approve`` must match the in-memory flag —
        the invariant battery checks that. The socket must also survive the (responseless) toggle."""
        if self._ws is None:
            self.act_new_chat()
        # A non-turn action must not silently consume a parked chat's re-emitted approval card.
        self._resolve_current_parked_gate()
        enabled = self.rng.random() < 0.5
        self.trace.append(f"set_auto_approve({enabled})")
        self._ws.send_json({"type": "set_auto_approve", "enabled": enabled})
        # The toggle emits no frame; round-trip a ping so the set + synchronous persist is fully
        # applied before the next read / invariant sweep, and to prove the socket is still live.
        self._ws.send_json({"type": "ping"})
        self._invariant_frames(read_protocol(self._ws, until={"pong"}))

    def act_list_jobs(self) -> None:
        """Read the orchestrator REST mirror (``GET /api/jobs``) for a namespace folder — a surface
        the fuzzer never touched. It is a READ-ONLY mirror contracted to NEVER 5xx (it soft-degrades
        to ``available: false`` when no cluster is reachable), so any non-200 is a real contract
        break. ``namespace`` is required by the route, so we always pass one."""
        ns = self.rng.choice(sorted(self.namespaces) + [NO_NAMESPACE, "llmd-quickstart"])
        self.trace.append(f"list_jobs({ns})")
        resp = self.client.get("/api/jobs", params={"namespace": ns})
        self._fail_if([] if resp.status_code == 200
                      else [f"/api/jobs?namespace={ns} returned {resp.status_code}"])

    def act_reopen_after_delete(self) -> None:
        """Delete a chat, then immediately reconnect to its now-dead id. The handler must mint a
        FRESH session (``resumed=False``) — never resurrect the deleted on-disk state — so a
        ``resumed=True`` here is a real state-corruption bug. The minted id is tracked so later
        actions/invariants can target it."""
        if not self.session_ids:
            self.act_new_chat()
            return
        sid = self.rng.choice(self.session_ids)
        self.trace.append(f"reopen_after_delete(sid={sid[:6]})")
        if self._cur_sid == sid:
            self._close()  # deleting tears down its runtime; reading a torn-down socket would block
        resp = self.client.delete(f"/api/sessions/{sid}")
        self._fail_if(
            [] if resp.status_code in (200, 404)
            else [f"DELETE /api/sessions/{sid} returned {resp.status_code}"]
        )
        # Reconnect to the dead id: get_or_load fails → a brand-new session is minted (resumed=False).
        ready = self._open(sid)
        self._fail_if(
            [] if ready["data"]["resumed"] is False
            else [f"reopened deleted session {sid}: resume returned resumed=True — stale state not cleared"]
        )
        minted = ready["data"]["session_id"]
        if minted not in self.session_ids:
            self.session_ids.append(minted)

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
            (self.act_set_auto_approve, 1),
            (self.act_list_jobs, 1),
            (self.act_reopen_after_delete, 1),
        ]
        pool: list[Any] = []
        for fn, weight in actions:
            pool += [fn] * weight
        self.rng.choice(pool)()
        # After every action: full invariant sweep (the property under test).
        self.check_all()

    def finish(self) -> None:
        self._close()
