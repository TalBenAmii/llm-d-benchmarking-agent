# API trust: auth + rate-limit + CORS (operator/agent reference)

The FastAPI surface ships **safe to expose** but **frictionless for local use**: three
independent controls — Bearer-token **auth**, an in-memory **rate limiter**, and **CORS** —
that all **default OFF/open**, so the local single-user flow (and the test suite) is unchanged.
This file is the *judgment* (when/how to turn them on); the *mechanism* lives in
`app/security/auth.py` and is wired in `app/main.py`. No new runtime dependency (stdlib +
FastAPI/Starlette only).

## When to enable each

| Exposure | Enable |
|---|---|
| Local dev on `127.0.0.1` (the default) | nothing — leave all three off |
| Reachable on a LAN / behind a shared ingress | `AUTH_ENABLED` (always), `RATE_LIMIT_ENABLED` |
| A browser app on a *different* origin calls the API | add `CORS_ALLOW_ORIGINS` |
| Internet-exposed | all three, plus TLS/ingress in front (out of scope here) |

Rule of thumb: **the moment the API binds to anything other than localhost, turn on auth.**

## Bearer auth (`AUTH_ENABLED`, `AUTH_TOKEN`)
- When enabled, **every HTTP route and the `/ws` endpoint** require
  `Authorization: Bearer <AUTH_TOKEN>`. The `/ws` handshake may instead pass the token as a
  `?token=...` query param (browsers can't set WS headers). Missing/bad token -> **401** on
  HTTP, **WS close 1008** on the socket.
- The token is compared with `secrets.compare_digest` (**constant-time**), so a wrong guess
  leaks no timing signal.
- `AUTH_TOKEN` is a **backend secret** — treat it like the LLM keys. It never reaches the
  browser and is never logged. Generate a long random value (e.g. `openssl rand -hex 32`).
- Enabling auth with an **empty token is a misconfiguration**: the server refuses to start
  (fail loud) rather than silently 401-ing every request.
- The liveness/readiness probes (`/healthz`, `/readyz`) stay **unauthenticated even when auth is
  on** — a K8s kubelet can't carry a Bearer token, and they expose only up/ready facts (no session
  data, no secrets). `/metrics` is **not** exempted: with auth on, your Prometheus scrape must send
  the token (or scrape at the ingress) — do **not** weaken the app to special-case it.

## Rate limit (`RATE_LIMIT_ENABLED`, `RATE_LIMIT_RPS`, `RATE_LIMIT_BURST`)
- A **token bucket** on the `/api/*` message-intake surface: `RPS` is the steady refill rate
  (tokens/second), `BURST` is the bucket capacity (max instantaneous). An empty bucket -> **429**
  (with `Retry-After: 1`).
- It's **per-process**, not distributed — it protects *this server's* work budget, not per-client
  fairness. Behind multiple replicas, set the limit per replica accordingly (or rate-limit at the
  ingress for a global cap).
- `/healthz` and `/metrics` are **never** throttled (a probe/scrape storm must not flap them).
- The bucket uses an **injected monotonic clock**, which is why the tests are deterministic with
  no sleeps — not an operator knob, just why the math is testable.

## CORS (`CORS_ALLOW_ORIGINS`)
- Comma-separated allowed origins (e.g. `https://app.example.com,https://staging.example.com`).
  **Empty (default) installs no CORS middleware at all** — responses carry no CORS headers, which
  is today's behavior (same-origin only).
- Only set this if a browser on a *different* origin must call the API. List **exact origins** —
  never `*` with credentials. This is a browser-side control; it is **not** a substitute for auth.

## What stays true regardless
- All three off by default => existing flows and the whole test suite are unchanged.
- These are **pure mechanism**: there is no per-request decision logic in Python beyond the
  token-bucket math and a constant-time compare. The judgment about *whether* to expose the API
  and *how strict* to be lives here, in this doc.
