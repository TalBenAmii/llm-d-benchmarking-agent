"""``app.web`` — CORS wiring (``install_cors``) + the ``/api`` surface being open by default.

Hermetic: FastAPI ``TestClient`` against the REAL ``app.main`` wiring (no live cluster, no
network, no GPU) for the whole-app checks, and a throwaway FastAPI app for the ``install_cors``
unit checks (so nothing leaks into the shared app other tests import).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.main as main_mod
from app.config import Settings, get_settings

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


def test_cors_wildcard_origin_never_reflects_arbitrary_origin_with_credentials():
    """SECURITY regression: ``CORS_ALLOW_ORIGINS=*`` must NOT turn into "reflect any origin WITH
    credentials". Starlette, given ``allow_origins=["*"]`` + ``allow_credentials=True``, refuses
    the literal ``*`` and instead echoes the request's own Origin back together with
    ``Access-Control-Allow-Credentials: true`` — letting every website on the internet make
    authenticated cross-origin reads of the API. ``install_cors`` must drop credentials for the
    wildcard so the response is a safe literal ``*`` that browsers won't pair with credentials.

    BEFORE the fix: ACAO == "https://evil.example.com" and ACAC == "true" (the vuln).
    AFTER the fix:  ACAO == "*" and no ACAC (safe; credentials disabled for the wildcard)."""
    from fastapi import FastAPI

    throwaway = FastAPI()

    @throwaway.get("/probe")
    async def _probe():  # pragma: no cover - trivial
        return {"ok": True}

    # This is exactly what Settings(cors_allow_origins="*").cors_origins_list produces.
    assert Settings(cors_allow_origins="*").cors_origins_list == ["*"]

    main_mod.install_cors(throwaway, ["*"])
    with TestClient(throwaway) as client:
        evil = "https://evil.example.com"  # an origin the operator never intended to allow
        r = client.get("/probe", headers={"Origin": evil})
        # The attacker's origin must NOT be reflected back...
        assert r.headers.get("access-control-allow-origin") != evil
        # ...the wildcard may be returned, but ONLY without credentials.
        if r.headers.get("access-control-allow-credentials") == "true":
            assert r.headers.get("access-control-allow-origin") not in (evil, "*")


def test_cors_explicit_origin_list_keeps_credentials():
    """A non-wildcard explicit allowlist still gets credentialed CORS (the intended use): the
    configured origin is reflected WITH ``Access-Control-Allow-Credentials: true``, while an
    unlisted origin is not reflected at all."""
    from fastapi import FastAPI

    throwaway = FastAPI()

    @throwaway.get("/probe")
    async def _probe():  # pragma: no cover - trivial
        return {"ok": True}

    main_mod.install_cors(throwaway, ["https://app.example.com"])
    with TestClient(throwaway) as client:
        allowed = client.get("/probe", headers={"Origin": "https://app.example.com"})
        assert allowed.headers.get("access-control-allow-origin") == "https://app.example.com"
        assert allowed.headers.get("access-control-allow-credentials") == "true"

        denied = client.get("/probe", headers={"Origin": "https://evil.example.com"})
        assert denied.headers.get("access-control-allow-origin") is None


# ---------------------------------------------------------------------------
# Defaults: the API is open (no auth, no rate-limit) and CORS is off unless configured.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not get_settings().bench_repo.is_dir(), reason="repo not present")
def test_defaults_keep_api_open_and_no_cors_headers():
    s = get_settings()
    assert s.cors_origins_list == []

    app = main_mod.app
    with TestClient(app) as client:
        # HTTP open — this is a single-user in-cluster service, no Bearer auth.
        assert client.get("/api/sessions").status_code == 200
        assert client.get("/healthz").status_code == 200

        # No CORS headers emitted by default (middleware not installed).
        r = client.get("/healthz", headers={"Origin": "https://app.example.com"})
        assert r.headers.get("access-control-allow-origin") is None

        # WS opens with no token.
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json()["type"] == "ready"
