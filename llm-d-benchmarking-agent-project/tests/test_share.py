"""Share a chat via a read-only link (ChatGPT-style).

Two layers, both hermetic (no cluster, no network):

* the :class:`~app.storage.share.ShareStore` mechanism — token minting, the filesystem-safe
  token guard, write/read/delete — on a tmp workspace;
* the HTTP surface over the REAL ``app.main`` wiring with a tmp workspace (the
  ``client_with_share`` fixture mirrors ``test_artifacts.client_with_workspace``): minting an
  immutable snapshot, the PUBLIC read viewer, revocation, the not-found/empty paths, the
  pending-approval filter, and — with auth ON — that the public GET viewer bypasses Bearer auth
  while minting/revoking stay gated.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import app.main as main_mod
from app.config import get_settings
from app.storage.share import ShareStore, _is_valid_token

TOKEN = "super-secret-token"


# ---------------------------------------------------------------------------
# Pure mechanism: ShareStore (no HTTP).
# ---------------------------------------------------------------------------


def test_token_shape_guard():
    assert _is_valid_token("0123456789abcdef" * 2) is True   # 32 lowercase hex chars
    assert _is_valid_token("a" * 32) is True                 # all-hex (a is a hex digit)
    assert _is_valid_token("g" * 32) is False                # g is not a hex digit
    assert _is_valid_token("A" * 32) is False                # uppercase is not matched
    assert _is_valid_token("a" * 31) is False                # too short
    assert _is_valid_token("../../etc/passwd") is False       # traversal can never match the hex shape
    assert _is_valid_token(None) is False


def test_create_read_delete_roundtrip(tmp_path):
    store = ShareStore(tmp_path)
    items = [{"role": "user", "text": "hi"}, {"role": "assistant", "text": "hello"}]
    token = store.create(items=items, title="greeting", created_at=1.0, source_session_id="sess1")
    assert _is_valid_token(token)

    data = store.read(token)
    assert data is not None
    assert data["title"] == "greeting"
    assert data["items"] == items
    assert data["source_session_id"] == "sess1"
    assert data["shared_at"] >= data["created_at"]

    assert store.delete(token) is True
    assert store.read(token) is None          # gone after revoke
    assert store.delete(token) is False       # idempotent — already gone


def test_read_rejects_malformed_or_unknown_token(tmp_path):
    store = ShareStore(tmp_path)
    assert store.read("not-a-token") is None
    assert store.read("a" * 32) is None        # well-formed-but-unknown reads as missing, not error
    assert store.delete("../escape") is False


def test_each_create_mints_a_fresh_token(tmp_path):
    store = ShareStore(tmp_path)
    t1 = store.create(items=[], title="x", created_at=0.0, source_session_id="s")
    t2 = store.create(items=[], title="x", created_at=0.0, source_session_id="s")
    assert t1 != t2


def test_create_writes_atomically_leaving_no_tmp(tmp_path):
    """Regression: create() must write via temp-then-replace (like the sibling stores), so a
    concurrent reader never sees a half-written file and no ``.tmp`` is left behind."""
    store = ShareStore(tmp_path)
    token = store.create(items=[{"role": "assistant", "text": "hi"}], title="t",
                         created_at=0.0, source_session_id="s")
    shares = tmp_path / "shares"
    assert (shares / f"{token}.json").is_file()
    assert list(shares.glob("*.tmp")) == []          # no leftover temp file
    assert store.read(token)["items"][0]["text"] == "hi"


# ---------------------------------------------------------------------------
# HTTP surface over the real app, pointed at a tmp workspace.
# ---------------------------------------------------------------------------


@pytest.fixture
def client_with_share(tmp_path, monkeypatch):
    """A TestClient whose app resolves its workspace (sessions + shares) to tmp_path. Yields
    ``(client, settings)`` so a test can also flip auth on via dependency_overrides."""
    settings = get_settings().model_copy(update={"workspace_dir": tmp_path / "ws"})
    monkeypatch.setattr(main_mod, "get_settings", lambda: settings)
    with TestClient(main_mod.app) as client:
        yield client, settings


def _seed_chat(client, *, title="deploy a tiny model", with_pending=False):
    """Create a session with a realistic transcript (user + assistant tool call + a decided
    approval + an executed command), optionally parked on a still-pending approval gate."""
    s = client.app.state.sessions.create()
    s.messages = [
        {"role": "user", "content": title},
        {"role": "assistant", "content": "On it.",
         "tool_calls": [{"id": "tc1", "name": "run_setup", "input": {"spec": "cicd/kind"}}]},
    ]
    s.approvals = [{"tool_call_id": "tc1", "request_id": "r1", "kind": "command",
                    "payload": {"argv": ["echo", "hi"]}, "approved": True}]
    s.commands = [{"tool_call_id": "tc1", "text": "echo hi", "argv": ["echo", "hi"],
                   "mode": "read_only", "auto_run": True}]
    if with_pending:
        s.in_flight_approvals = [{"tool_call_id": "tc1", "request_id": "r2", "kind": "command",
                                  "payload": {"argv": ["kubectl", "delete", "ns", "x"]}}]
    # Realistic token counters, so the frozen snapshot's `usage` block is exercised end-to-end.
    s.total_input_tokens = 1200
    s.total_output_tokens = 340
    s.total_cache_read_tokens = 5600
    s.total_cache_write_tokens = 700
    s.last_context_tokens = 8100
    s.title = title
    s.persist()
    return s


def test_create_then_public_read_then_revoke(client_with_share):
    client, _ = client_with_share
    s = _seed_chat(client)

    r = client.post(f"/api/sessions/{s.id}/share")
    assert r.status_code == 200
    body = r.json()
    token = body["token"]
    assert _is_valid_token(token)
    # Default (SHARE_BASE_URL unset): a relative path — the browser prepends its own origin.
    assert body["url"] == f"/share/{token}"


def test_revoke_rejects_malformed_token_cleanly(client_with_share):
    """Regression: a malformed (non-32-hex) token must 404 cleanly, never becoming a
    filesystem path (validated up front)."""
    client, _ = client_with_share
    assert client.delete("/api/share/not-a-valid-token").status_code == 404
    assert client.delete("/api/share/" + "Z" * 32).status_code == 404   # right length, wrong alphabet


def test_share_base_url_mints_absolute_public_link(tmp_path, monkeypatch):
    """With SHARE_BASE_URL configured, the mint route returns an ABSOLUTE link carrying the
    public host (a friend can open it off-host); the trailing slash is normalized."""
    settings = get_settings().model_copy(
        update={"workspace_dir": tmp_path / "ws", "share_base_url": "https://demo.example.com/"}
    )
    monkeypatch.setattr(main_mod, "get_settings", lambda: settings)
    with TestClient(main_mod.app) as client:
        s = _seed_chat(client)
        body = client.post(f"/api/sessions/{s.id}/share").json()
        token = body["token"]
        assert body["url"] == f"https://demo.example.com/share/{token}"   # absolute, slash trimmed
        # The public read route still works regardless of how the link was formatted.
        assert client.get(f"/api/share/{token}").status_code == 200

    # The PUBLIC transcript route returns the rendered snapshot + metadata, and withholds the
    # owning session id.
    pub = client.get(f"/api/share/{token}")
    assert pub.status_code == 200
    payload = pub.json()
    assert payload["title"] == "deploy a tiny model"
    assert "source_session_id" not in payload
    roles = [it["role"] for it in payload["items"]]
    assert roles[0] == "user" and "assistant" in roles and "tool_call" in roles
    assert "approval_decision" in roles and "command" in roles

    # Revoke (owner-only) → the link stops working.
    assert client.delete(f"/api/share/{token}").status_code == 200
    assert client.get(f"/api/share/{token}").status_code == 404
    assert client.delete(f"/api/share/{token}").status_code == 404   # already gone


def test_snapshot_is_immutable_after_more_messages(client_with_share):
    """ChatGPT semantics: messages sent AFTER sharing don't change the shared copy."""
    client, _ = client_with_share
    s = _seed_chat(client)
    token = client.post(f"/api/sessions/{s.id}/share").json()["token"]
    before = client.get(f"/api/share/{token}").json()["items"]

    # The owner keeps chatting.
    s.messages.append({"role": "user", "content": "now scale it to 500 users"})
    s.persist()

    after = client.get(f"/api/share/{token}").json()["items"]
    assert after == before          # the frozen snapshot is unchanged
    assert all(it.get("text") != "now scale it to 500 users" for it in after)


def test_share_snapshot_carries_full_session_usage(client_with_share):
    """The frozen `usage` block carries the WHOLE token picture — cumulative totals including
    cache_write, plus the context-window occupancy at share time — so the shared/exported viewer
    can show the session's token spend exactly as the owner saw it."""
    client, _ = client_with_share
    s = _seed_chat(client)
    token = client.post(f"/api/sessions/{s.id}/share").json()["token"]
    usage = client.get(f"/api/share/{token}").json()["usage"]
    assert usage == {
        "input": 1200, "output": 340, "cache_read": 5600, "cache_write": 700,
        "total": 1200 + 340 + 5600 + 700,   # session_total sums all four counters
        "context": 8100,
    }


def test_pending_approval_is_filtered_from_snapshot(client_with_share):
    """A still-pending (live, clickable) gate must never leak into a public read-only snapshot."""
    client, _ = client_with_share
    s = _seed_chat(client, with_pending=True)
    token = client.post(f"/api/sessions/{s.id}/share").json()["token"]
    roles = [it["role"] for it in client.get(f"/api/share/{token}").json()["items"]]
    assert "approval_request" not in roles      # filtered out
    assert "approval_decision" in roles         # decided gates are kept (informative + safe)


def _seed_chat_with_report_card(client, *, session_id_marker):
    """A chat that ran a benchmark: a card-rendering tool result persisted in ``card_results``
    whose ``report_path`` is the absolute host path UNDER the per-session dir (so it embeds the
    session id) and a not-found probe's ``searched`` roots — exactly what locate_and_parse_report
    persists. ``session_id_marker`` is a distinctive token planted in those paths."""
    s = client.app.state.sessions.create()
    s.messages = [
        {"role": "user", "content": "show me the benchmark results"},
        {"role": "assistant", "content": "Here they are.",
         "tool_calls": [{"id": "tc1", "name": "locate_and_parse_report", "input": {}}]},
    ]
    report_path = f"/home/operator/ws/sessions/{session_id_marker}/runs/benchmark_report_v0.2.json"
    s.card_results = [{
        "tool_call_id": "tc1", "name": "locate_and_parse_report",
        "result": {
            "found": True,
            "report_path": report_path,                                  # absolute internal path
            "searched": [f"/home/operator/ws/sessions/{session_id_marker}"],
            "summary": {"model": "tiny", "requests_total": 120},         # render-relevant — keep
            "charts": [{"path": "runs/latency.png", "title": "latency"}],
        },
    }]
    s.title = "benchmark results"
    s.persist()
    return s


def test_share_snapshot_redacts_internal_report_paths(client_with_share):
    """BUG: a card-rendering tool result (locate_and_parse_report) is persisted with its absolute
    ``report_path`` (under <sessions_root>/<session_id>/…) and search roots, and _history_items
    replays the FULL result into the snapshot. A public, UNAUTHENTICATED share therefore disclosed
    the host filesystem layout AND the owning session id — the very id the snapshot deliberately
    withholds. The share path must scrub those path-bearing keys while keeping the render-relevant
    summary/charts."""
    client, _ = client_with_share
    marker = "0123456789abcdef0123456789abcdef"        # stands in for the owning session id
    s = _seed_chat_with_report_card(client, session_id_marker=marker)

    token = client.post(f"/api/sessions/{s.id}/share").json()["token"]

    # The PUBLIC JSON transcript must not carry the internal paths or the embedded session id.
    body = client.get(f"/api/share/{token}").json()
    raw = json.dumps(body)
    assert "report_path" not in raw, "report_path (absolute host path) leaked into the public share"
    assert marker not in raw, "owning session id leaked via an internal path in the share snapshot"
    assert s.id not in raw, "the real session id must never appear in the public snapshot"
    # The render-relevant card data survives the scrub (the report still renders).
    tr = next(it for it in body["items"] if it.get("role") == "tool_result")
    assert tr["result"]["summary"]["requests_total"] == 120
    assert tr["result"]["charts"]                      # session-relative charts kept

    # The offline single-file export inlines the SAME snapshot — it must be scrubbed too.
    page = client.get(f"/api/share/{token}/page.html").text
    assert "report_path" not in page and marker not in page


def test_create_404_for_unknown_session(client_with_share):
    client, _ = client_with_share
    assert client.post("/api/sessions/does-not-exist/share").status_code == 404


def test_create_400_for_empty_chat(client_with_share):
    client, _ = client_with_share
    s = client.app.state.sessions.create()   # no messages
    s.persist()
    assert client.post(f"/api/sessions/{s.id}/share").status_code == 400


def test_share_survives_corrupt_session_transcript(client_with_share):
    """BUG: a corrupt/forward-incompatible state.json whose ``messages`` (or any of the
    persisted command/approval/card-result trails) carries a NON-DICT element must NOT 500 the
    share route — ``_history_items`` reconstructs from disk with no per-element type check
    (same class as BUG-011/020-023), so a torn/hand-edited element used to escape as an uncaught
    AttributeError/TypeError. The render now coerces non-dict shapes away and degrades to the
    rows it can render instead of crashing the whole transcript / share / WS reconnect."""
    client, _ = client_with_share
    s = _seed_chat(client)
    # Inject malformed elements across every list _history_items walks: a non-dict message, a
    # scalar tool_calls, a non-dict tool_calls element, and a torn tool_results block.
    s.messages = [
        {"role": "user", "content": "hi"},
        "TORN-NON-DICT-MESSAGE",
        {"role": "assistant", "tool_calls": "not-a-list"},
        {"role": "assistant", "tool_calls": ["scalar-tool-call"]},
        {"role": "tool_results", "results": 5},
        {"role": "tool_results", "results": ["scalar-result"]},
    ]
    s.approvals = ["torn-approval"]
    s.in_flight_approvals = ["torn-pending"]
    s.commands = ["torn-command"]
    s.card_results = ["torn-card-result"]
    s.persist()
    r = client.post(f"/api/sessions/{s.id}/share")
    # The real user message is still shareable, so this mints (200) rather than 400 — and never 500.
    assert r.status_code == 200, f"corrupt transcript must not 500 the share route (got {r.status_code})"
    items = client.get(f"/api/share/{r.json()['token']}").json()["items"]
    assert any(it.get("role") == "user" and it.get("text") == "hi" for it in items)


def test_read_404_for_malformed_token(client_with_share):
    client, _ = client_with_share
    assert client.get("/api/share/not-a-valid-token").status_code == 404


def test_page_html_export_is_a_self_contained_download(client_with_share):
    """GET /api/share/<token>/page.html returns ONE self-contained .html (offline single-file
    export) as an attachment — the SPA + snapshot inlined, no /static/ refs, no external fonts."""
    client, _ = client_with_share
    token = client.post(f"/api/sessions/{_seed_chat(client).id}/share").json()["token"]

    r = client.get(f"/api/share/{token}/page.html")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "attachment" in r.headers.get("content-disposition", "")
    assert f"shared-chat-{token}.html" in r.headers["content-disposition"]
    body = r.text
    assert "window.__LLMD_SHARED__ = " in body          # snapshot embedded
    assert "/static/app.js" not in body                 # app.js inlined, not referenced
    assert "/static/styles.css" not in body             # css inlined, not referenced
    assert "fonts.googleapis.com" not in body           # external fonts stripped


def test_page_html_404_for_unknown_or_malformed_token(client_with_share):
    client, _ = client_with_share
    assert client.get("/api/share/" + "a" * 32 + "/page.html").status_code == 404   # unknown
    assert client.get("/api/share/not-a-token/page.html").status_code == 404         # malformed


def test_share_page_serves_the_spa_shell(client_with_share):
    """/share/<token> serves the same SPA HTML as / — the client renders the snapshot read-only.
    We don't 404 the page on an unknown token (the SPA shows that state from the JSON route)."""
    client, _ = client_with_share
    r = client.get("/share/" + "a" * 32)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "/static/app.js" in r.text


def test_public_get_bypasses_auth_but_minting_and_revoking_stay_gated(client_with_share):
    """With Bearer auth ON: the public viewer GETs bypass auth (the token is the credential),
    but POST-create and DELETE-revoke require the app token."""
    client, settings = client_with_share
    app = main_mod.app
    enabled = settings.model_copy(update={"auth_enabled": True, "auth_token": TOKEN})
    app.dependency_overrides[get_settings] = lambda: enabled
    auth = {"Authorization": f"Bearer {TOKEN}"}
    try:
        s = _seed_chat(client)
        # Minting requires the token.
        assert client.post(f"/api/sessions/{s.id}/share").status_code == 401
        r = client.post(f"/api/sessions/{s.id}/share", headers=auth)
        assert r.status_code == 200
        token = r.json()["token"]

        # PUBLIC viewer: reachable with NO token (incl. the single-file .html export).
        assert client.get(f"/api/share/{token}").status_code == 200
        assert client.get(f"/share/{token}").status_code == 200
        assert client.get(f"/api/share/{token}/page.html").status_code == 200
        # A normal API route still 401s without the token (proves the bypass is scoped).
        assert client.get("/api/sessions").status_code == 401

        # Revoking requires the token (DELETE is not exempt).
        assert client.delete(f"/api/share/{token}").status_code == 401
        assert client.delete(f"/api/share/{token}", headers=auth).status_code == 200
    finally:
        app.dependency_overrides.pop(get_settings, None)
