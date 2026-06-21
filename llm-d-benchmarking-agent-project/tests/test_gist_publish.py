"""The gist-publish engine (:mod:`app.packaging.gist_publish`).

It renders a frozen share snapshot to a self-contained .html and uploads it as a SECRET GitHub gist
so a chat gets a PUBLIC link without exposing the agent. Every test stubs the ``gh`` CLI — no
network, no GitHub account touched — so we exercise the plumbing (URL derivation, the token→gist
mapping, idempotent reuse, the failure reasons) deterministically.
"""
from __future__ import annotations

import subprocess

import pytest

from app.config import get_settings
from app.packaging import gist_publish
from app.storage.share import ShareStore

_UI_DIR = get_settings().ui_dir


def _make_fake_gh(calls):
    """A stand-in for ``gist_publish._gh`` that records argv and fakes gh's outputs."""

    def fake(args):
        calls.append(args)
        if args[:2] == ["gist", "create"]:
            return "https://gist.github.com/octocat/deadbeefcafef00d"
        if args[0] == "api":                       # ["api", "gists/<id>", "--jq", ...]
            gid = args[1].split("/", 1)[1]
            return f"https://gist.githubusercontent.com/octocat/{gid}/raw/abc123/chat.html"
        if args[:2] == ["gist", "delete"]:
            return ""
        raise AssertionError(f"unexpected gh args: {args}")

    return fake


def _seed(workspace) -> str:
    return ShareStore(workspace).create(
        items=[{"role": "user", "text": "hi"}, {"role": "assistant", "text": "hello"}],
        title="demo chat", created_at=1.0, source_session_id="sess",
    )


def test_publish_uploads_a_secret_gist_and_records_the_mapping(tmp_path, monkeypatch):
    token = _seed(tmp_path)
    calls = []
    monkeypatch.setattr(gist_publish, "_gh", _make_fake_gh(calls))

    res = gist_publish.publish(token, workspace=tmp_path, ui_dir=_UI_DIR)

    assert res.reused is False and res.gist_id == "deadbeefcafef00d"
    # Primary link is on the githack render proxy; the fallback is htmlpreview over the raw URL.
    assert res.public_url == "https://gist.githack.com/octocat/deadbeefcafef00d/raw/abc123/chat.html"
    assert res.fallback_url.startswith("https://htmlpreview.github.io/?https://gist.githubusercontent.com/")
    # The gist was created exactly once, with the cross-compatible description.
    creates = [a for a in calls if a[:2] == ["gist", "create"]]
    assert len(creates) == 1 and creates[0][3] == f"llm-d shared chat {token}"
    # The token→gist-id mapping was written where the script also reads/writes it.
    assert gist_publish.mapping_path(tmp_path, token).read_text().strip() == "deadbeefcafef00d"


def test_publish_reuses_an_existing_gist_without_re_uploading(tmp_path, monkeypatch):
    token = _seed(tmp_path)
    mapping = gist_publish.mapping_path(tmp_path, token)
    mapping.parent.mkdir(parents=True, exist_ok=True)
    mapping.write_text("existing1\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(gist_publish, "_gh", _make_fake_gh(calls))

    res = gist_publish.publish(token, workspace=tmp_path, ui_dir=_UI_DIR)

    assert res.reused is True and res.gist_id == "existing1"
    assert "deadbeefcafef00d" not in res.public_url        # the recorded gist, not a new one
    assert not any(a[:2] == ["gist", "create"] for a in calls)   # no second upload


def test_publish_records_the_mapping_even_when_url_derivation_fails(tmp_path, monkeypatch):
    """A `gh api` hiccup AFTER the gist was created must NOT orphan a live gist: the token→gist-id
    mapping has to be recorded the moment the gist exists, so the gist stays revocable (and a retry
    reuses it) instead of leaking an unrevocable, unrecorded gist on every transient failure."""
    token = _seed(tmp_path)

    def flaky_gh(args):
        if args[:2] == ["gist", "create"]:
            return "https://gist.github.com/octocat/deadbeefcafef00d"  # the gist is now LIVE
        if args[0] == "api":                                          # raw-url lookup hiccups
            raise gist_publish.GistPublishError("gh api failed", reason="gh-failed")
        raise AssertionError(f"unexpected gh args: {args}")

    monkeypatch.setattr(gist_publish, "_gh", flaky_gh)

    with pytest.raises(gist_publish.GistPublishError):
        gist_publish.publish(token, workspace=tmp_path, ui_dir=_UI_DIR)

    # The gist is live, so its id MUST be recorded — otherwise neither revoke() nor the script can
    # ever delete it, and a retry would create a second orphaned gist.
    mapping = gist_publish.mapping_path(tmp_path, token)
    assert mapping.exists(), "a created gist was left unrecorded → unrevocable orphan"
    assert mapping.read_text().strip() == "deadbeefcafef00d"


def test_publish_raises_not_shared_for_an_unknown_token(tmp_path):
    with pytest.raises(gist_publish.GistPublishError) as exc:
        gist_publish.publish("a" * 32, workspace=tmp_path, ui_dir=_UI_DIR)
    assert exc.value.reason == "not-shared"


def test_publish_raises_gh_missing_when_the_cli_is_absent(tmp_path, monkeypatch):
    token = _seed(tmp_path)
    monkeypatch.setattr(gist_publish, "have_gh", lambda: False)   # real _gh, but gh "not installed"
    with pytest.raises(gist_publish.GistPublishError) as exc:
        gist_publish.publish(token, workspace=tmp_path, ui_dir=_UI_DIR)
    assert exc.value.reason == "gh-missing"


def test_revoke_deletes_the_gist_and_forgets_the_mapping(tmp_path, monkeypatch):
    token = _seed(tmp_path)
    mapping = gist_publish.mapping_path(tmp_path, token)
    mapping.parent.mkdir(parents=True, exist_ok=True)
    mapping.write_text("g1\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(gist_publish, "_gh", _make_fake_gh(calls))

    assert gist_publish.revoke(token, workspace=tmp_path) == "g1"
    assert ["gist", "delete", "g1"] in calls
    assert not mapping.exists()


def test_revoke_raises_not_published_without_a_mapping(tmp_path):
    with pytest.raises(gist_publish.GistPublishError) as exc:
        gist_publish.revoke("b" * 32, workspace=tmp_path)
    assert exc.value.reason == "not-published"


def test_gh_failure_surfaces_a_gh_failed_reason(monkeypatch):
    """A non-zero ``gh`` exit becomes GistPublishError(reason='gh-failed') with gh's own tail."""
    monkeypatch.setattr(gist_publish.shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(
        gist_publish.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="not logged in\n"),
    )
    with pytest.raises(gist_publish.GistPublishError) as exc:
        gist_publish._gh(["gist", "create"])
    assert exc.value.reason == "gh-failed" and "not logged in" in str(exc.value)


def test_mapping_contract_matches_the_publish_script():
    """The dialog (this engine) and the terminal script must agree on the gist description and the
    token→gist mapping filename, so either can revoke the other's gists."""
    script = (get_settings().ui_dir.parent / "scripts" / "publish_shared_chat.sh").read_text()
    assert gist_publish._GIST_DESC.format(token="X") == "llm-d shared chat X"
    assert "llm-d shared chat" in script                       # same description prefix
    assert gist_publish.mapping_path("/ws", "X").name == "X.gist"
    assert "$TOKEN.gist" in script                             # same mapping filename
