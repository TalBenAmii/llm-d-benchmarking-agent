"""Disk-backed session persistence — the substrate for WS resume and the sidebar."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.agent.session import SessionManager, derive_title
from app.config import Settings
from app.main import app
from app.security.allowlist import Allowlist
from app.security.runner import CommandRunner


def make_manager(tmp_path) -> SessionManager:
    settings = Settings(workspace_dir=tmp_path)
    allowlist = Allowlist.from_file(settings.allowlist_path)
    runner = CommandRunner(settings.repo_paths)
    return SessionManager(settings, allowlist, runner)


@pytest.fixture
def manager(tmp_path) -> SessionManager:
    return make_manager(tmp_path)


def _seed(manager: SessionManager, text: str = "deploy llm-d"):
    s = manager.create()
    s.messages.append({"role": "user", "content": text})
    s.persist()
    return s


def test_create_then_load_roundtrip(manager):
    s = _seed(manager, "hello")
    manager._sessions.clear()  # force a disk load
    loaded = manager.load(s.id)
    assert loaded is not None
    assert loaded.id == s.id
    assert loaded.messages == [{"role": "user", "content": "hello"}]
    assert loaded.title == "hello"


def test_get_or_load_prefers_memory(manager):
    s = _seed(manager)
    assert manager.get_or_load(s.id) is s


def test_load_unknown_returns_none(manager):
    assert manager.load("does-not-exist") is None
    assert manager.get_or_load(None) is None


def test_load_rejects_path_traversal(manager):
    assert manager.load("../../etc") is None
    assert manager.delete("a/b") is False


def test_list_newest_first_and_skips_empty(manager):
    a = _seed(manager, "first")
    b = _seed(manager, "second")
    a.messages.append({"role": "assistant", "content": "ok"})
    a.persist()  # bump a so it is most recent
    manager.create()  # an empty, never-used session must not appear
    listed = manager.list()
    assert [x["id"] for x in listed] == [a.id, b.id]
    assert "messages" not in listed[0]  # summaries only
    assert listed[0]["message_count"] == 2


def test_delete(manager):
    s = _seed(manager)
    assert manager.delete(s.id) is True
    assert manager.load(s.id) is None
    assert manager.delete(s.id) is False


def test_title_is_stable_across_saves(manager):
    s = _seed(manager, "my benchmark run please")
    original = s.title
    s.messages.append({"role": "user", "content": "follow-up"})
    s.persist()
    manager._sessions.clear()
    assert manager.load(s.id).title == original


def test_derive_title_truncates_long_text():
    t = derive_title([{"role": "user", "content": "x" * 100}])
    assert t.endswith("…") and len(t) <= 61


def test_derive_title_defaults_without_user_message():
    assert derive_title([{"role": "assistant", "content": "hi"}]) == "New chat"


# ---- REST endpoints that feed the sidebar ------------------------------------
def test_api_sessions_list_and_delete(tmp_path):
    manager = make_manager(tmp_path)
    s = _seed(manager, "deploy on kind")
    with TestClient(app) as client:
        client.app.state.sessions = manager  # back the app with our tmp store
        listed = client.get("/api/sessions").json()["sessions"]
        assert any(x["id"] == s.id and x["title"] == "deploy on kind" for x in listed)

        r = client.delete(f"/api/sessions/{s.id}")
        assert r.status_code == 200 and r.json()["deleted"] is True
        assert all(x["id"] != s.id for x in client.get("/api/sessions").json()["sessions"])


def test_api_delete_unknown_returns_404(tmp_path):
    manager = make_manager(tmp_path)
    with TestClient(app) as client:
        client.app.state.sessions = manager
        assert client.delete("/api/sessions/nope").status_code == 404
