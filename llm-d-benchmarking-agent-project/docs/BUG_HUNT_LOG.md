# Bug-Hunt Log

Running log of bugs found by "playing with the app like a regular user" (driving the
HTTP/WS API + reading the client and backend). Each entry: symptom → trigger → root cause →
fix → status.

Started 2026-06-20.

## Summary (session of 2026-06-20)
**17 bugs found → verified → fixed → tested → merged** to local `main` across 6 fast-forward
batches; full suite green (2014 passed / 41 skipped), ruff + mypy clean. Method: fan out read-only
subagents over disjoint areas, **verify every lead against source before fixing** (~70% of
"high-confidence" agent leads were debunked), then drive the live app adversarially. The
highest-value bugs (009, 010 — unhandled `OSError`/`ValueError` → 500 on the artifact/bundle routes)
were found by **adversarial HTTP fuzzing**, not static review.

Bugs 011–015 (round 5, resume): backend crash-hardening on malformed input + two frontend UX bugs.

Dynamic end-to-end checks that passed clean (no bugs): a live WS chat turn, the malformed-frame WS
fuzz (socket survived every bad frame), and a **full SIMULATE=1 benchmark flow**
(probe → plan → approve → standup → smoketest → run → report → summary, no errors). Severity: most
bugs low/medium; none were crashes of the core flow.

| # | Where | One-liner |
|---|-------|-----------|
| 001 | ui/app.js | dead duplicate `fmtNum` (hoist shadow) |
| 002 | ui/app.js | resilience card `undefined/N classified correctly` |
| 003 | app/storage/share.py | non-atomic snapshot write |
| 004 | app/main.py | `/ws?after_seq=²` crashed the handshake |
| 005 | app/main.py | `revoke_share` hit FS + `gh` with unvalidated token |
| 006 | ui/styles.css | share dialog clipped its buttons (short viewport) |
| 007 | deploy/helm | Prometheus scrape annotation wrong port |
| 008 | run.sh | `read_env` deleted internal spaces/quotes |
| 009 | app/main.py | over-long id → 500 (ENAMETOOLONG) on artifact/bundle |
| 010 | app/main.py | NUL byte → 500 (ValueError) on artifact/bundle |
| 011 | app/validation/report.py | `summarize_report` crashes on a malformed (non-dict-child) report |
| 012 | app/tools/execute.py | results-store non-iterable `paths` → TypeError before allowlist |
| 013 | app/tools/discover.py | `discover_stack` crashes on a non-dict component element |
| 014 | ui/app.js | Builder "Send" refused to steer + clobbered the composer draft mid-turn |
| 015 | ui/styles.css | dead next-steps/report-action controls in the read-only shared viewer |
| 016 | ui/app.js | per-pod resource sparklines grafted the previous run's pods onto a 2nd run (same chat) |
| 017 | ui/app.js | trend-metric dropdown was one-shot — metrics from later runs never appeared |
| 018 | app/llm/openai_provider.py | OpenAI-compatible provider crashes on an empty `choices` array |
| 019 | ui/app.js | WS `onclose`/`onmessage` not socket-bound → rapid chat switch spawns duplicate sockets |
| 020 | app/storage/history.py | corrupt `stored_at` (null/string) → `TypeError` crashes the WHOLE history list + all trends |
| 021 | app/storage/provenance.py | truthy non-numeric `created_at` → `TypeError` crashes the WHOLE bundle list |
| 022 | app/storage/autotune.py | non-numeric trial `index` → `TypeError` crashes `load()` (whole autotune log) |
| 023 | app/orchestrator/job.py | non-numeric Job count (`active`/`succeeded`/`failed`) → `int()` `ValueError` aborts the watch loop |

**Security observation (NOT auto-fixed — needs maintainer decision):** the *documented* relaxed-flag
policy (`security/allowlist.yaml` lines 42-48) accepts UNKNOWN flags on an allowlisted command,
metachar-screened but not value-constrained, never changing the mode. Since the metachar screen
(`app/security/allowlist.py` `_DANGEROUS`) permits `:` and `=`, an unknown flag like
`kubectl get pods --as=system:admin` (RBAC impersonation), `--token=…`, or `--server=…` would pass
on an AUTO-RUN read-only command with no approval. This is a deliberate, thoroughly-documented design
trade-off (the hard boundary is `shell=False` + the metachar screen + cluster RBAC), so it was left
unchanged. Suggested hardening if desired: deny a small set of known-dangerous flags
(`--as`, `--as-group`, `--token`, `--server`, `--username`, `--password`) even under the relaxed policy.

---

## BUG-011 — `summarize_report` crashes on a malformed (non-dict-child) report
- **Status:** FIXED
- **Severity:** medium (an AttributeError escapes as an agent error, not a clean tool error)
- **Where:** `app/validation/report.py::summarize_report`, reached PRE-validation by
  `app/tools/compare.py` (`compare_reports`) and `app/tools/multiharness.py` (`compare_harness_runs`)
  — both summarize BEFORE checking `validation.valid`.
- **Root cause:** the top-level extractions only guarded `report` (not the child VALUES), and the
  model chain / `run.time` lookups did `.get(...).get(...)` on a present-but-non-dict child. A report
  parsing to a dict but with e.g. `run: "2026"` or `scenario.stack: "x"` → `AttributeError`.
- **Fix:** a `_d()` non-dict→`{}` coercion applied at EVERY nesting level (run/scenario/results/agg/
  requests/latency/throughput/stack-element/standardized/model/time), honoring the docstring's
  "every lookup is optional and missing pieces are simply omitted." Regression in `test_report_validation.py`.

## BUG-012 — results-store non-iterable `paths` → TypeError before the allowlist
- **Status:** FIXED
- **Severity:** low (niche team-sharing path; an uncaught TypeError instead of a clean ToolError)
- **Where:** `app/tools/execute.py` `_build_results_store_argv` — `[str(p) for p in (store.get("paths") or [])]`.
- **Root cause:** `paths` read from the unconstrained `store` dict and iterated with no shape check;
  a scalar (`paths: 5`) raised `TypeError` at argv-build time, before `allowlist.validate` could reject
  it; a bare string silently iterated per-character.
- **Fix:** `isinstance(paths, (list, tuple))` guard → a self-correctable `ToolError`. Regression in `test_results_store.py`.

## BUG-013 — `discover_stack` crashes on a non-dict component element
- **Status:** FIXED
- **Severity:** low (robustness — a garbled discovery stream; trusted subprocess in practice)
- **Where:** `app/tools/discover.py::_summarize_stack` — iterated components calling `comp.get(...)`;
  `_parse_components` validates only list-ness, not element shape.
- **Root cause:** a JSON list with a non-dict element (or non-dict `standardized`/`model`/`accelerator`)
  → `AttributeError`.
- **Fix:** same `_d()` coercion over comp/std/meta/model/accelerator. Regression in `test_stack_discovery.py`.

## BUG-014 — Builder "Send" refused to steer and clobbered the composer draft mid-turn
- **Status:** FIXED
- **Severity:** medium (silent no-op + data loss: overwrites whatever the user was typing)
- **Where:** `ui/app.js::submitBuilder` — `if (busy || !ws || ...) { input.value = text; return; }`.
- **Root cause:** the whole app design allows sending WHILE a turn runs ("steer" — see
  `sendUserMessage`'s explicit no-busy-guard), but the Builder was the lone path that refused mid-turn
  AND destroyed the composer's existing draft by assigning `input.value = text`.
- **Fix:** drop `busy` from the guard (gate only on socket state); let `sendUserMessage` handle the steer.

## BUG-015 — Dead interactive controls in the read-only shared viewer
- **Status:** FIXED
- **Severity:** medium (a share recipient sees clickable controls that silently do nothing / 404)
- **Where:** `ui/styles.css` `body.share-view` hide-list omitted `.next-steps` + `.report-actions`.
- **Root cause:** a shared snapshot keeps report/analysis tool results (only `approval_request` items
  are stripped at mint time), so the viewer renders the "Suggested next steps" chips (call
  `sendUserMessage` → no-op with no socket) and the report-action buttons (Reproduce/Save no-op;
  Export opens an API URL that 404s for a recipient with no backend).
- **Fix:** add `.next-steps` + `.report-actions` to the `body.share-view { display:none }` rule (the
  approval-card sibling concern from an earlier pass was already covered by the mint-time strip).

## BUG-016 — Per-pod resource sparklines graft the previous run's pods onto a second run
- **Status:** FIXED
- **Severity:** low (cosmetic trend staleness — two runs in one chat, no reload)
- **Where:** `ui/app.js` — `cur.resourceHistory` was only reset on new-chat / full pane rebuild, never
  between two runs in the same chat. `clearResourceStats` (on `done`) only collapses the panel.
- **Fix:** set a `cur.resourceRunEnded` flag on `done`; on the next run's first `resource_stats` tick,
  reset `cur.resourceHistory`. Keyed off the `done` flag (NOT the `resourceActive` transition, which a
  manual mid-run collapse also flips and must not wipe a running run's history).

## BUG-017 — Trend-metric dropdown was one-shot; later runs' metrics never appeared
- **Status:** FIXED
- **Severity:** low-medium (a metric introduced by a later run couldn't be trended without a reload)
- **Where:** `ui/app.js::populateTrendMetrics` — short-circuited forever on a `trendMetricsLoaded` flag.
- **Fix:** drop the one-shot flag; reconcile the incoming metrics against the dropdown's CURRENT
  options and append only the new ones (selection preserved).

---

## BUG-018 — OpenAI-compatible provider crashes on an empty `choices` array
- **Status:** FIXED
- **Severity:** low-medium (caught by the agent loop's broad `except`, but degraded a recoverable
  provider-shape into an opaque `IndexError: list index out of range` with no actionable message).
- **Where:** `app/llm/openai_provider.py::OpenAIProvider.chat` — `resp.choices[0].message` /
  `resp.choices[0].finish_reason` indexed `choices[0]` with no guard. An OpenAI-compatible server
  (vLLM / llm-d under content-filter or error conditions — the file's own stated target) can return
  a 200 with an empty `choices` array. The adjacent `_usage_from` is explicitly written to
  "never crash, return zeros," so the unguarded index was an inconsistency in the same contract.
- **Fix:** guard `choice = (resp.choices or [None])[0]`; on `None` raise a clear
  `ProviderError("the model server returned no choices (empty response)")`.
- **Regression test:** `tests/test_llm_caching_usage.py::test_openai_empty_choices_raises_clear_provider_error`.

---

## BUG-019 — WS `onclose`/`onmessage` not socket-bound → rapid chat switch spawns duplicate sockets
- **Status:** FIXED
- **Severity:** medium (connection leak + double-rendered events under rapid chat switching / flaky links)
- **Where:** `ui/app.js::connect` + `switchTo` — the socket event handlers closed over the module-global
  `ws` and a single shared `switching` boolean instead of being bound to their own socket instance.
- **Trigger:** switch chats faster than the prior socket's `close()` fires its (always-async) `onclose`
  — e.g. A→B→C in quick succession, or any switch while a `close` is still in flight. `switchTo` sets
  `switching = true` once and opens a new socket; `connect` reassigns the global `ws` and registers a
  fresh `onclose` on the NEW socket, but each OLD socket still holds its own `onclose` closure.
- **Root cause:** a single shared `switching` flag cannot gate *multiple* in-flight deliberate closes.
  The first old socket's `onclose` consumes `switching` (sets it false); the second old socket's
  `onclose` then sees `switching === false`, falls through to the auto-reconnect branch, and calls
  `connect(currentSession, …)` — spawning a DUPLICATE socket to the now-active chat. Both sockets then
  receive the same events and both run `handle()` → double-rendered events, doubled `cur.lastSeq`
  advancement, and a leaked/flapping connection. (Confirmed with a focused simulation: old logic fires
  2 spurious reconnects on an A→B→C switch; the fix fires 0.)
- **Fix:** bind every handler to the socket instance it was created for (`const sock = new WebSocket(...)`),
  and gate `onopen`/`onclose`/`onerror`/`onmessage` on `sock === ws`. A superseded socket (one a switch
  or reconnect has already replaced) is then inert: its deferred `onclose` returns early instead of
  reconnecting, and its late `onmessage` can't double-render. This makes the fragile shared `switching`
  flag unnecessary, so it was removed entirely.
- **Regression test:** `tests/test_ui_frontend.py::test_ws_handlers_are_socket_bound`.

---

## BUG-020/021/022 — corrupt on-disk JSON crashes the WHOLE storage list/trend via a non-numeric sort key
- **Status:** FIXED (these are CAND-A/B/C below, promoted from "queued" to fixed)
- **Severity:** medium — ONE corrupt/forged file takes down an entire list view, not just its own row.
- **Where + trigger (each reproduced before fixing → `TypeError: '<' not supported between …`):**
  - **BUG-020 `app/storage/history.py`** — `HistoryStore.list` (`out.sort(key=r.stored_at, reverse=True)`)
    AND `trend()` (`sorted(records, key=r.stored_at)`). `_read` reconstructs `HistoryRecord` from JSON
    with no per-field type-check, so a record file with `"stored_at": null` (or a string) crashes BOTH
    the history list and EVERY trend — and the analyzer's history pull — for ALL records, not just the
    bad one.
  - **BUG-021 `app/storage/provenance.py`** — `BundleStore.list` sorted on `b.get("created_at") or 0.0`,
    which only neutralizes *falsy* values; a truthy non-numeric `created_at` (forged/corrupt string)
    still crashed the whole bundle list.
  - **BUG-022 `app/storage/autotune.py`** — `AutotuneStore.load` (`trials.sort(key=t.index)`). `Trial`'s
    `index: int` annotation isn't enforced (built straight from JSON), so a `"index": null`/string trial
    crashed `load()` — violating the class's documented "a corrupt log degrades to empty, never crashes".
- **Root cause:** Python 3 raises `TypeError` when a `sorted`/`list.sort` key mixes `None`/`str` with
  numbers. The on-disk record is reconstructed with NO type validation (same class as BUG-011/012/013).
- **Fix:** a tiny local `_as_num(v)` helper in each module — returns the value when it's a real number
  (`int`/`float`, `bool` excluded), else `0.0` — used as the sort key. A corrupt record now stays VISIBLE
  (sorted as oldest) rather than crashing the list or being dropped. Minimal blast radius, no behavior
  change for valid data.
- **Reproduced first:** a standalone script confirmed all four call sites (`history.list`, `trend`,
  `provenance.list`, `autotune.load`) raised `TypeError` on the crafted corrupt files, then returned
  cleanly after the fix.
- **Regression tests:** `tests/test_history.py::test_list_and_trend_survive_corrupt_stored_at`,
  `tests/test_provenance.py::test_list_survives_non_numeric_created_at`,
  `tests/test_autotune.py::test_load_survives_non_numeric_index`.

---

## BUG-023 — non-numeric Job count crashes `classify_job_status` (ValueError) → aborts the watch loop
- **Status:** FIXED (this is CAND-D below)
- **Severity:** medium (a forged/corrupt `kubectl get -o json` aborts the whole watch/reconstruct loop)
- **Where:** `app/orchestrator/job.py::classify_job_status` —
  `active = int(status.get("active", 0) or 0)` (and the same for `succeeded`/`failed`).
- **Trigger (reproduced):** a Job status with a non-numeric count, e.g. `{"status": {"active": "lots"}}`
  → `int("lots")` raises `ValueError: invalid literal for int()`. `kubectl` normally emits integer
  counts, but a corrupt/forged status object propagates the `ValueError` straight out of classify,
  aborting `watch()`/`reconstruct()` (the cluster is the source of truth those loops read).
- **Fix:** an `_as_int(v)` helper (`int(v or 0)` guarded by `except (TypeError, ValueError) -> 0`)
  used for all three counts — a non-numeric count reads as 0, so a Job with only bogus counts and no
  terminal signal classifies as PENDING instead of crashing. Same hardening class as BUG-020/021/022.
- **Regression test:** extended `tests/test_orchestrator_controller.py::test_classify_edge_cases`
  with a forged non-numeric-count status (asserts PENDING + zeroed counts, no raise).
- **CAND-E (the sibling success-heuristic edge) deliberately NOT changed** — see the candidates note
  below for the regression reasoning.

---

## Candidates surfaced 2026-06-20 (verified by source) — A/B/C+D now FIXED (2026-06-21), E deliberately declined
Two parallel hunters (orchestrator, storage) returned the findings below. Each was confirmed
real against source — a missing-guard on **malformed/forged on-disk or kubectl JSON**, not
reachable through the agent's normal flow. The three storage crashes (A/B/C) were fixed on
2026-06-21 as BUG-020/021/022 (see above). The two orchestrator items (D/E) remain queued:
- **CAND-A** ✅ **FIXED → BUG-022** (`app/storage/autotune.py`) — `AutotuneStore.load`'s
  `trials.sort(key=t.index)` crashed on a non-numeric `index`. Fixed with the defensive `_as_num` key.
- **CAND-B** ✅ **FIXED → BUG-020** (`app/storage/history.py`) — `HistoryStore.list`/`trend` sorts on
  `r.stored_at` crashed on a null/string value, breaking listing/trending for ALL records. Fixed with
  the defensive `_as_num` key (the record stays visible, sorted as oldest, rather than crashing/dropping).
- **CAND-C** ✅ **FIXED → BUG-021** (`app/storage/provenance.py`) — `BundleStore.list`'s
  `b.get("created_at") or 0.0` key still crashed on a truthy non-number. Fixed with the `_as_num` key.
- **CAND-D** ✅ **FIXED → BUG-023** (`app/orchestrator/job.py`) — `classify_job_status`'s
  `int(status.get(...))` crashed on a non-numeric count. Fixed with the `_as_int` helper.
- **CAND-E `app/orchestrator/job.py`** — ⛔ **deliberately NOT fixed** (declined 2026-06-21). The
  proposed `failed == 0` guard on the `succeeded>0 and active==0 and not _cond("Failed")` fallback
  would introduce a **regression**: a `backoffLimit>0` Job that failed-then-succeeded has
  `succeeded>0, failed>0`, and in the brief K8s window *before* the `Complete` condition is written it
  relies on exactly this fallback — the guard would misclassify that genuinely-succeeded Job as FAILED.
  The original mis-report (succeeded=1/failed=1 → SUCCEEDED) is **not reachable for agent Jobs** (all
  single-pod `backoffLimit:0`, where succeeded and failed can't co-occur — see `orchestrator/CLAUDE.md`);
  it only affects hand-applied multi-pod Jobs the agent never creates. Fixing an unreachable theoretical
  edge at the cost of a real retry-path regression is the wrong trade — left as-is.

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

## BUG-010 — NUL byte in artifact/bundle path 500s (ValueError, sibling of BUG-009)
- **Status:** FIXED
- **Severity:** medium (500 on `%00` in a path/sid; classic injection-probe input)
- **Where:** `app/main.py` `session_artifact` + `_resolve_bundle` — the BUG-009 fix caught only `OSError`.
- **Trigger:** `GET /api/sessions/<sid>/artifact?path=a%00b.png` (or `%00` in the sid) → **500**.
- **Root cause:** an embedded NUL byte makes `Path.resolve()` raise **`ValueError`** ("embedded null
  byte"), which is NOT an `OSError`, so the BUG-009 `except OSError` missed it. (The bundle *bundle_id*
  was already safe via `_safe_id`, but the bundle *sid* shared the gap.)
- **Fix:** broaden both guards to `except (OSError, ValueError)`.
- **Tests:** extended `tests/test_artifacts.py::test_artifact_route_404_for_overlong_ids` with a NUL-byte path.

## Round 3 — turn-lifecycle deep audit + HTTP/WS fuzzing (verified sound)
- **WebSocket frame fuzzing:** drove non-JSON, non-object JSON, unknown/missing `type`, extra fields,
  wrong field types, bad/empty/unknown approval `request_id`, a 2 MB text frame, and a binary frame.
  The handler returned a structured `protocol_error` (or harmlessly ignored a stale approval) for every
  one and **kept the socket alive** — no crash, no hang. (Two defensible non-bugs: an unknown approval
  `request_id` gets no response — idempotent stale-approval handling; chat text has no hard size cap.)
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
