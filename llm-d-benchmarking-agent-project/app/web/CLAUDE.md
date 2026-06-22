# app/web/ — pure, decorator-free HTTP/SSE helpers

Helpers extracted OUT of `app.main` so the route module stays thin (decorated handlers + `app` wiring + the
`/ws` loop). **Every helper is pure** — no module-level `app`/`app.state`, no decorators, no
`get_settings()`; it takes whatever it needs (e.g. an already-resolved `sessions_root`) as an argument, so
route-level monkeypatching of `app.main.get_settings` in tests still steers behavior. `app.main` is the sole
caller/wirer; nothing here registers on the FastAPI `app`.

## Security-load-bearing (preserve exact behavior)
- **`paths.py` — 404-not-500 path-traversal hardening** for the read-only artifact + provenance-bundle
  routes: rejects `../` (`base.parent == sessions_root` + `is_relative_to`), serves image-suffix-only
  artifacts, and degrades over-long components (ENAMETOOLONG) and embedded NUL bytes to a clean 404 — never a
  500. `sessions_root` MUST already be `.resolve()`-d by the caller.
- **`static.py::install_cors` — wildcard-credentials guard:** when origin `"*"` is configured it DROPS
  `allow_credentials` (else Starlette reflects any Origin back with credentials → authenticated cross-origin
  reads). Wires nothing when `origins` is empty. `RevalidateStaticFiles` forces `no-cache, must-revalidate`
  so UI reloads pick up new `app.js`/`styles.css`. ⚠️ Dev gotcha: the chat UI is a single-page app that fetches
  `/static/app.js` once — an already-open tab still needs ONE manual hard-reload (Ctrl+Shift+R) to see a UI change,
  and changing this header itself requires a SERVER RESTART to take effect.
- **`share.py::redact_share_items` — the public-share redaction boundary:** strips server-internal keys
  (`report_path`, `searched`) from `tool_result` rows before a snapshot is frozen (a public share is
  UNAUTHENTICATED). Returns NEW dicts (never mutates the live session).

## Key files
- `paths.py` — `resolve_artifact`, `resolve_bundle`. · `static.py` — `RevalidateStaticFiles`, `install_cors`.
- `share.py` — `redact_share_items`. · `errors.py` — `first_validation_message`. · `views.py` — `history_record_view`.

## Scoped tests
```bash
pytest tests/test_api_trust.py     # CORS + path-traversal + share-redaction trust boundaries
```
(the helpers are also exercised through the HTTP/WS route tests in `tests/test_api*.py`.)
