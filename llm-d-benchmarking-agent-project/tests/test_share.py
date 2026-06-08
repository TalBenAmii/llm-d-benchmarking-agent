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


def test_pending_approval_is_filtered_from_snapshot(client_with_share):
    """A still-pending (live, clickable) gate must never leak into a public read-only snapshot."""
    client, _ = client_with_share
    s = _seed_chat(client, with_pending=True)
    token = client.post(f"/api/sessions/{s.id}/share").json()["token"]
    roles = [it["role"] for it in client.get(f"/api/share/{token}").json()["items"]]
    assert "approval_request" not in roles      # filtered out
    assert "approval_decision" in roles         # decided gates are kept (informative + safe)


def test_create_404_for_unknown_session(client_with_share):
    client, _ = client_with_share
    assert client.post("/api/sessions/does-not-exist/share").status_code == 404


def test_create_400_for_empty_chat(client_with_share):
    client, _ = client_with_share
    s = client.app.state.sessions.create()   # no messages
    s.persist()
    assert client.post(f"/api/sessions/{s.id}/share").status_code == 400


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


def test_publish_returns_the_public_link(client_with_share, monkeypatch):
    """POST /api/share/<token>/publish returns the secret-gist render URLs the dialog shows by
    default. The gist upload itself (gh) is stubbed — we assert the route's plumbing, not GitHub."""
    from app.packaging import gist_publish

    client, _ = client_with_share
    token = client.post(f"/api/sessions/{_seed_chat(client).id}/share").json()["token"]

    captured = {}

    def fake_publish(tok, *, workspace, ui_dir, snapshot=None):
        captured.update(token=tok, has_snapshot=snapshot is not None)
        return gist_publish.PublishResult(
            token=tok, gist_id="abc123",
            public_url="https://gist.githack.com/u/abc123/raw/x/chat.html",
            fallback_url="https://htmlpreview.github.io/?https://gist.githubusercontent.com/u/abc123/raw/x/chat.html",
            reused=False,
        )

    monkeypatch.setattr(gist_publish, "publish", fake_publish)
    r = client.post(f"/api/share/{token}/publish")
    assert r.status_code == 200
    body = r.json()
    assert body["public_url"].startswith("https://gist.githack.com/")
    assert body["gist_id"] == "abc123" and body["reused"] is False
    # The route loaded the snapshot once and handed it to the engine (no double read).
    assert captured == {"token": token, "has_snapshot": True}


def test_publish_503_when_gh_is_unavailable(client_with_share, monkeypatch):
    """When the GitHub CLI is missing/unauthenticated the engine raises; the route answers 503 with
    a machine ``reason`` so the dialog can fall back to the same-origin link."""
    from app.packaging import gist_publish

    client, _ = client_with_share
    token = client.post(f"/api/sessions/{_seed_chat(client).id}/share").json()["token"]

    def boom(*a, **k):
        raise gist_publish.GistPublishError("no gh here", reason="gh-missing")

    monkeypatch.setattr(gist_publish, "publish", boom)
    r = client.post(f"/api/share/{token}/publish")
    assert r.status_code == 503
    assert r.json()["reason"] == "gh-missing"


def test_publish_404_for_unknown_token(client_with_share):
    client, _ = client_with_share
    assert client.post("/api/share/" + "a" * 32 + "/publish").status_code == 404


def test_revoke_also_revokes_the_published_gist(client_with_share, monkeypatch):
    """Deleting the in-app link also deletes its published gist, so the public link dies with it.
    The gh delete is stubbed; we assert the route invokes the engine when a mapping exists."""
    from app.packaging import gist_publish

    client, settings = client_with_share
    token = client.post(f"/api/sessions/{_seed_chat(client).id}/share").json()["token"]

    # Simulate a previously-published gist by writing the token→gist mapping the engine reads.
    mapping = gist_publish.mapping_path(settings.resolved_workspace_dir, token)
    mapping.parent.mkdir(parents=True, exist_ok=True)
    mapping.write_text("gistabc\n", encoding="utf-8")

    revoked = {}
    monkeypatch.setattr(gist_publish, "revoke",
                        lambda tok, *, workspace: revoked.setdefault("token", tok))
    r = client.delete(f"/api/share/{token}")
    assert r.status_code == 200
    assert r.json()["gist_revoked"] is True
    assert revoked == {"token": token}


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
        # Publishing a public link is owner-only too (it creates content on the user's account) —
        # rejected before the route runs, so no gh call happens here.
        assert client.post(f"/api/share/{token}/publish").status_code == 401

        # Revoking requires the token (DELETE is not exempt).
        assert client.delete(f"/api/share/{token}").status_code == 401
        assert client.delete(f"/api/share/{token}", headers=auth).status_code == 200
    finally:
        app.dependency_overrides.pop(get_settings, None)
