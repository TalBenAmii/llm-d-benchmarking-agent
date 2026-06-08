"""Self-contained, offline HTML export of a shared conversation
(:mod:`app.packaging.shared_chat`).

The export inlines the live SPA (``styles.css`` + ``app.js``) and embeds the frozen snapshot as
``window.__LLMD_SHARED__`` so a shared chat renders read-only from ONE file with no server and no
network — the artifact the "publish a public link without exposing the agent" path puts on a host.

Covers: the XSS-safe JSON embed; the self-contained guarantee (no ``/static/`` refs, no external
fonts, owning session id withheld); the loud failure when the UI shell drifts; and the
``python -m app.packaging.shared_chat`` CLI the publish script shells out to.
"""
from __future__ import annotations

import pytest

import app.config as config_mod
from app.config import get_settings
from app.packaging.shared_chat import _embed_json, main, render_shared_chat
from app.storage.share import ShareStore

_SNAPSHOT = {
    "title": "deploy a tiny model",
    "created_at": 0.5,
    "shared_at": 1.0,
    "items": [
        {"role": "user", "text": "hi & welcome <b>friend</b>"},
        {"role": "assistant", "text": "On it."},
    ],
    "usage": {"total": 42},
    "source_session_id": "SECRET-session-id",   # must NEVER appear in the exported file
}


def test_embed_json_escapes_script_breakout_chars():
    """< > & and the U+2028/U+2029 line separators are escaped so a value can't break out of the
    inline <script> (no </script> injection) and stays valid JS."""
    seps = chr(0x2028) + chr(0x2029)
    out = _embed_json({"x": "</script><b>& done", "y": f"a{seps}b"})
    assert "</script>" not in out and "<" not in out and ">" not in out and "&" not in out
    assert "\\u003c" in out and "\\u003e" in out and "\\u0026" in out
    assert chr(0x2028) not in out and chr(0x2029) not in out
    assert "\\u2028" in out and "\\u2029" in out


def test_render_is_self_contained_and_embeds_the_snapshot():
    html = render_shared_chat(_SNAPSHOT, ui_dir=get_settings().ui_dir)
    # Inlined, not referenced — the file pulls nothing off a server.
    assert "/static/styles.css" not in html
    assert "/static/app.js" not in html
    assert "fonts.googleapis.com" not in html and "fonts.gstatic.com" not in html
    # The SPA + snapshot + the static-boot path are all present.
    assert "<style>" in html and "function renderHistory" in html
    assert "window.__LLMD_SHARED__ = " in html
    assert "bootSharedStatic" in html
    assert '"deploy a tiny model"' in html


def test_render_withholds_the_owning_session_id():
    """A published file is public — it must carry only the transcript, never the session id."""
    html = render_shared_chat(_SNAPSHOT, ui_dir=get_settings().ui_dir)
    assert "SECRET-session-id" not in html
    assert "source_session_id" not in html


def test_render_escapes_a_script_breakout_in_a_message():
    nasty = {**_SNAPSHOT, "title": "x</script><script>alert(1)</script>"}
    html = render_shared_chat(nasty, ui_dir=get_settings().ui_dir)
    assert "</script><script>alert(1)" not in html   # the raw breakout never survives


def _fake_ui(tmp_path, *, css_ref=True, js_ref=True, boot=True):
    ui = tmp_path / "ui"
    ui.mkdir()
    css = '<link rel="stylesheet" href="/static/styles.css" />' if css_ref else "<!-- no css -->"
    js = '<script src="/static/app.js"></script>' if js_ref else "<!-- no js -->"
    (ui / "index.html").write_text(f"<html><head>{css}</head><body>{js}</body></html>", "utf-8")
    (ui / "styles.css").write_text("body{}", "utf-8")
    (ui / "app.js").write_text("var x=1;" + ("window.__LLMD_SHARED__;" if boot else ""), "utf-8")
    return ui


@pytest.mark.parametrize("kw", [{"css_ref": False}, {"js_ref": False}, {"boot": False}])
def test_render_raises_loudly_when_the_ui_shell_drifts(tmp_path, kw):
    """If index.html loses an asset ref (would ship a non-self-contained file) or app.js loses the
    static-boot marker (would ship a blank page), fail loudly rather than emit a broken export."""
    with pytest.raises(RuntimeError):
        render_shared_chat(_SNAPSHOT, ui_dir=_fake_ui(tmp_path, **kw))


# --- the `python -m app.packaging.shared_chat <token>` CLI the publish script uses -------------
def _seed_share(tmp_path, monkeypatch):
    settings = get_settings().model_copy(update={"workspace_dir": tmp_path / "ws"})
    monkeypatch.setattr(config_mod, "get_settings", lambda: settings)
    token = ShareStore(settings.resolved_workspace_dir).create(
        items=_SNAPSHOT["items"], title=_SNAPSHOT["title"],
        created_at=_SNAPSHOT["created_at"], source_session_id=_SNAPSHOT["source_session_id"],
    )
    return token


def test_cli_writes_the_export_for_a_valid_token(tmp_path, monkeypatch):
    token = _seed_share(tmp_path, monkeypatch)
    out = tmp_path / "shared.html"
    assert main([token, "-o", str(out)]) == 0
    body = out.read_text("utf-8")
    assert "window.__LLMD_SHARED__ = " in body
    assert "SECRET-session-id" not in body


def test_cli_errors_for_an_unknown_token(tmp_path, monkeypatch, capsys):
    _seed_share(tmp_path, monkeypatch)              # populate the store with a DIFFERENT token
    assert main(["b" * 32]) == 1
    assert "no shared conversation" in capsys.readouterr().err
