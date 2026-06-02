"""API trust mechanism (Phase 12): optional Bearer-token auth + an in-memory token-bucket
rate limiter. Pure mechanism, stdlib only — NO new runtime dependency.

Both features default OFF (see ``Settings.auth_enabled`` / ``Settings.rate_limit_enabled``),
so the local, single-user flow is byte-for-byte unchanged. When an operator exposes the API
beyond localhost they enable them via env; the *judgment* about when/how lives in
``knowledge/api_trust.md`` (thin code, thick agent).

Design notes:
- Auth comparison is constant-time (``secrets.compare_digest``) so a wrong token leaks no
  timing signal about how many leading bytes matched.
- The rate limiter takes an INJECTABLE monotonic clock (a ``Callable[[], float]``) so tests
  advance time deterministically with NO real sleeps. Default clock is ``time.monotonic``.
- The token-bucket math is deliberately tiny: refill = elapsed * rps, capped at burst; a
  request costs one token; an empty bucket is rejected (429).
"""
from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable

from fastapi import Depends, HTTPException, Request, WebSocket, status
from starlette.requests import HTTPConnection

from app.config import Settings, get_settings

# ---------------------------------------------------------------------------
# Bearer-token extraction + constant-time check (mechanism, no policy).
# ---------------------------------------------------------------------------

_BEARER_PREFIX = "bearer "  # case-insensitive scheme per RFC 6750


def extract_bearer(authorization: str | None) -> str | None:
    """Pull the raw token out of an ``Authorization: Bearer <token>`` header value.

    Returns ``None`` when the header is absent or not a Bearer credential. Does NOT validate
    the token — that's :func:`token_matches` (kept separate so the check is constant-time)."""
    if not authorization:
        return None
    if authorization[: len(_BEARER_PREFIX)].lower() != _BEARER_PREFIX:
        return None
    token = authorization[len(_BEARER_PREFIX):].strip()
    return token or None


def token_matches(presented: str | None, expected: str) -> bool:
    """Constant-time comparison of a presented token against the configured one.

    ``secrets.compare_digest`` runs in time independent of the contents (for equal-length
    inputs), so a partially-correct guess gains no timing advantage. A missing presented
    token, or an empty configured token, never matches."""
    if not presented or not expected:
        return False
    return secrets.compare_digest(presented, expected)


def _unauthorized() -> HTTPException:
    # 401 + the WWW-Authenticate challenge clients expect for Bearer auth.
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="missing or invalid bearer token",
        headers={"WWW-Authenticate": "Bearer"},
    )


def check_http_auth(conn: HTTPConnection, settings: Settings = Depends(get_settings)) -> None:
    """App-level FastAPI dependency guarding HTTP routes.

    Typed as ``HTTPConnection`` (the common base of ``Request`` and ``WebSocket``) so it can
    be registered app-wide without breaking the WebSocket route — Starlette applies app-level
    dependencies to WS routes too, and a ``Request``-typed param would fail to resolve there.
    WebSocket connections are intentionally skipped here: the ``/ws`` handler does its own
    auth and closes with a proper 1008 (a 401 HTTPException can't be returned over a socket).

    When ``auth_enabled`` is False (default) this is a no-op and every route stays open exactly
    as today. When enabled, an HTTP request needs a valid Bearer token or it raises 401."""
    if not settings.auth_enabled:
        return
    if conn.scope.get("type") != "http":  # WebSocket handshake -> guarded in the /ws handler
        return
    presented = extract_bearer(conn.headers.get("authorization"))
    if not token_matches(presented, settings.auth_token):
        raise _unauthorized()


def websocket_authorized(websocket: WebSocket, settings: Settings) -> bool:
    """Authorize a WebSocket handshake.

    WS clients can't always set arbitrary headers, so we accept the token from EITHER the
    ``Authorization: Bearer`` header OR a ``?token=`` query param. Returns True when auth is
    disabled (open) or the token is valid; False otherwise (caller closes with 1008).

    Open by default — when ``auth_enabled`` is False this always returns True."""
    if not settings.auth_enabled:
        return True
    presented = extract_bearer(websocket.headers.get("authorization"))
    if presented is None:
        presented = websocket.query_params.get("token") or None
    return token_matches(presented, settings.auth_token)


# ---------------------------------------------------------------------------
# Token-bucket rate limiter (clock-injected so tests are deterministic).
# ---------------------------------------------------------------------------


class TokenBucket:
    """A single token bucket: ``capacity`` tokens, refilling at ``rps`` tokens/second, never
    exceeding ``capacity``. ``allow()`` spends one token and returns whether it was available.

    The clock is injected (``Callable[[], float]`` returning a MONOTONIC float) so tests can
    advance time without sleeping; production passes ``time.monotonic``. Thread-safe via a
    lock — a sync FastAPI dependency may be called from the threadpool."""

    def __init__(self, *, rps: float, capacity: float, clock: Callable[[], float]) -> None:
        if rps <= 0:
            raise ValueError("rps must be > 0")
        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        self._rps = float(rps)
        self._capacity = float(capacity)
        self._clock = clock
        self._tokens = float(capacity)  # start full (allow an initial burst)
        self._last = clock()
        self._lock = threading.Lock()

    def _refill_locked(self) -> None:
        now = self._clock()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rps)
            self._last = now

    def allow(self, cost: float = 1.0) -> bool:
        """Try to spend ``cost`` tokens. Refills based on elapsed time first, then either
        deducts and returns True, or leaves the bucket untouched and returns False."""
        with self._lock:
            self._refill_locked()
            if self._tokens >= cost:
                self._tokens -= cost
                return True
            return False

    def tokens(self) -> float:
        """Current token count (after a refill). For tests/observability only."""
        with self._lock:
            self._refill_locked()
            return self._tokens


class RateLimiter:
    """A process-wide rate limiter over a single shared :class:`TokenBucket`.

    The HTTP message-intake surface shares one bucket (the limiter protects the *server's*
    work budget, not per-client fairness — that would need a distributed store and is out of
    scope). Disabled by default: when off, :meth:`check` is a no-op and nothing is limited."""

    def __init__(
        self,
        *,
        enabled: bool,
        rps: float,
        burst: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.enabled = enabled
        self._clock = clock
        self._bucket = (
            TokenBucket(rps=rps, capacity=burst, clock=clock) if enabled else None
        )

    @classmethod
    def from_settings(
        cls, settings: Settings, *, clock: Callable[[], float] = time.monotonic
    ) -> RateLimiter:
        return cls(
            enabled=settings.rate_limit_enabled,
            rps=settings.rate_limit_rps,
            burst=settings.rate_limit_burst,
            clock=clock,
        )

    def check(self) -> None:
        """Spend one token or raise 429. No-op when disabled (the default)."""
        if not self.enabled or self._bucket is None:
            return
        if not self._bucket.allow():
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="rate limit exceeded — slow down",
                headers={"Retry-After": "1"},
            )


def rate_limit(request: Request) -> None:
    """FastAPI dependency that applies the app-wide rate limiter (built once at startup and
    stashed on ``app.state.rate_limiter``). Off by default -> no-op."""
    limiter: RateLimiter | None = getattr(request.app.state, "rate_limiter", None)
    if limiter is not None:
        limiter.check()
