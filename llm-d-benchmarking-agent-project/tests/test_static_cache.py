"""The UI static assets must be served with revalidation headers.

The chat UI is a single-page app that fetches ``/static/app.js`` once and never re-fetches it on
in-app navigation. Without ``Cache-Control: no-cache`` a browser keeps serving the cached JS, so a
shipped UI change (e.g. the metrics-server pre-flight hint) stays invisible until a manual
hard-refresh. These hermetic TestClient checks lock the revalidation header in place so future UI
changes are picked up on a normal reload.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import main


def test_static_assets_send_no_cache_revalidation():
    with TestClient(main.app) as client:
        for asset in ("app.js", "styles.css", "index.html"):
            r = client.get(f"/static/{asset}")
            assert r.status_code == 200, f"/static/{asset} not served"
            cc = r.headers.get("cache-control", "")
            assert "no-cache" in cc, f"/static/{asset} missing no-cache (got {cc!r})"


def test_served_app_js_carries_the_metrics_server_hint():
    # Guards that the asset on the wire is current. The mid-run install BUTTON was retired (it
    # collided with the in-flight-turn guard); the agent now offers the install before the run, so
    # the unavailable panel shows a passive hint instead.
    with TestClient(main.app) as client:
        body = client.get("/static/app.js").text
    assert "resource-fix-btn" not in body
    assert "offers to install it" in body
