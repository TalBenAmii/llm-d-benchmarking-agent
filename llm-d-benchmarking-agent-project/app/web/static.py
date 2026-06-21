"""Static-asset serving + CORS wiring — both pure mechanism the route module installs.

``RevalidateStaticFiles`` is a ``StaticFiles`` subclass (no ``app`` dependency); ``app.main``
mounts it. ``install_cors`` wires the CORS middleware onto a passed-in target app (``app.main``
calls it on the shared ``app``, and a test exercises it on a throwaway app — which is exactly
why it takes ``target`` rather than referencing the module-level ``app``).
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles


class RevalidateStaticFiles(StaticFiles):
    """Serve the UI assets with ``Cache-Control: no-cache`` so a browser reload ALWAYS picks up the
    latest ``app.js`` / ``styles.css``.

    The UI is a single-page app: it fetches ``/static/app.js`` once and never re-fetches it on
    in-app navigation (new chat, new run). With the default static headers a browser will happily
    keep serving a cached copy, so a shipped UI change stays invisible until a manual hard-refresh —
    a real, repeated source of "I can't see the new button" confusion. ``no-cache`` does not disable
    caching; it forces the browser to REVALIDATE every load (a cheap conditional request that still
    returns 304 when nothing changed), so the first reload after a deploy gets the new bytes."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        # Only tag real file responses (200/206/304); leave 404s etc. alone.
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


def install_cors(target: FastAPI, origins: list[str]) -> None:
    """Wire CORS (Phase 12) onto ``target`` — but ONLY when ``origins`` is non-empty, so the
    default (empty CORS_ALLOW_ORIGINS) keeps today's behavior: no CORS middleware, no CORS
    headers on responses. Factored out so the wiring can be exercised on a throwaway app in a
    test without reloading this module (a reload would rebind the shared ``app`` and leak).

    SECURITY: never pair the wildcard origin (``"*"``) with ``allow_credentials=True``. Starlette
    refuses to emit a literal ``Access-Control-Allow-Origin: *`` once credentials are allowed and
    instead REFLECTS the request's own ``Origin`` back (with ``Access-Control-Allow-Credentials:
    true``) for ANY origin — so ``CORS_ALLOW_ORIGINS=*`` would silently let every website on the
    internet make authenticated cross-origin reads of the API. When the wildcard is configured we
    therefore drop credentials, yielding a safe ``Access-Control-Allow-Origin: *`` that browsers
    will not pair with credentials. An explicit origin allowlist keeps credentials enabled."""
    if origins:
        wildcard = "*" in origins
        target.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=not wildcard,
            allow_methods=["*"],
            allow_headers=["*"],
        )
