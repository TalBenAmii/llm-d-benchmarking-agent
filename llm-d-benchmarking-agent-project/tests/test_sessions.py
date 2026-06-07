"""Disk-backed session persistence — the substrate for WS resume and the sidebar."""
from __future__ import annotations

import json

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


def test_derive_title_skips_synthetic_pre_probe(tmp_path):
    # Regression (TODO #6 probe-leak): the injected environment pre-probe snapshot is a
    # synthetic user message the human never typed. derive_title must skip it and pick the
    # first REAL user message, so it never leaks into the sidebar chat title.
    messages = [
        {"role": "user", "synthetic": True,
         "content": "[environment pre-probe — read-only snapshot …] {\"tools\": {}}"},
        {"role": "user", "content": "deploy a tiny model on kind"},
    ]
    assert derive_title(messages) == "deploy a tiny model on kind"


def test_derive_title_synthetic_only_falls_back(tmp_path):
    # If the ONLY user message is synthetic (e.g. a chat persisted after pre-probe but before
    # the human sent anything), the title stays the clean default — never the snapshot.
    messages = [{"role": "user", "synthetic": True,
                 "content": "[environment pre-probe …] {\"tools\": {}}"}]
    assert derive_title(messages) == "New chat"


def test_synthetic_pre_probe_never_leaks_into_namespace_folder_title(tmp_path):
    # End-to-end: a session whose messages start with a synthetic pre-probe must show its real
    # title (not the snapshot) in the /api/sessions list the sidebar folders render from.
    manager = make_manager(tmp_path)
    s = manager.create()
    s.messages.append({"role": "user", "synthetic": True,
                       "content": "[environment pre-probe …] {\"kind_clusters\": {}}"})
    s.messages.append({"role": "user", "content": "run a quick benchmark"})
    s.persist()
    manager._sessions.clear()
    with TestClient(app) as client:
        client.app.state.sessions = manager
        listed = client.get("/api/sessions").json()["sessions"]
        row = next(x for x in listed if x["id"] == s.id)
        assert row["title"] == "run a quick benchmark"
        assert "pre-probe" not in row["title"]


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


# ---- namespace: the sidebar's folder key -------------------------------------
def test_create_stamps_default_namespace(manager):
    # conftest sets DEFAULT_SESSION_NAMESPACE=test, so every session the suite mints is born
    # "test" — keeping test chats out of the real list and exercising the folder feature.
    assert manager.create().namespace == "test"


def test_in_flight_approval_survives_persist_load(manager):
    # Regression (TODO #7 approval-state): a still-PENDING (undecided) approval gate must be
    # persisted so it survives a chat switch / pane eviction / channel eviction and can be
    # replayed in its transcript position on reconnect — not held only in-memory on the Channel.
    s = _seed(manager, "parked at a gate")
    s.record_in_flight_approval({"tool_call_id": "c1", "request_id": "r1",
                                 "kind": "command", "payload": {"command": "kubectl apply"}})
    # Idempotent: re-recording the same request_id (e.g. a reemit) must not duplicate it.
    s.record_in_flight_approval({"tool_call_id": "c1", "request_id": "r1",
                                 "kind": "command", "payload": {"command": "kubectl apply"}})
    s.persist()
    manager._sessions.clear()  # force a disk load
    loaded = manager.load(s.id)
    assert loaded is not None
    assert len(loaded.in_flight_approvals) == 1
    assert loaded.in_flight_approvals[0]["request_id"] == "r1"
    assert loaded.in_flight_approvals[0]["tool_call_id"] == "c1"


def test_clear_in_flight_approval_removes_only_target(manager):
    s = _seed(manager, "two gates")
    s.record_in_flight_approval({"tool_call_id": "c1", "request_id": "r1", "kind": "command", "payload": {}})
    s.record_in_flight_approval({"tool_call_id": "c2", "request_id": "r2", "kind": "session_plan", "payload": {}})
    s.clear_in_flight_approval("r1")
    assert [a["request_id"] for a in s.in_flight_approvals] == ["r2"]
    s.clear_in_flight_approval("missing")  # no-op when absent
    assert [a["request_id"] for a in s.in_flight_approvals] == ["r2"]


def test_in_flight_approvals_default_empty_on_legacy_state(manager):
    # A state.json persisted before the field existed must load with an empty in-flight list.
    s = _seed(manager, "legacy")
    state = manager._root / s.id / "state.json"
    data = json.loads(state.read_text())
    data.pop("in_flight_approvals", None)
    state.write_text(json.dumps(data))
    manager._sessions.clear()
    loaded = manager.load(s.id)
    assert loaded is not None and loaded.in_flight_approvals == []


def test_prewarmed_survives_persist_load(manager):
    # Regression (BUG F probe-leak): the one-shot `prewarmed` flag — set once the loop has
    # injected the environment pre-probe snapshot as a synthetic message — must be PERSISTED.
    # Were it runtime-only it would reset to False on resume, and a later pre-probe (or a
    # stale-but-set env_snapshot) would re-inject the "[environment pre-probe …]" snapshot
    # mid-transcript, leaking it into the rendered chat + sidebar title.
    s = _seed(manager, "parked after pre-probe")
    s.prewarmed = True
    s.persist()
    manager._sessions.clear()  # force a disk load
    loaded = manager.load(s.id)
    assert loaded is not None and loaded.prewarmed is True


def test_prewarmed_defaults_false_on_legacy_state(manager):
    # A state.json persisted before the field existed must load with prewarmed=False (so the
    # next turn injects the snapshot once, then persists True and never re-injects it).
    s = _seed(manager, "legacy pre-prewarmed")
    state = manager._root / s.id / "state.json"
    data = json.loads(state.read_text())
    data.pop("prewarmed", None)
    state.write_text(json.dumps(data))
    manager._sessions.clear()
    loaded = manager.load(s.id)
    assert loaded is not None and loaded.prewarmed is False


def test_namespace_survives_persist_load(manager):
    s = _seed(manager, "ns roundtrip")
    s.namespace = "llmd-quickstart"
    s.persist()
    manager._sessions.clear()  # force a disk load
    loaded = manager.load(s.id)
    assert loaded is not None and loaded.namespace == "llmd-quickstart"


def test_list_includes_namespace(manager):
    s = _seed(manager, "grouped chat")
    s.namespace = "ns-a"
    s.persist()
    summary = next(x for x in manager.list() if x["id"] == s.id)
    assert summary["namespace"] == "ns-a"


def test_list_namespace_falls_back_to_approved_plan(manager):
    # A session persisted before the namespace field existed: no "namespace" key on disk, but an
    # approved_plan that already chose one. list() must still group it under that namespace.
    s = _seed(manager, "legacy chat")
    state = manager._root / s.id / "state.json"
    data = json.loads(state.read_text())
    data.pop("namespace", None)
    data["approved_plan"] = {"namespace": "from-plan"}
    state.write_text(json.dumps(data))
    summary = next(x for x in manager.list() if x["id"] == s.id)
    assert summary["namespace"] == "from-plan"


def test_list_namespace_none_when_unset(manager):
    # Neither a namespace field nor an approved plan => the UI's "no_namespace" folder (None).
    s = _seed(manager, "unfiled chat")
    state = manager._root / s.id / "state.json"
    data = json.loads(state.read_text())
    data.pop("namespace", None)
    data.pop("approved_plan", None)
    state.write_text(json.dumps(data))
    summary = next(x for x in manager.list() if x["id"] == s.id)
    assert summary["namespace"] is None


# ---- delete_namespace: remove a whole sidebar folder at once -----------------
def _seed_ns(manager: SessionManager, text: str, ns: str | None):
    s = _seed(manager, text)
    s.namespace = ns  # override the conftest "test" default with the folder under test
    s.persist()
    return s


def test_delete_namespace_removes_only_that_folder(manager):
    keep = _seed_ns(manager, "other folder", "ns-b")
    a1 = _seed_ns(manager, "first in a", "ns-a")
    a2 = _seed_ns(manager, "second in a", "ns-a")

    removed = manager.delete_namespace("ns-a")

    assert set(removed) == {a1.id, a2.id}
    assert {x["id"] for x in manager.list()} == {keep.id}


def test_delete_namespace_no_namespace_sentinel(manager):
    # The "no_namespace" folder holds chats with the namespace unset; the sentinel must delete
    # exactly those, leaving namespaced chats alone.
    unfiled = _seed_ns(manager, "unfiled", None)
    filed = _seed_ns(manager, "filed", "ns-a")

    removed = manager.delete_namespace("no_namespace")

    assert removed == [unfiled.id]
    assert {x["id"] for x in manager.list()} == {filed.id}


def test_delete_namespace_unknown_returns_empty(manager):
    _seed_ns(manager, "kept", "ns-a")
    assert manager.delete_namespace("does-not-exist") == []
    assert len(manager.list()) == 1


def test_api_delete_namespace(tmp_path):
    manager = make_manager(tmp_path)
    a1 = _seed_ns(manager, "a one", "ns-a")
    a2 = _seed_ns(manager, "a two", "ns-a")
    keep = _seed_ns(manager, "b one", "ns-b")
    with TestClient(app) as client:
        client.app.state.sessions = manager
        r = client.delete("/api/namespaces/ns-a")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 2 and set(body["deleted"]) == {a1.id, a2.id}
        remaining = {x["id"] for x in client.get("/api/sessions").json()["sessions"]}
        assert remaining == {keep.id}
        # The folder is now empty, so a second delete matches nothing => 404.
        assert client.delete("/api/namespaces/ns-a").status_code == 404
