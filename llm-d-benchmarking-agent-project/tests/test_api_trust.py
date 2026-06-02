"""Phase 12 — API trust: optional Bearer auth + token-bucket rate limit + CORS.

Hermetic: FastAPI ``TestClient`` against the REAL ``app.main`` wiring (no live cluster, no
network, no GPU). The rate limiter is exercised with an INJECTABLE FAKE CLOCK — time is
advanced by mutating a list, never by sleeping — so the over-budget (429) path and the refill
are fully deterministic.

Isolation discipline: every test that toggles a feature restores it. Auth/rate-limit settings
are supplied via ``app.dependency_overrides`` / ``app.state`` (auto-scoped to the TestClient),
and the CORS test reloads ``app.main`` under a monkeypatched env then restores the default app
+ the settings cache in a finally — so no toggle leaks into the rest of the suite.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.main as main_mod
from app.config import Settings, get_settings
from app.security.auth import (
    RateLimiter,
    TokenBucket,
    extract_bearer,
    token_matches,
    websocket_authorized,
)

TOKEN = "super-secret-token"


def _auth_settings(**overrides) -> Settings:
    """A copy of the live settings with auth on — keeps resolved repo paths intact so the
    app's startup (which reads the catalog) still works in this worktree."""
    base = get_settings()
    return base.model_copy(update={"auth_enabled": True, "auth_token": TOKEN, **overrides})


# ---------------------------------------------------------------------------
# Pure mechanism: token bucket + bearer helpers (no HTTP).
# ---------------------------------------------------------------------------


def test_token_bucket_is_clock_injected_and_refills_deterministically():
    now = [0.0]
    bucket = TokenBucket(rps=2.0, capacity=3.0, clock=lambda: now[0])

    # Starts full -> a burst of `capacity` is allowed, then the next is denied.
    assert bucket.allow() and bucket.allow() and bucket.allow()
    assert bucket.allow() is False

    # No real time passed -> still empty (proves it's the injected clock, not wall time).
    assert bucket.allow() is False

    # Advance 0.5s at 2 rps -> exactly 1 token back.
    now[0] = 0.5
    assert bucket.allow() is True
    assert bucket.allow() is False

    # A long wait refills but is capped at capacity (never accumulates unbounded credit).
    now[0] = 1000.0
    assert abs(bucket.tokens() - 3.0) < 1e-9


def test_token_bucket_rejects_nonpositive_config():
    with pytest.raises(ValueError):
        TokenBucket(rps=0, capacity=1, clock=lambda: 0.0)
    with pytest.raises(ValueError):
        TokenBucket(rps=1, capacity=0, clock=lambda: 0.0)


def test_bearer_extraction_and_constant_time_match():
    assert extract_bearer("Bearer abc") == "abc"
    assert extract_bearer("bearer abc") == "abc"          # scheme is case-insensitive
    assert extract_bearer("BEARER   spaced  ") == "spaced"
    assert extract_bearer("Basic abc") is None            # wrong scheme
    assert extract_bearer("abc") is None                  # no scheme
    assert extract_bearer("Bearer   ") is None            # empty credential
    assert extract_bearer(None) is None

    assert token_matches("abc", "abc") is True
    assert token_matches("abc", "abd") is False
    assert token_matches(None, "abc") is False            # missing presented
    assert token_matches("abc", "") is False              # empty configured never matches


def test_rate_limiter_disabled_is_noop():
    rl = RateLimiter(enabled=False, rps=1.0, burst=1, clock=lambda: 0.0)
    for _ in range(100):
        rl.check()  # never raises when disabled


# ---------------------------------------------------------------------------
# Auth over the real app (HTTP routes + the /ws endpoint).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not get_settings().bench_repo.is_dir(), reason="repo not present")
def test_http_route_401_without_token_and_200_with_token():
    app = main_mod.app
    enabled = _auth_settings()
    # The override must be a zero-arg callable matching the Depends contract, or FastAPI would
    # treat extra params as request fields (422). Return the precomputed auth-on settings.
    app.dependency_overrides[get_settings] = lambda: enabled
    try:
        with TestClient(app) as client:
            # No credential -> 401 + the Bearer challenge header.
            r = client.get("/api/sessions")
            assert r.status_code == 401
            assert r.headers.get("www-authenticate") == "Bearer"

            # Wrong token -> 401.
            assert client.get(
                "/api/sessions", headers={"Authorization": "Bearer nope"}
            ).status_code == 401

            # Correct token -> 200 (route works as normal).
            r = client.get("/api/sessions", headers={"Authorization": f"Bearer {TOKEN}"})
            assert r.status_code == 200
            assert "sessions" in r.json()
    finally:
        app.dependency_overrides.pop(get_settings, None)


@pytest.mark.skipif(not get_settings().bench_repo.is_dir(), reason="repo not present")
def test_health_and_ready_probes_exempt_from_auth():
    """Liveness/readiness probes stay reachable WITHOUT a token even when auth is on, so a
    Kubernetes kubelet (which can't carry a Bearer token) isn't locked out — while the regular
    API surface is still 401 without one."""
    app = main_mod.app
    enabled = _auth_settings()
    app.dependency_overrides[get_settings] = lambda: enabled
    try:
        with TestClient(app) as client:
            # Probes: no token, never 401 (healthz is up=200; readyz is 200/503 on readiness).
            assert client.get("/healthz").status_code == 200
            assert client.get("/readyz").status_code in (200, 503)
            # A normal API route is still guarded.
            assert client.get("/api/sessions").status_code == 401
    finally:
        app.dependency_overrides.pop(get_settings, None)


@pytest.mark.skipif(not get_settings().bench_repo.is_dir(), reason="repo not present")
def test_ws_rejected_without_token_and_accepted_with_token(monkeypatch):
    app = main_mod.app
    enabled = _auth_settings()
    # The /ws handler calls get_settings() directly; the app-level dep uses the override.
    monkeypatch.setattr(main_mod, "get_settings", lambda: enabled)
    app.dependency_overrides[get_settings] = lambda: enabled
    try:
        with TestClient(app) as client:
            # No token -> handshake accepted then closed with 1008 (TestClient surfaces this
            # as an exception when the first receive finds the socket closed).
            with pytest.raises(Exception):
                with client.websocket_connect("/ws") as ws:
                    ws.receive_json()

            # Token via the ?token= query param -> a normal session opens.
            with client.websocket_connect(f"/ws?token={TOKEN}") as ws:
                assert ws.receive_json()["type"] == "ready"

            # Token via the Authorization header -> also opens.
            with client.websocket_connect(
                "/ws", headers={"Authorization": f"Bearer {TOKEN}"}
            ) as ws:
                assert ws.receive_json()["type"] == "ready"

            # A present-but-wrong token is rejected.
            with pytest.raises(Exception):
                with client.websocket_connect("/ws?token=wrong") as ws:
                    ws.receive_json()
    finally:
        app.dependency_overrides.pop(get_settings, None)


def test_websocket_authorized_unit():
    """The WS authorizer is open when auth is off, and checks header OR query when on."""
    class _FakeWS:
        def __init__(self, headers=None, params=None):
            self.headers = headers or {}
            self.query_params = params or {}

    off = get_settings().model_copy(update={"auth_enabled": False})
    assert websocket_authorized(_FakeWS(), off) is True  # open by default

    on = _auth_settings()
    assert websocket_authorized(_FakeWS(), on) is False
    assert websocket_authorized(_FakeWS(headers={"authorization": f"Bearer {TOKEN}"}), on) is True
    assert websocket_authorized(_FakeWS(params={"token": TOKEN}), on) is True
    assert websocket_authorized(_FakeWS(params={"token": "bad"}), on) is False


# ---------------------------------------------------------------------------
# Rate limit over the real app (HTTP message-intake -> 429), fake clock.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not get_settings().bench_repo.is_dir(), reason="repo not present")
def test_http_429_over_budget_then_refill_with_injected_clock():
    app = main_mod.app
    now = [0.0]
    with TestClient(app) as client:
        # Replace the startup-built limiter with a fake-clock one (same injection pattern the
        # WS tests use for FakeProvider). burst=2 -> two requests, then the third is over-budget.
        app.state.rate_limiter = RateLimiter(
            enabled=True, rps=1.0, burst=2, clock=lambda: now[0]
        )

        assert client.get("/api/sessions").status_code == 200
        assert client.get("/api/sessions").status_code == 200
        # Bucket empty -> 429 (NO sleep — time is frozen at 0.0).
        r = client.get("/api/sessions")
        assert r.status_code == 429
        assert r.headers.get("retry-after") == "1"

        # Advance the injected clock 1s @ 1 rps -> exactly one token returns.
        now[0] = 1.0
        assert client.get("/api/sessions").status_code == 200
        assert client.get("/api/sessions").status_code == 429

        # Liveness/scrape endpoints are deliberately NOT throttled.
        assert client.get("/healthz").status_code == 200
        assert client.get("/metrics").status_code == 200


# ---------------------------------------------------------------------------
# CORS headers present only when origins are configured.
# ---------------------------------------------------------------------------


def test_cors_origins_setting_parses_comma_list():
    s = get_settings().model_copy(
        update={"cors_allow_origins": "https://a.example.com, https://b.example.com ,"}
    )
    assert s.cors_origins_list == ["https://a.example.com", "https://b.example.com"]
    # Default (unset) -> empty list, the signal to NOT install CORS.
    assert get_settings().model_copy(update={"cors_allow_origins": ""}).cors_origins_list == []


def test_cors_headers_present_when_origins_configured():
    """Exercise the REAL ``install_cors`` wiring on a throwaway FastAPI app (no module reload,
    so nothing leaks into the shared app other tests import). When origins are configured the
    CORSMiddleware echoes the allow-origin header on both simple and preflight requests."""
    from fastapi import FastAPI

    throwaway = FastAPI()

    @throwaway.get("/probe")
    async def _probe():  # pragma: no cover - trivial
        return {"ok": True}

    main_mod.install_cors(throwaway, ["https://app.example.com"])
    with TestClient(throwaway) as client:
        # Simple request with an allowed Origin -> the allow-origin header is echoed back.
        r = client.get("/probe", headers={"Origin": "https://app.example.com"})
        assert r.headers.get("access-control-allow-origin") == "https://app.example.com"

        # A CORS preflight is answered with the allow-origin header.
        r = client.options(
            "/probe",
            headers={
                "Origin": "https://app.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert r.headers.get("access-control-allow-origin") == "https://app.example.com"


def test_cors_not_installed_for_empty_origins():
    """Empty origins (the default) -> install_cors is a no-op: no middleware, no CORS header."""
    from fastapi import FastAPI

    throwaway = FastAPI()

    @throwaway.get("/probe")
    async def _probe():  # pragma: no cover - trivial
        return {"ok": True}

    main_mod.install_cors(throwaway, [])
    assert not any("CORS" in m.cls.__name__ for m in throwaway.user_middleware)
    with TestClient(throwaway) as client:
        r = client.get("/probe", headers={"Origin": "https://app.example.com"})
        assert r.headers.get("access-control-allow-origin") is None


# ---------------------------------------------------------------------------
# Defaults: everything OFF -> the API is open exactly as today.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not get_settings().bench_repo.is_dir(), reason="repo not present")
def test_defaults_keep_api_open_and_no_cors_headers():
    s = get_settings()
    assert s.auth_enabled is False
    assert s.rate_limit_enabled is False
    assert s.cors_origins_list == []

    app = main_mod.app
    with TestClient(app) as client:
        # HTTP open with no token.
        assert client.get("/api/sessions").status_code == 200
        assert client.get("/healthz").status_code == 200

        # No CORS headers emitted by default (middleware not installed).
        r = client.get("/healthz", headers={"Origin": "https://app.example.com"})
        assert r.headers.get("access-control-allow-origin") is None

        # WS opens with no token.
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json()["type"] == "ready"
