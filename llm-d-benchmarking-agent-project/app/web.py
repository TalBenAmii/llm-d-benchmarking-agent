"""Pure, decorator-free HTTP helpers extracted from ``app.main``.

This module holds the *mechanism* the FastAPI route handlers in ``app.main`` call but that
themselves do NOT register on the ``app`` object and do NOT need it: path-traversal hardening
for artifact/bundle serving, the public-share snapshot redaction, the no-cache static-files
subclass + CORS wiring, and the validation-error formatter.

Keeping these out of ``app.main`` shrinks the route module to its decorated handlers + the
``app`` wiring + the ``/ws`` loop, while the routes stay thin callers. Each helper is pure
(no module-level ``app``/``app.state``, no decorators) and takes whatever it needs as an
argument — notably any ``get_settings()`` call stays in the route handler (so a test that
monkeypatches ``app.main.get_settings`` still steers which workspace is resolved, and *when*).

Scoped tests for the trust boundaries here (CORS + path-traversal + share-redaction) live in
``tests/test_api_trust.py`` (the helpers are also exercised through the HTTP/WS route tests in
``tests/test_api*.py``).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.agent.ws_schemas import ValidationError
from app.config import Settings
from app.llm.provider import AGENT_SDK_PROVIDERS, OPENAI_PROVIDERS
from app.storage.provenance import BundleStore

# ── inbound-frame validation-error formatting (WS protocol ``error`` event) ─────────────────


def first_validation_message(exc: ValidationError) -> str:
    """A short, human-readable reason from a Pydantic validation error for the protocol
    `error` event — the field path + message of the first error, without leaking internals."""
    errs = exc.errors()
    if not errs:
        return "invalid frame"
    e = errs[0]
    loc = ".".join(str(p) for p in e.get("loc", ())) or "frame"
    return f"{loc}: {e.get('msg', 'invalid')}"


# ── response-shaping for the HTTP routes ────────────────────────────────────────────────────


def provider_view(settings: Settings, provider_error: str | None) -> dict[str, Any]:
    """The active LLM provider + model as the header badge shows them (GET /api/provider).

    Shares ``get_provider``'s alias constants (config dispatch, not judgment) but stays
    settings-based so it still answers when the provider FAILED to build — exactly the state
    the badge must surface (``configured: False`` → "LLM not configured"). An unknown provider
    name (which makes ``get_provider`` raise) gets ``model: None`` rather than a model it never
    resolved to. Deliberately minimal: never a key, account identity, or the error text (which
    can name env vars)."""
    provider = (settings.llm_provider or "anthropic").lower()
    if provider in AGENT_SDK_PROVIDERS:
        model = settings.agent_sdk_model
    elif provider in OPENAI_PROVIDERS:
        model = settings.openai_model
    elif provider == "anthropic":
        model = settings.anthropic_model
    else:
        model = None
    return {"provider": provider, "model": model, "configured": provider_error is None}


def history_record_view(rec) -> dict[str, Any]:
    """The results-browser summary view of one stored history record (no heavy body)."""
    return {
        "id": rec.id, "stored_at": rec.stored_at, "label": rec.label, "tags": rec.tags,
        "model": rec.model, "run_uid": rec.run_uid, "spec": rec.spec,
        "harness": rec.harness, "workload": rec.workload, "namespace": rec.namespace,
        # Reproducibility: when this record has a provenance bundle, surface its id (+ the
        # owning session id) so the sidebar can offer Reproduce / Export report-card.
        "bundle_id": getattr(rec, "bundle_id", None),
        "session_id": getattr(rec, "session_id", None),
    }


# ── public-share snapshot redaction ─────────────────────────────────────────────────────────

# Fields on a card tool's ``result`` that carry server-internal absolute filesystem paths — the
# located report's path (``<sessions_root>/<session_id>/.../benchmark_report*.json``), and the
# directories a not-found probe searched. They drive nothing in the read-only viewer (the client
# renders ``summary``/``charts`` only; charts are already session-relative), yet a public share is
# UNAUTHENTICATED, so shipping them would disclose the host path layout AND the owning session id —
# the very id the snapshot deliberately withholds (see read_share / shared_chat._PUBLIC_FIELDS).
_SHARE_REDACT_RESULT_KEYS = ("report_path", "searched")


def _scrub_share_path(s: Any, ws: str, sid: str, home: str) -> Any:
    """Mask the workspace root, the owning session id, and the home dir in ONE string (a
    non-string passes through). The command name/flags around them stay intact so the shared
    command is still readable — only the leaking absolute path + id become placeholders. Order
    matters: the workspace prefix (which itself sits under ``home``) is masked before ``home``."""
    if not isinstance(s, str):
        return s
    s = s.replace(ws, "<workspace>").replace(sid, "<session>")
    return s.replace(home, "~") if home else s


def _scrub_share_command(v: Any, ws: str, sid: str, home: str) -> Any:
    """Scrub a command/argv field that is either a shell-string command or an argv list."""
    if isinstance(v, list):
        return [_scrub_share_path(x, ws, sid, home) for x in v]
    return _scrub_share_path(v, ws, sid, home)


def redact_share_items(
    items: list[dict[str, Any]], *, workspace_root: Path, session_id: str,
) -> list[dict[str, Any]]:
    """Strip server-internal absolute paths + the owning session id from a PUBLIC share snapshot.

    The transcript replayed to the owner on resume legitimately carries the located report's
    absolute path and the command trail's ``--workspace <sessions_root>/<session_id>/…`` paths;
    a public, UNAUTHENTICATED share must not (they disclose the host path layout, OS username, and
    the very session id the snapshot withholds). Returns NEW item dicts (the live session is never
    mutated): the path-bearing keys are removed from any ``tool_result`` result, and the workspace
    root / session id / home dir are masked to placeholders across every command-trail field
    (``tool_call`` input command, ``command`` text+argv, ``approval_decision`` payload command+argv),
    leaving every render-relevant field (summary, charts, metrics) and the command names intact."""
    ws, sid, home = str(workspace_root), session_id, str(Path.home())
    out: list[dict[str, Any]] = []
    for it in items:
        role = it.get("role")
        if role == "tool_result":
            result = it.get("result")
            if isinstance(result, dict) and any(k in result for k in _SHARE_REDACT_RESULT_KEYS):
                out.append({**it, "result": {k: v for k, v in result.items()
                                             if k not in _SHARE_REDACT_RESULT_KEYS}})
            else:
                out.append(it)
        elif role == "tool_call" and isinstance(it.get("input"), dict) and "command" in it["input"]:
            command = _scrub_share_command(it["input"]["command"], ws, sid, home)
            out.append({**it, "input": {**it["input"], "command": command}})
        elif role == "command":
            out.append({**it, "text": _scrub_share_path(it.get("text"), ws, sid, home),
                        "argv": _scrub_share_command(it.get("argv"), ws, sid, home)})
        elif role == "approval_decision" and isinstance(it.get("payload"), dict):
            payload = {**it["payload"]}
            for k in ("command", "argv"):
                if k in payload:
                    payload[k] = _scrub_share_command(payload[k], ws, sid, home)
            out.append({**it, "payload": payload})
        else:
            out.append(it)
    return out


# ── static-asset serving + CORS wiring ──────────────────────────────────────────────────────


class RevalidateStaticFiles(StaticFiles):
    """Serve the UI assets with ``Cache-Control: no-cache`` so a browser reload ALWAYS picks up the
    latest ``app.js`` / ``styles.css``.

    The UI is a single-page app: it fetches ``/static/app.js`` once and never re-fetches it on
    in-app navigation (new chat, new run). With the default static headers a browser will happily
    keep serving a cached copy, so a shipped UI change stays invisible until a manual hard-refresh —
    a real, repeated source of "I can't see the new button" confusion. ``no-cache`` does not disable
    caching; it forces the browser to REVALIDATE every load (a cheap conditional request that still
    returns 304 when nothing changed), so the first reload after a deploy gets the new bytes.

    ⚠️ Dev gotcha: because the SPA fetches ``/static/app.js`` once, an already-open tab still needs
    ONE manual hard-reload (Ctrl+Shift+R) to see a UI change, and changing this ``Cache-Control``
    header itself requires a SERVER RESTART to take effect."""

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


# ── path-traversal hardening for artifact + provenance-bundle serving ───────────────────────

# Per-run chart images (e.g. the latency/throughput PNGs inference-perf renders into a
# session's analysis/ dir) live under the gitignored workspace, which the /static mount does
# NOT serve. The artifact route exposes them so the UI can show a run's charts inline next to
# its summary. Hardened: image suffixes only, and the resolved path must stay INSIDE the named
# session dir (defeats ../ traversal). The chart paths come from locate_and_parse_report's
# `charts` field.
_ARTIFACT_SUFFIXES = frozenset({".png", ".svg", ".jpg", ".jpeg", ".webp"})
_ARTIFACT_MEDIA = {
    ".png": "image/png", ".svg": "image/svg+xml", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".webp": "image/webp",
}


def resolve_artifact(sessions_root: Path, sid: str, path: str) -> tuple[Path, str]:
    """Resolve one image artifact under ``<sessions_root>/<sid>/`` (read-only, image-only).

    Returns ``(candidate, media_type)`` for a valid request; raises ``HTTPException(404,
    "artifact not found")`` for anything else. ``sessions_root`` must already be ``.resolve()``-d
    by the caller. Rejects ../ traversal in either ``sid`` or ``path``, non-image suffixes,
    non-files, and degrades over-long components / embedded NUL bytes to a clean 404 (never 500)."""
    try:
        base = (sessions_root / sid).resolve()
        candidate = (base / path).resolve()
        # `base` must be a real session dir directly under sessions_root, and `candidate` must not
        # escape it — together these reject ../ traversal in either `sid` or `path`.
        if base.parent != sessions_root or not base.is_dir() or not candidate.is_relative_to(base):
            raise HTTPException(status_code=404, detail="artifact not found")
        suffix = candidate.suffix.lower()
        if suffix not in _ARTIFACT_SUFFIXES or not candidate.is_file():
            raise HTTPException(status_code=404, detail="artifact not found")
    except (OSError, ValueError):
        # An over-long `sid`/`path` component (ENAMETOOLONG → OSError) or an embedded NUL byte
        # (`%00` → ValueError "embedded null byte") during resolution must read as a clean 404 —
        # never a 500. (HTTPException is neither, so the explicit 404s above propagate untouched.)
        raise HTTPException(status_code=404, detail="artifact not found") from None
    return candidate, _ARTIFACT_MEDIA[suffix]


def resolve_bundle(sessions_root: Path, sid: str, bundle_id: str) -> dict[str, Any]:
    """Locate one provenance bundle JSON under a session's ``bundles/`` dir, reusing the SAME
    path-traversal hardening as ``resolve_artifact`` (``base.parent == sessions_root``,
    ``is_relative_to``) PLUS the BundleStore's own ``_safe_id`` guard on the bundle id. A 404 for
    a bad ``sid`` / ``bundle_id`` / missing bundle (never an info leak). ``sessions_root`` must
    already be ``.resolve()``-d by the caller."""
    try:
        base = (sessions_root / sid).resolve()
        if base.parent != sessions_root or not base.is_dir():
            raise HTTPException(status_code=404, detail="bundle not found")
    except (OSError, ValueError):
        # Over-long `sid` (ENAMETOOLONG → OSError) or an embedded NUL byte (`%00` → ValueError) →
        # clean 404, never a 500.
        raise HTTPException(status_code=404, detail="bundle not found") from None
    bundle = BundleStore(base).read(bundle_id)  # _safe_id inside rejects ../ and a/b ids
    if bundle is None:
        raise HTTPException(status_code=404, detail="bundle not found")
    # Defense in depth: the resolved file must stay inside the session's bundles dir.
    candidate = (base / "bundles" / f"{bundle_id}.json").resolve()
    if not candidate.is_relative_to(base):
        raise HTTPException(status_code=404, detail="bundle not found")
    return bundle
