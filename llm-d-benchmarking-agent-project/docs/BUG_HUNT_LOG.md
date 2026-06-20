# Bug-Hunt Log

Running log of bugs found by "playing with the app like a regular user" (driving the
HTTP/WS API + reading the client and backend). Each entry: symptom → trigger → root cause →
fix → status.

Started 2026-06-20.

---

## BUG-001 — Dead duplicate `fmtNum` in `ui/app.js` (latent)
- **Status:** FIXED
- **Severity:** low (latent maintainability trap; no current user-visible effect)
- **Where:** `ui/app.js` defined `function fmtNum` twice — line ~1291 and line ~2243.
- **Trigger / root cause:** `app.js` is loaded as a classic script (`<script src>` in
  `index.html`, not `type="module"`). Two top-level `function fmtNum` declarations are legal
  there, but the **second declaration hoists over the first**, so the line-1291 version was
  dead code — *every* call site (incl. the ones textually above 2243: `metaRow`, the goodput
  gauge, the Pareto scatter point titles) silently resolved to the 2243 implementation, which
  formats differently (thousands separators, exponential for tiny values, `isFinite` guard).
  Anyone editing the 1291 version to change formatting would see no effect — a real trap.
- **Fix:** removed the dead line-1291 `fmtNum`; kept the more complete 2243 version (it already
  governed every call site, and additionally guards `NaN`/`Infinity`). No runtime behavior
  change — pure dead-code removal that eliminates the shadowing trap.
- **Verified non-bugs checked nearby (defensive code is correct):** `sparkline` (caller guards
  `points.length < 2`), `resSpark` (`vals.length < 2` guard), `scatterPlot.span` (`lo === hi`
  guard) — all correctly avoid the single-point / flat-axis divide-by-zero.

## BUG-002 — Resilience card shows `undefined/N classified correctly`
- **Status:** FIXED
- **Severity:** low (cosmetic garble in the chaos/resilience verdict line)
- **Where:** `ui/app.js`, resilience card builder (`vc = card.verdict_counts`).
- **Trigger / root cause:** both numerator lines were guarded only on
  `vc.faults_injected != null`, then interpolated `vc.classified_correctly` /
  `vc.recovered_as_designed`. A resilience drill whose `verdict_counts` carries
  `faults_injected` but omits one numerator rendered `undefined/3 classified correctly`.
- **Fix:** guard each line on BOTH its own numerator and `faults_injected`.

## BUG-003 — `ShareStore.create` writes the snapshot non-atomically
- **Status:** FIXED
- **Severity:** low (rare corruption / torn read under concurrency or a crash mid-write)
- **Where:** `app/storage/share.py` `create()` — `(self._root / f"{token}.json").write_text(...)`.
- **Root cause:** the lone store writing directly to the target path. All three sibling stores
  (`history.py`, `provenance.py` BundleStore, `autotune.py`) use the temp-then-`replace()` atomic
  pattern, and this store's own docstring promises a "durable write." A public viewer reading
  `/share/<token>` mid-write could see a half-written file; a crash mid-write leaves a corrupt snapshot.
- **Fix:** write to `<token>.json.tmp` then `tmp.replace(path)` — atomic, matching the siblings.

## BUG-004 — `/ws?after_seq=²` crashes the WebSocket handshake (500)
- **Status:** FIXED
- **Severity:** medium (unhandled exception tears down the connection; adversarial/edge input)
- **Where:** `app/main.py` WS handler — `after_seq = int(after_raw) if (after_raw and after_raw.isdigit()) else None`.
- **Root cause:** `str.isdigit()` is broader than `int()` accepts — it's `True` for unicode digits
  like the superscript `²`, but `int("²")` raises `ValueError`. The parse runs *before* the handler's
  `try` block, so `GET /ws?after_seq=²` raises straight out of the (already-accepted) handshake.
- **Fix:** require `after_raw.isascii() and after_raw.isdigit()` before `int()`; any other value
  falls back to the full-history path as intended.

## BUG-005 — `revoke_share` touches the filesystem + `gh` with an unvalidated token
- **Status:** FIXED
- **Severity:** low (defense-in-depth; route regex already blocks `/`, so traversal was bounded)
- **Where:** `app/main.py` `revoke_share` — `gist_publish.mapping_path(workspace, token).exists()`
  and `gist_publish.revoke(token, ...)` run BEFORE `_share_store().delete(token)` validates the token.
- **Root cause:** unlike `read`/`delete` (which guard via the share token regex) and unlike
  `publish_share` (which calls `read()` first), `revoke_share` reached `mapping_path`/`gh` with the raw
  route token. Exploitability was bounded (the `{token}` route won't match a `/`), but a malformed
  token should never reach a filesystem path or a subprocess argv.
- **Fix:** added a public `is_valid_token()` to `app/storage/share.py` and guard `revoke_share`
  with it up front (404 on a malformed token), before any filesystem/`gh` work.

## BUG-006 — Share dialog clips its action buttons on a short viewport
- **Status:** FIXED
- **Severity:** medium (the link/Open/Done/Delete actions become unreachable; user is stuck)
- **Where:** `ui/styles.css` `.share-dialog` — `overflow: hidden` with no `max-height`.
- **Root cause:** the dialog grows to its natural (tall) content height; on a short viewport
  (landscape phone, ~450–550px) it extends past the screen and `overflow:hidden` clips the bottom
  action row — the user can't reach Done/Delete. The sibling `.builder` dialog does it correctly
  (`max-height: 90vh; overflow: auto`).
- **Fix:** add `max-height: 90vh; overflow: auto` to `.share-dialog` (mirror `.builder`).

## BUG-007 — Helm Prometheus scrape annotation targets the wrong port
- **Status:** FIXED
- **Severity:** medium (broken metrics scrape — `up==0`, `AgentDown` fires — when `service.port` is overridden)
- **Where:** `deploy/helm/.../templates/deployment.yaml` pod annotation
  `prometheus.io/port: {{ .Values.service.port | quote }}`.
- **Root cause:** pod-level Prometheus discovery scrapes the POD IP at the annotated port, but the
  container always listens on the hardcoded `containerPort: 8000` / `PORT=8000`. `--set service.port=8080`
  pointed the scrape at a port nothing listens on. The Kustomize base hardcodes `"8000"` and is correct —
  only the Helm chart drifted.
- **Fix:** annotate the container port (`prometheus.io/port: "8000"`), matching the Kustomize base.
  Verified with `helm template --set service.port=8080` → annotation stays `"8000"`.

## BUG-008 — `run.sh` `read_env` deletes every internal space/quote in a value
- **Status:** FIXED
- **Severity:** low (only the startup HOST/PORT/PROVIDER/KEY reads; KEY drives a non-blocking warning)
- **Where:** `run.sh` `read_env()` — `... | tr -d ' "'\''`.
- **Root cause:** `tr -d` strips spaces/quotes EVERYWHERE, so `HOST="my host"` became `myhost` and a
  spaced/quoted value was silently corrupted; the "no API key" warn-check could misfire.
- **Fix:** strip only SURROUNDING whitespace/quotes via `sed -E "s/^[[:space:]'\"]+//; s/...$//"`.
  Verified: `HOST="my host"` → `my host`, padded `LLM_PROVIDER` trimmed, `PORT=8000` unchanged.

## BUG-009 — Over-long session id 500s the artifact + bundle routes (ENAMETOOLONG)
- **Status:** FIXED
- **Severity:** medium (unhandled server error / 500 on adversarial-but-trivial input; scanners hit it)
- **Where:** `app/main.py` `session_artifact` and `_resolve_bundle` (→ `/bundle/{id}` and
  `/bundle/{id}/report-card.html`).
- **Trigger:** `GET /api/sessions/<2000-char-id>/artifact?path=x.png` (or the same long id on either
  bundle route) → **500**. Found by adversarial HTTP fuzzing.
- **Root cause:** `base.is_dir()` (and, with a real session, `candidate.is_file()`) call `stat()` on a
  path whose component exceeds `NAME_MAX` (255 bytes). When `sessions_root` exists, that raises
  `OSError(errno 36, ENAMETOOLONG)`, which propagates uncaught → 500. (Reproduced: `is_dir()` raises;
  the route had no `except OSError`.) Other routes were clean — `delete`/`share`/`namespace` validate
  the id shape before any filesystem stat.
- **Fix:** wrap the path-resolution/stat block in `try/except OSError -> HTTPException(404) from None`
  in both routes — a malformed/over-long id now reads as a clean 404 (consistent with traversal),
  never a 500. HTTPException isn't an OSError, so the explicit 404s still propagate.
- **Tests:** `tests/test_artifacts.py` — long sid + long path (artifact), long sid (both bundle routes).

## Round 3 — turn-lifecycle deep audit + HTTP fuzzing (verified sound)
- A focused deep read of the race-prone live-turn lifecycle (`app/agent/loop.py`, `channel.py`,
  `lifecycle.py`, `ws_schemas.py`) found **no actionable bugs**: the resume-cursor window math, the
  approval park/resume bookkeeping, steer-drain (no `await` between read and reset), cancellation/slot
  accounting, and the busy/persist-on-exception `finally` paths all have correct, test-backed guards.
- Adversarial HTTP fuzzing (long/unicode/traversal/oversized inputs across every endpoint) was clean
  except BUG-009 above. A live WS chat turn was driven end-to-end (probes → streaming `assistant_delta`
  → `assistant_text` → `done`) with no errors.

## Round-2 minor items (fixture-only, not fixed)
- `ui/preview.html` uses a stale `class="builder-btn"` (no CSS rule) and lacks the `#share-chat`
  button/dialog that `index.html` has. preview.html is a dev fixture, not the served app (app.js
  guards every `getElementById` with `if (el)`, so nothing throws); not a live-user bug. Left as-is
  to avoid churning a test fixture.

## Considered but deliberately NOT changed
- **`await app.state.runs.cancel(session.id)` blocks the WS receive loop up to ~5s** (`main.py`,
  cancel control-frame handler). Real latency nuance, but the inline `await` intentionally frees the
  concurrency slot and lets the turn fully unwind before the next frame is read; making it
  fire-and-forget risks a race with an immediately-following new turn. Left as-is (low value / real risk).
- **`scatterPlot`/`sparkline` non-finite hardening, font-link fail-loud assertion, resume-cursor
  empty-buffer flash** — either unreachable with real inputs or cosmetic; not worth the churn/risk now.

## Verified NON-bugs (claims that didn't survive scrutiny)
Logged so they aren't re-investigated. A background UI hunter flagged these; the code is correct:
- **Trend delta "prints raw digits"** — false: backend `history.trend()` already
  `round(delta_pct, 2)`s the value (`app/storage/history.py:294`), so the chip shows ≤2 decimals.
- **`sparkline` NaN from a null point value** — not reachable: `trend()` *skips* records with a
  `None` value (`history.py:276-277`) and report values are schema-validated finite numbers.
  (`sparkline` is less defensive than `resSpark`, but no real input reaches it non-finite.)
- **Share viewer renders a live, clickable approval gate (`ws.send` on null `ws`)** — not
  reachable: `create_share` strips every `approval_request` item at mint time
  (`app/main.py:602`), so a shared snapshot never contains a live gate.

## HTTP API probe — robustness confirmed (no bugs)
Drove the REST surface like a user; all behaved correctly:
- `GET /api/sessions/{bad}/artifact` → 422 (FastAPI requires the `path` query param before the
  handler runs). Not a bug — the UI always supplies `path`; a bare hit is simply rejected.
- `GET /api/share/{bad}` → 404; `DELETE /api/sessions/{bad}` → 404; `DELETE /api/share/{bad}` → 404.
- `GET /api/history/trend` with no `metric` → 422; with an unknown metric → 200 + `{error, available_metrics}`.
- Full share lifecycle (create → read → page.html (257 KB) → revoke → read=404) works end to end.
- `DELETE /api/namespaces/no_namespace` correctly removes all no-namespace chats (documented sentinel).
