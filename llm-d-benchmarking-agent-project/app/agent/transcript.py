"""Reconstruct a render-friendly transcript for replaying a resumed/shared chat in the UI.

This is **mechanism** (a pure transformation), not FastAPI: given a persisted ``Session`` it
flattens the stored LLM wire-format messages — plus the side-trail lists the session keeps
(decided/pending approval gates, executed commands, persisted card results) — into the same
flat item shape the live WebSocket event stream produces, so the client can reuse its renderers.

It lives beside ``cards.py`` (the other deterministic, knowledge-sourced render
mechanism) rather than inside ``app.main`` so it is testable through its own seam: the HTTP
routes (history replay on reconnect, public-share snapshotting) call ``history_items`` and stay
thin. ``app.main`` re-exports these under their original private names for the existing tests.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.agent.cards import build_results_card
from app.dig import find_last_json
from app.tools.analyze import report_locate
from app.tools.mcp_server import CARD_RESULT_TOOLS


def history_items(session) -> list[dict[str, Any]]:
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

    approvals_by_tc: dict[str | None, list[dict[str, Any]]] = {}
    for a in _dicts(getattr(session, "approvals", [])):
        approvals_by_tc.setdefault(a.get("tool_call_id"), []).append(a)
    pending_by_tc: dict[str | None, list[dict[str, Any]]] = {}
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
                        items.extend(card_result_items(cr))
                # Fallback: no persisted card result for a card-rendering tool call → re-derive
                # the card (+ its charts) from the tool_result kept in ``messages`` so it doesn't
                # degrade to bare tiles (see ``tool_results_by_tc`` above).
                elif tc.get("name") in CARD_RESULT_TOOLS:
                    fb = fallback_card_items(
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
            items.extend(card_result_items(cr))
    return items


def card_result_items(cr: dict[str, Any]) -> list[dict[str, Any]]:
    """Render items for one persisted card result: the ``tool_result`` (drives the report /
    analysis / env / etc. card) plus, when the analyzer produced one, the deterministic
    ``results_card`` — re-derived from the same result, exactly as the live stream emits it."""
    name, result = cr.get("name"), cr.get("result")
    out: list[dict[str, Any]] = [{"role": "tool_result", "name": name, "result": result}]
    card = build_results_card(name or "", result)
    if card is not None:
        out.append({"role": "results_card", "card": card})
    return out


def fallback_card_items(name: str | None, tr: dict[str, Any] | None, session) -> list[dict[str, Any]]:
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
    return card_result_items({"name": name, "result": result})
