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
| 024 | ui/app.js | `splitTableRow` unmatched backtick left `inCode` stuck → collapsed the rest of a table row into one cell |
| 025 | ui/app.js | live `streamBubble` not snapshotted per chat → mid-stream chat switch appended deltas into the previous pane |
| 026 | ui/app.js | approval `resolve` sent on the socket with no `readyState` guard → throws if clicked mid-reconnect |
| 027 | app/validation/report.py | `load_report` raised raw parse exception → opaque tool error across 6 tools on a corrupt report |
| 028 | app/tools/probe.py | `probe_environment(spec=…)` followed `..` traversal, parsing an arbitrary YAML file into image_tags |
| 029 | app/orchestrator/faults.py | `classify_failure` crashed on a non-list `conditions`/`containerStatuses` or non-dict pod element |
| 030 | app/capacity/planner.py | capacity verdict read `feasible:true` when KV-cache sizing never ran (count-summary mistaken for sizing proof) |
| 031 | app/tools/multiharness.py | `compare_harness_runs` aborted the whole comparison on one corrupt report (sibling `compare_reports` skips it) |
| 032 | app/main.py + app/agent/channel.py | reconnect mid-turn double-rendered events (buffer replay overlapped the live emit during attach) |
| 033 | app/main.py | env pre-probe `create_task` untracked → GC-able mid-probe (lost reference) |
| 034 | deploy/ RBAC + app/packaging/assets.py | Role lacked `configmaps` → every in-cluster checkpointed DOE sweep fails Forbidden |

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

## BUG-024 — `splitTableRow` unmatched backtick collapses the rest of a markdown table row
- **Status:** FIXED
- **Severity:** low (garbled table rendering when the agent emits a stray backtick inside a table row)
- **Where:** `ui/app.js::splitTableRow` — `else if (ch === "\`") { inCode = !inCode; ... }`.
- **Trigger (reproduced via a standalone node sim):** a table body/header row containing a single
  UNmatched backtick, e.g. `| a\` | b | c |`. The lone backtick flips `inCode` true and it never flips
  back, so every `|` after it is treated as inside a code span and NOT split — the row collapses from
  3 cells to 1 (`["a\` | b | c"]`), garbling the rendered table.
- **Root cause:** the splitter toggled code-mode on EVERY backtick, assuming they always come in pairs.
  A matched pair correctly protects an embedded `|` (the intended GFM feature); an odd/stray backtick
  has no closing partner and shouldn't open a protected span at all.
- **Fix:** pre-compute the set of backtick positions that form a matched pair (indices 0&1, 2&3, …; a
  trailing odd backtick is left out) and toggle `inCode` only on those. Matched code spans still protect
  their pipes; a stray backtick is now inert. (node sim: stray-backtick row `["a\` | b | c"]` →
  `["a\`","b","c"]`; the matched-pair `| \`a|b\` | c | d |` case is unchanged.)
- **Regression test:** `tests/test_ui_frontend.py::test_table_row_split_pairs_backticks`.

---

## Round 6 (2026-06-21) — two fresh parallel hunts (backend tools/validation + new app.js), verified then fixed
A second app.js hunter and a backend `app/tools/`+`app/validation/` hunter ran in parallel; every lead
was re-verified against source before fixing (one was debunked, see the non-bug note). 4 fixed (BUG-025–028).

## BUG-025 — live streaming bubble not snapshotted per chat → mid-stream switch writes into the wrong pane
- **Status:** FIXED
- **Severity:** medium (a destination chat's assistant reply silently vanishes after a chat switch)
- **Where:** `ui/app.js` — `streamBubble`/`streamText` are module globals NOT saved in `snapshotActive`
  / restored in `activate` / initialized in `makeRecord` (every other live-turn global is).
- **Trigger:** switch away from a chat mid-stream (an `assistant_delta` created `streamBubble`) to another
  running chat that is a cache-hit (its `ready` carries `resume.incremental=true`, so `clearActivePane()`
  — the only place that calls `resetStreamBubble()` — is skipped). The destination's next `assistant_delta`
  / `assistant_text` sees the stale truthy `streamBubble` (a node in the PREVIOUS, now-detached pane) and
  appends/finalizes there; the destination pane never gets a fresh bubble.
- **Root cause:** `streamBubble` is per-turn live state but was left out of the per-chat snapshot machinery,
  so it leaked across switches (same class as BUG-019: turn state not bound to its owner).
- **Fix:** snapshot/restore `streamBubble`+`streamText` per chat in `makeRecord`/`snapshotActive`/`activate`,
  exactly like `toolEls`/`turnUsage`. A switch now restores the destination chat's own (or null) bubble;
  returning to the original chat resumes its in-progress stream into its own node.
- **Regression test:** `tests/test_ui_frontend.py::test_stream_bubble_is_snapshotted_per_chat`.

## BUG-026 — approval `resolve` sends on the socket with no readyState guard (throws mid-reconnect)
- **Status:** FIXED
- **Severity:** low (clicking Approve/Reject during a brief reconnect throws; the click no-ops with no feedback)
- **Where:** `ui/app.js` `addApprovalCard`'s `resolve` — `ws.send(...)` with no guard / try-catch, unlike
  `cancelRun`/`sendUserMessage` which both check `ws.readyState === WebSocket.OPEN`.
- **Trigger:** disconnect (the 1.5s auto-reconnect window) leaves the approval buttons clickable —
  `setEnabled(false)` only disables the composer, not the card buttons — so clicking Approve/Reject calls
  `ws.send` on a CLOSING/CLOSED socket → `InvalidStateError`.
- **Fix:** guard `if (!ws || ws.readyState !== WebSocket.OPEN) return;` at the top of `resolve`, before any
  state mutation / optimistic "✓ Approved" UI, so the gate stays clickable and the decision can be re-sent
  once reconnected (mirrors `cancelRun`).
- **Regression test:** `tests/test_ui_frontend.py::test_approval_resolve_guards_socket_before_send`.

## BUG-027 — `load_report` raised a raw parse exception → opaque tool error across 6 tools
- **Status:** FIXED
- **Severity:** medium (a present-but-corrupt report makes 6 tools fail with an opaque `tool '...' raised`)
- **Where:** `app/validation/report.py::load_report` — `json.loads`/`yaml.load` with NO error handling.
  Unguarded callers: `compare.py`, `analyze.py`, `multiharness.py`, `reproducibility.py`, `history.py`,
  `autotune.py`.
- **Trigger (reproduced end-to-end):** `compare_reports`/`analyze_results`/… on a run dir whose
  `benchmark_report_v0.2.{json,yaml}` is present but truncated/corrupt (e.g. an OOM-killed run) →
  `json.JSONDecodeError`/`yaml.YAMLError` (NOT `ReportError`) escapes as `tool '...' raised: ...`. The
  earlier corrupt-report hardening (BUG-011) covered `summarize_report`; the PARSE step runs before it.
- **Fix:** harden `load_report` to catch `(OSError, ValueError, yaml.YAMLError)` and `raise ReportError`
  (names the bad file) — fixes all 6 callers' error message at once. Then route it into the existing
  `skipped` channel in `compare_reports` + `analyze_results` (`try load_report / except ReportError →
  skipped`) so one corrupt report is skipped, not fatal for the whole comparison/analysis.
- **Regression test:** `tests/test_report_validation.py::test_load_report_raises_reporterror_on_corrupt`.

## BUG-028 — `probe_environment(spec=…)` followed `..` traversal out of the scenarios dir
- **Status:** FIXED
- **Severity:** low (read-only info disclosure: parse an arbitrary YAML-parseable host file into image_tags)
- **Where:** `app/tools/probe.py::_parse_image_tags` — `bench_repo/config/scenarios/<spec>.yaml` joined with
  no containment check; `spec` is a free-form `str | None`.
- **Trigger:** `probe_environment(checks=["cluster_preconditions"], spec="../../../../etc/hosts")` →
  the `..` segments escape the scenarios dir and the file is `yaml.safe_load`-ed into `image_tags`.
- **Fix:** after resolving the path, require `path.resolve().is_relative_to((bench_repo/config/scenarios)
  .resolve())`, else return `[]` (the same 'unknown' an absent file yields). Read-only, no write/crash.
- **Regression test:** `tests/test_infra_preconditions.py::test_image_tags_spec_rejects_path_traversal`.

### Verified NON-bug this round (debunked before fixing)
- **Approval `resolve` calls `startWorking()` instead of `resumeWorking()`** (flagged "resets elapsed + wipes
  token tally"). NOT a bug: the `approval_request` handler calls `stopWorking()` so the working indicator is
  HIDDEN during the gate (no visible jump), and the backend emits CUMULATIVE turn usage so the next usage
  event restores the full tally. `resumeWorking(Date.now()-workStart)` would be WORSE — it would count the
  user's deliberation time at the gate as agent "working" seconds. `startWorking()` is correct. Left as-is.

### Low-value backend candidates surfaced this round — NOT fixed (queued)
- `probe_environment(checks=[…all-invalid…])` returns `{}` with no signal the check names were wrong
  (`probe.py` `wanted=[c for c in checks if c in _ALL_CHECKS]`). Low value (agent supplies valid names);
  fix would be `return {"error", "valid_checks"}` when `checks` non-empty but `wanted` empty.
- `autotune_search(action="propose_next_config")` returns `ok:True` for an out-of-bounds candidate when
  `knobs` is omitted (empty `bounds` → the unknown-key/type checks are skipped). Niche; fix: `if not bounds:
  return {"ok": False, "reason": "needs the plan's knobs to validate"}`.

---

## Round 7 (2026-06-21) — orchestrator hunt: 1 fixed (BUG-029), 1 design concern surfaced (not patched)

## BUG-029 — `classify_failure` crashes on malformed pod JSON (non-list conditions/containerStatuses, non-dict pod)
- **Status:** FIXED
- **Severity:** low (defensive — not reachable from real `kubectl get pods -o json`, but it violated the
  module's documented "classification never crashes" invariant, same class as BUG-023)
- **Where:** `app/orchestrator/faults.py` — `_container_statuses` / `_scan_unschedulable` use the
  `(... or [])` fallback, which only catches FALSY values; a truthy non-list (`conditions: "x"`,
  `containerStatuses: "x"`) was iterated element-by-element → `.get(...)` on a `str`/`int` →
  `AttributeError`; a non-dict/`None` pod element crashed `pod.get(...)`.
- **Trigger (reproduced):** `classify_failure(job_status, pods)` with a malformed/forged pods list →
  `AttributeError` escapes `diagnose()` as an opaque tool error instead of degrading to UNKNOWN.
- **Fix:** `_container_statuses` returns `[]` for a non-list and filters non-dict elements;
  `_scan_unschedulable` skips a non-list `conditions` and non-dict cond entries; `classify_failure`
  filters non-dict pod elements up front. Real signals (OOM, etc.) still classify; malformed input → UNKNOWN.
- **Regression test:** `tests/test_orchestrator_faults.py::test_classify_failure_never_crashes_on_malformed_pods`.

### Design concern surfaced — watch-timeout dead-letters a still-running Job (NEEDS MAINTAINER DECISION, not patched)
- **Where:** `app/orchestrator/controller.py:329-336` (the `else` branch) + sweep checkpoint at `:406-415`.
- **Observation:** when `max_wait` elapses while a Job is still `active`/`pending`, `watch()` correctly
  returns the non-terminal status (honoring the documented invariant "hitting max_wait is NOT a failure"),
  but `run_with_retries` then synthesizes `Failure(TIMEOUT)` and — since TIMEOUT ∉ retryable — dead-letters
  it. In a checkpointed sweep this records the treatment `COMPLETED, dead_lettered=True`, so resume SKIPS it
  forever even though the Job may still be running / may have succeeded. The line is explicitly commented, so
  the author was aware; and CLAUDE.md's retry-decision section DOES list TIMEOUT as a deterministic
  dead-letter — so the two documented invariants are in tension.
- **Why NOT patched here:** the correct behavior is a genuine design decision, and every concrete fix is
  risky: making the synthesized TIMEOUT *retryable* would resubmit a FRESH Job (`-a2`) while the original is
  still running (double-run, orphaned Job); returning an "inconclusive / still-running" outcome and recording
  it `IN_FLIGHT` (so resume re-checks via `reconstruct`) is a substantial semantic change to the
  retry/sweep/checkpoint contract that existing tests + the agent's remediation advice rely on. This warrants
  a maintainer call (and likely a distinct `RunOutcome` "inconclusive" state), not a blind in-hunt patch.
- **Recommended direction:** distinguish a *client watch timeout* (Job still running) from a real
  `DeadlineExceeded` TIMEOUT fault; for the former, do NOT `record_completed`/dead-letter — leave it
  in-flight so a same-`sweep_id` resume re-checks the cluster (the source of truth) and re-runs only if the
  Job truly didn't finish. (Default `max_wait` is 3600s/7200s, so this only bites very long benchmarks.)

---

## Round 8 (2026-06-21) — security hunt (`app/security/`): boundary confirmed hardened; 1 LATENT defect surfaced
A focused hunt over `allowlist.py` / `auth.py` / `runner.py` found **no exploitable** allowlist/approval/
auth bypass, env-leak, or path-escape — the metachar screen, fail-closed default, env scrub, `(?!.*\.\.)`
path constraints, `re.fullmatch` anchoring, quota-before-approval ordering, constant-time token compare, and
the GET-only/`/healthz`/`/readyz` auth exemptions all hold under adversarial probing. One latent defect:

### SECURITY OBSERVATION (latent, NOT exploitable today) — **RESOLVED by BUG-042 (Round 16)**
- **Resolution:** fixed in Round 16 as **BUG-042** — read-only-trigger detection is now **region-aware**
  (`pre_tokens` → `pre_regions: list[(tokens, effective_flags)]`; a trigger is honored only against its OWN
  region's flag-dict), so a subcommand-OWN trigger sitting in the global pre-region no longer downgrades a
  mutating subcommand. The genuine global `--version` trigger and the intentional nested-subcommand
  propagation are preserved. **Nuance retained (honesty):** this was LATENT, not live-exploitable under the
  current pinned upstream — the subparser's `--dry-run` uses `default=argparse.SUPPRESS`, so a global-position
  `-n` IS honored as a real dry-run today; the fix is defense-in-depth so the security gate doesn't depend on
  an unobservable upstream default in a read-only repo we don't control. The original observation is preserved
  below for the record.
- **What:** a `read_only_trigger` flag in the **leading global region** downgrades an otherwise-MUTATING
  subcommand to `read_only` → **auto-run, no approval, no quota**. Reproduced:
  `validate(["llmdbenchmark","--version","standup","-p","myns"])` → `mode=read_only,
  requires_approval=False` (vs `standup` alone → `mutating`/approval; `--bogus standup` correctly stays
  `mutating`).
- **Where:** `app/security/allowlist.py:379` (`read_only_triggered = _has_read_only_trigger(pre_tokens, flags)`)
  + `:443-444`, reached from `:248` (`pre_tokens=pre` carries the global region into the subcommand walk).
- **Why it's NOT exploitable now:** the only executable-level `read_only_trigger` is `--version`
  (`security/allowlist.yaml:781`), which upstream `llmdbenchmark` registers as argparse
  `action="version"` (`llm-d-benchmark/llmdbenchmark/cli.py:1650`) — it prints and `sys.exit(0)` during
  `parse_args`, so `llmdbenchmark --version standup` exits before `standup` ever deploys. The validator's
  correctness here rests on that upstream early-exit ACCIDENT, not on its own invariant.
- **The latent danger:** the day anyone adds a SECOND executable-level `read_only_trigger` that does NOT
  early-exit (e.g. a future global `--explain`/`--show`), EVERY mutating subcommand on that executable
  becomes auto-runnable with the approval gate AND the per-session quota both silently bypassed.
- **Why NOT patched here:** the fix touches the security validator, where `pre_tokens` trigger-propagation
  is INTENTIONAL for nested subcommands (`allowlist.py:338`, comment "read_only_triggers from the global /
  outer region matter too") — a naive "don't downgrade from the global region" change risks breaking that
  intended nested-command behavior. The correct fix is a design decision for the maintainer.
- **Recommended fix:** add an explicit `exits_before_action: true` (or `neutralizes_command: true`) flag
  annotation; only allow a trigger found in the global/outer `pre_tokens` region to downgrade a matched
  mutating subcommand when it carries that annotation (mark `--version` with it). Subcommand-OWN-region
  triggers (e.g. `run --dry-run`) keep downgrading as today. Add a `tests/test_allowlist.py` case asserting
  a non-annotated global trigger does NOT downgrade a mutating subcommand.
- **Related:** the previously-documented relaxed-flag `--as` impersonation observation (a separate,
  deliberate design trade-off) — see the security observation near the top of this log.

---

## Round 9 (2026-06-21) — capacity/packaging/retention/compaction hunt: 1 fixed (BUG-030)
The hunt confirmed retention GC, share store, gist_publish, and context_mgmt compaction are
well-hardened (active-session bytes accounting, stub-length non-negative counter, tool-call/result
pairing, stale-gist fall-through, token traversal all guarded). One solid HIGH-severity bug:

## BUG-030 — capacity planner reports `feasible:true` when KV-cache sizing never actually ran
- **Status:** FIXED
- **Severity:** high (a confident WRONG "it fits" at the plan gate → user proceeds to a ~10-min standup
  that then OOMs / fails to load — the exact failure capacity pre-flight exists to prevent)
- **Where:** `app/capacity/planner.py` — `_SIZED_MARKER = "available gpu memory"` (the sizing-proof) +
  `classify_diagnostics` (`any_sized`).
- **Trigger (reproduced):** a `check_capacity` run (default `enforce=False` → `ignore_failures=True`) where
  one method emits the GPU-COUNT summary but its KV-cache sizing THROWS (e.g. a HuggingFace 401/offline
  config fetch for a large gated model → only a `WARNING:` line), while the other method is 0-replica
  skipped. `classify_diagnostics` returned `feasible=True, sizing_evaluated=True`.
- **Root cause:** `any_sized` keyed on `"available gpu memory"`, which matches the upstream GPU-COUNT
  summary line (`capacity_validator.py:206-210`, `"...total available GPU memory = X GB"`) — emitted
  BEFORE and INDEPENDENT of whether the KV-cache arithmetic ran. So `any_sized=True` cancelled the
  `(replica_skip and not any_sized)` bypass, and the sizing-exception (`:305`, only a WARNING under
  ignoreFailedValidation, so not `hard_infeasible`) read as feasible. The module's OWN comment already
  noted "the '...available GPU memory' line is NOT a fit verdict" — the marker contradicted it.
- **Fix:** key `any_sized` on the FIT-path KV-cache lines only — `_SIZED_MARKERS = ("allocatable kv cache
  memory", "per-request kv cache", "max concurrent requests")` (`capacity_validator.py:280-302`) — and add
  `_SIZE_FAILED_MARKER = "cannot estimate model memory or kv cache"` as a bypass signal so a thrown sizing
  also downgrades to `feasible=None`. Won't-fit verdicts are still carried by `_FAIL_MARKER`. Safe
  direction: the change can only turn a false `feasible:true` into the cautious `None`, never the reverse.
- **Regression test:** `tests/test_qafix_tools_capacity_history_config_report.py::test_classify_sizing_exception_is_inconclusive_not_feasible`
  (all 20 existing capacity tests still pass).

---

## Round 10 (2026-06-21) — final tool/provider sweep: 1 fixed (BUG-031, completes the BUG-027 family)
A high-signal sweep of the remaining tool handlers + all LLM providers confirmed them well-defended
(OpenAI empty-choices already fixed as BUG-018; Scheduling/ChaosPlan `from_dict`, sweep treatment
schema, observe table parsing, agent-SDK streaming, execute argv all guarded). One medium bug:

## BUG-031 — `compare_harness_runs` aborts the whole comparison on one corrupt report
- **Status:** FIXED
- **Severity:** medium (one truncated report fails the entire cross-harness comparison; sibling handles it)
- **Where:** `app/tools/multiharness.py` — `report = load_report(path)` was unguarded.
- **Trigger:** `compare_harness_runs(sources=[<run with a truncated report>, <other>])`. Since BUG-027
  hardened `load_report` to raise typed `ReportError`, the unguarded call now propagates it out of the
  handler → the loop's catch-all → `tool 'compare_harness_runs' raised: ...`, aborting the comparison.
- **Root cause:** BUG-027 routed the corrupt-report skip into the two *multi-report* tools `compare_reports`
  + `analyze_results` but missed the third multi-report tool, `compare_harness_runs` — its only try/except
  wrapped `compare_across_harnesses`, not the `load_report` loop. (The single-report callers —
  reproducibility/history/autotune — correctly surface the typed error; only the multi-report aggregators
  should skip-and-continue.)
- **Fix:** mirror `compare_reports` exactly — `try load_report / except ReportError → skipped.append(...);
  continue`. `ReportError` was already imported. One corrupt report is now skipped, the rest contrasted.
- **Regression test:** `tests/test_multiharness.py::test_compare_harness_runs_skips_unreadable_report`.

---

## Round 11 (2026-06-21) — async/concurrency-class hunt: 2 fixed (BUG-032 high, BUG-033 low)
A dedicated race-condition lens over the async backend (WS task mgmt, Channel futures/buffer,
RunRegistry, sweep checkpoint lock). The scary paths re-verified correct (no double-concurrent-turn:
the backstop `finally` is await-free; no double-resolve: a parked-gate Channel is never evicted so a
`restored` future can't coexist with the original; sweep checkpoint RMW all inside `ck_lock`). Two real
findings:

## BUG-032 — reconnect mid-turn double-renders buffered events
- **Status:** FIXED
- **Severity:** high (duplicate assistant bubbles / tool rows / report cards on reopening a running chat)
- **Where:** `app/main.py` (the `/ws` reconnect path: attach at `channel.ws = websocket`, then the
  `ready`/`history` sends, then `replay_live`) + `app/agent/channel.py::replay_live`.
- **Interleaving:** a turn task keeps running after the socket dropped (`channel.ws is None`). A new socket
  W2 reconnects: the handler sets `channel.ws = W2` (sync), then `await channel.emit("ready", …)` and (full
  path) `await channel.emit("history", …)` — a large transcript send that SUSPENDS. During that await the
  background turn resumes and `channel.emit(<turn event>)` fans the frame out LIVE to W2 *and* appends it to
  the buffer (new seq). The handler then calls `replay_live()`, which resends the WHOLE buffer — including
  the frame W2 just received live. The client (`ui/app.js handle()`) renders every frame and only de-dupes
  `approval_request` by id, so every other turn event double-renders. Both paths affected (full replays the
  whole buffer; incremental replays all `seq > after_seq`, and the gap frame has `seq > after_seq` too).
- **Fix:** capture `replay_cutoff = channel.cur_seq` at attach (BEFORE the ready/history awaits) and pass it
  as `through_seq` to `replay_live`, which now skips frames with `seq > through_seq` — exactly the frames
  emitted live after attach. Sends only what the socket genuinely missed; no double-render.
- **Regression test:** `tests/test_ws.py::test_replay_live_skips_frames_emitted_after_attach`.

## BUG-033 — env pre-probe task is fire-and-forget (untracked → GC-able)
- **Status:** FIXED
- **Severity:** low (best-effort probe; a dropped probe just means the first turn isn't pre-armed)
- **Where:** `app/main.py` — `asyncio.create_task(_prewarm_env(...))` with the result discarded. CPython
  holds only a weak ref to a bare task, so between the probe's subprocess awaits it can be GC'd and silently
  cancelled (the documented "save a reference to the task" hazard).
- **Fix:** store it in `app.state.background_tasks` + an `add_done_callback(...discard)` (the same pattern the
  disconnect/backstop paths already use). (Finding-3, concurrent `env_snapshot`/`commands` writes by the
  probe vs the first turn, is the accepted best-effort behavior — not changed.)

---

## Round 12 (2026-06-21) — final infra/deploy/CSS sweep: 1 fixed (BUG-034, high)
A sweep of the non-Python surfaces (Helm + Kustomize manifests, scripts, styles.css/index.html).
Everything else checked out (Helm vs Kustomize ports/probes/selectors agree, scripts are `set -euo
pipefail` + properly quoted, every `getElementById` has a matching element, all dialogs reachable).
One high-severity deploy-breaking defect:

## BUG-034 — deployed RBAC Role lacks `configmaps` → every in-cluster checkpointed sweep fails Forbidden
- **Status:** FIXED
- **Severity:** high (the Phase 22 sweep checkpoint/resume feature — ON BY DEFAULT, `orchestrate_sweep(
  checkpoint=True)` — is entirely non-functional in any real in-cluster deployment)
- **Where:** `app/packaging/assets.py::ORCHESTRATOR_RBAC_RULES` (the contract) + `deploy/helm/.../templates/
  rbac.yaml` + `deploy/kustomize/base/rbac.yaml` (both derived from it) + the test that locked it in.
- **Trigger:** deploy the agent in-cluster, run a checkpointed DOE sweep → `CheckpointStore.load()` does
  `kubectl get configmaps -l …` and `.write()` does `kubectl apply` of a ConfigMap (via the SAME
  RealKubeClient/allowlisted kubectl the Jobs use), under the agent's ServiceAccount → `Error from server
  (Forbidden): configmaps is forbidden`.
- **Root cause:** the RBAC contract docstring says it's "derived from the kubectl verbs RealKubeClient runs"
  — but it enumerated only the Job/Pod ops and was never updated when Phase 22 added the ConfigMap
  read/write. `tests/test_packaging.py` even ASSERTED `configmaps` absent (grouped with secrets/roles), so
  the stale Role was locked in. Worked only in tests (in-memory fakes never hit RBAC). A code-vs-manifest
  contradiction, not a deliberate exclusion (configmaps here are the agent's OWN managed-by checkpoints).
- **Fix:** add a least-privilege `configmaps: [get, list, watch, create, patch]` rule (NO delete — the
  agent never prunes checkpoints) to the contract + both manifests; update the test to require that rule and
  keep forbidding secrets/roles/rolebindings; refresh the "Jobs only" comments. All 17 packaging tests pass
  (incl. the helm/kustomize-match-the-contract checks).
- **Regression test:** updated `tests/test_packaging.py` RBAC-contract test (asserts the configmaps rule +
  no-delete + still-no-secrets/roles).

---

## Round 15 (2026-06-21) — capacity/channel/orchestrator wave 1: 3 fixed (BUG-035 high, BUG-036 med, BUG-037 low-med)
A targeted wave over the capacity pre-flight verdict, the WS reconnect/re-emit path, and the stateless
Job classifier. Three real defects fixed + merged to `main`; one latent label-length observation surfaced
(below, in the design/latent section). A wave-2 UI hunter independently re-read all of `ui/app.js` and found
no new defect (see the Verified NON-bugs note).

## BUG-035 — capacity pre-flight reports `feasible:true` for a GPU-UNDER-provisioned spec
- **Status:** FIXED
- **Severity:** high (a confident WRONG "it fits" at the plan gate when the pod has too few GPUs for the
  requested parallelism → user proceeds to a standup that can't even schedule/shard the model)
- **Where:** `app/capacity/planner.py::classify_diagnostics` (~line 145).
- **Trigger (reproduced):** a `check_capacity` run for a spec whose `TP×PP×DP` exceeds the pod's accelerator
  count, under the DEFAULT `enforce=False` / `ignoreFailedValidation` path. Upstream
  `capacity_validator.py` emits a `"<N> GPUs are required per replica"` line, but under
  `ignoreFailedValidation` that line is tagged only `WARNING:` (not `ERROR:` / `DEPLOYMENT WILL FAIL`),
  and the validator keeps SIZING as if the GPUs existed — emitting the `_SIZED_MARKERS` fit lines. So
  `classify_diagnostics` saw no fail + sized-and-fit → `feasible=True`, directly disagreeing with the
  `enforce=True` verdict for the same spec (which is `ERROR:` / infeasible).
- **Root cause:** the classify loop only flipped `will_fail` on `_FAIL_MARKER`; the GPU-shortfall line
  carries its won't-deploy verdict in a DIFFERENT string that the loop didn't recognize under the
  non-enforcing path, so a sized-but-unschedulable spec read as a clean fit.
- **Fix:** added `_GPU_SHORTFALL_MARKER = "gpus are required per replica"`; the classify loop now sets
  `will_fail` on `_FAIL_MARKER` OR `_GPU_SHORTFALL_MARKER`. Conservative — it can only flip a false
  `feasible:true` → `false`; the benign over-provisioned `"Some GPUs will be idle"` warning uses a
  different string and is left untouched. DISTINCT from BUG-030 (which was KV-cache sizing keyed on the
  wrong summary line); BUG-030's fix was re-verified and remains fully correct.
- **Regression test:** `tests/test_qafix_tools_capacity_history_config_report.py::test_classify_gpu_count_shortfall_is_infeasible_not_feasible`
  (fails before / passes after; also asserts the benign idle-GPU over-provisioned case stays feasible).

## BUG-036 — `reemit_pending` re-buffers each pending gate on every reconnect → evicts the turn's real progress
- **Status:** FIXED
- **Severity:** medium (repeated reconnects to a parked-gate chat defeat the Phase-15 catch-up buffer; a
  later full replay recovers nothing of the turn's progress)
- **Where:** `app/agent/channel.py::reemit_pending` (~lines 255-281), reached from `app/main.py` on every
  (re)connect that has a pending gate.
- **Trigger:** reconnect repeatedly to a chat parked on an undecided approval. `reemit_pending` re-surfaced
  each pending gate via `emit()`; because `APPROVAL_REQUEST` is NOT in `NON_TURN_EVENTS`, `emit()` bumped
  `_seq` and appended to the bounded live ring on EVERY reconnect. Each reconnect therefore appended a
  duplicate gate frame, evicting the turn's real progress events from the bounded deque — so a later full
  `replay_live()` recovered nothing, defeating the catch-up buffer.
- **Root cause:** the gate was already buffered + seq-stamped once by `request_approval` and persisted in
  `session.in_flight_approvals`, so re-surfacing it should be a pure re-SEND, not a fresh `emit()` (which
  re-seqs and re-buffers).
- **Fix:** `reemit_pending` now sends each pending gate DIRECTLY to the attached socket via `ws.send_json`
  (the same path `replay_live` uses) — seqless, not buffered, and a no-op when no socket is attached. The
  client still de-dupes by `request_id`, so the re-send is idempotent. Adjacent to BUG-032 (which capped
  `replay_live` at `through_seq`) but a different function.
- **Regression test:** `tests/test_ws.py::test_channel_reemit_pending_does_not_pollute_buffer_or_seq`
  (fails before: the re-emitted frame carried a seq; passes after: seqless).

## BUG-037 — `classify_job_status` crashes on a non-dict element in `conditions` (AttributeError)
- **Status:** FIXED
- **Severity:** low-medium (a forged/corrupt kubectl JSON aborts the stateless watch/reconstruct loops,
  violating the documented "classify never crashes" invariant — same class as BUG-029/BUG-023)
- **Where:** `app/orchestrator/job.py::classify_job_status` — the `_cond` helper (~line 389).
- **Trigger (reproduced):** `conditions = status.get("conditions", []) or []` only neutralizes a FALSY
  value; a forged/corrupt status with a SCALAR `conditions`, or a list containing a non-dict element (a
  bare string or `null`), made `_cond` call `c.get(...)` on a non-dict → `AttributeError`, which
  propagates out of `classify` and aborts the stateless `watch()` / `reconstruct()` loops.
- **Root cause:** the `(... or [])` fallback caught only a falsy `conditions`, never a truthy non-list or a
  list with malformed elements — the same gap BUG-029 fixed in the sibling `classify_failure` and BUG-023
  fixed for the counts in this very function, but this `conditions` iteration was missed.
- **Fix:** `conditions = [c for c in raw if isinstance(c, dict)] if isinstance(raw, list) else []`. A real
  terminal condition among malformed siblings still classifies; an all-malformed `conditions` degrades to
  PENDING, never raises.
- **Regression test:** `tests/test_orchestrator_controller.py::test_classify_survives_malformed_conditions`.

---

## Round 16 (2026-06-21) — subagent-orchestrated waves 2-4 + meta-review: 8 fixed (BUG-038..045) + known-open #1 RESOLVED
Three subagent waves over disjoint areas — session/orchestrator persistence, the tool-layer date/catalog
paths, the security allowlist read-only-trigger region logic, and the persisted-render path (share + WS
history) — plus an adversarial meta-review of the session's fixes that surfaced two more (BUG-044/045). Eight
real defects fixed + merged to `main`; the previously-logged **known-open #1** security item (global-region
`read_only_trigger` downgrade, Round 8) is RESOLVED by BUG-042 (region-aware detection). Clean areas
re-reviewed with no defect (see the Verified-clean note for round 16, below).

## BUG-038 — `session.persist()` writes `state.json` non-atomically (torn read / truncated transcript)
- **Status:** FIXED
- **Severity:** medium-high (a reader can observe a torn JSON file → a running chat reads as GONE / drops
  from the sidebar; a crash mid-write truncates the whole transcript permanently)
- **Where:** `app/agent/session.py::persist()` (~line 194).
- **Trigger:** `persist()` was the LONE store writing `state.json` directly via `write_text` (every sibling
  store uses temp-then-replace). It fires on nearly every turn event while `SessionManager.load()`/`list()`
  read concurrently → a reader could catch a partial write (`JSONDecodeError`), and a crash mid-write left
  the file truncated.
- **Root cause:** a non-atomic single `write_text` on a hot, concurrently-read path — the same class as
  BUG-003 (`share.py`), whose fix overlooked session persistence.
- **Fix:** serialize to a local, write `state.json.tmp`, then `tmp.replace(path)` (atomic rename) — matching
  the sibling stores. A reader now sees either the prior complete snapshot or the new complete one, never a
  torn file; a crash mid-write leaves the prior snapshot intact.
- **Regression test:** `tests/test_sessions.py::test_persist_is_atomic_crash_mid_write_preserves_prior_snapshot`.

## BUG-039 — a sweep checkpoint-write failure for ONE treatment sinks the ENTIRE sweep
- **Status:** FIXED
- **Severity:** medium-high (one failed checkpoint write aborts every other treatment's result, violating
  the documented per-treatment isolation; `checkpoint=True` is the default)
- **Where:** `app/orchestrator/controller.py::run_sweep` — `_persist_in_flight` / `_persist_completed`
  (~lines 399 / 428).
- **Trigger:** the checkpoint ConfigMap writes ran OUTSIDE `_one`'s per-treatment `try/except`. Those are
  mutating `kubectl apply`s that genuinely raise (a `QuotaError` mid-sweep, `ApprovalRejected`, a transient
  apply error); an uncaught error propagated through `asyncio.gather` and aborted the WHOLE sweep, destroying
  every other treatment's result.
- **Root cause:** the checkpoint writes were unguarded and sat outside the isolation boundary that protects
  each treatment, so a single write failure escaped per-treatment containment.
- **Fix:** `_safe_checkpoint_write` wraps `store.write` in `contextlib.suppress(Exception)` — like the
  existing `_safe_metric` / `_tail_logs` helpers. The cluster is the source of truth, so a missed checkpoint
  write only degrades to a re-run-on-resume, never a lost sweep.
- **Regression test:** `tests/test_orchestrator_checkpoint.py::test_checkpoint_write_failure_for_one_treatment_does_not_sink_the_sweep`.

## BUG-040 — `_filter_by_date` crashes the WHOLE tool-layer history list/trend on a corrupt `stored_at`
- **Status:** FIXED
- **Severity:** medium (a single corrupt record breaks `result_history` list/trend for EVERY record the
  moment any `start_date`/`end_date` is supplied)
- **Where:** `app/tools/history.py::_filter_by_date` (~line 73).
- **Trigger:** it compared `r.stored_at` directly (`>= lo`); a corrupt on-disk record (`stored_at` null or a
  string, bypassing the validated `add()` path) → a non-`ToolError` `TypeError` that escaped the handler and
  broke the list/trend for every record whenever a date filter was applied.
- **Root cause:** the tool-layer date path did a raw comparison with no coercion — the same class as BUG-020,
  but on a date path BUG-020 didn't cover.
- **Fix:** compare via `_as_num(r.stored_at)` (the BUG-020 helper) → a corrupt value coerces to `0.0`
  (oldest) instead of raising; real records still filter correctly.
- **Regression test:** `tests/test_history.py::test_tool_list_and_trend_date_filter_survive_corrupt_stored_at`.

## BUG-041 — `_workloads` catalogs only `*.yaml.in` templates → valid rendered `*.yaml` profiles refused
- **Status:** FIXED
- **Severity:** medium (the agent's allowlist refuses valid workloads → SessionPlan validation fails them as
  "not in the catalog for any harness")
- **Where:** `app/tools/catalog.py::_workloads` (~line 70).
- **Trigger:** it enumerated only `*.yaml.in` templates, so plain rendered `*.yaml` profiles never entered
  the catalog. Real dropped files: `inference-perf/guide_multimodal-serving_1.yaml`,
  `guide_predicted-latency-routing_1.yaml`. Upstream `-w` resolution accepts a plain `*.yaml` (it looks for
  `<name>` first, then falls back to `<name>.in`), so the agent rejected workloads the CLI would have run.
- **Root cause:** the glob covered only the template form, not the rendered form upstream also accepts.
- **Fix:** collect both forms into a set — `{name.removesuffix(".in") for *.yaml.in} | {name for *.yaml}`,
  sorted. `glob *.yaml` does NOT match `*.yaml.in`, so there is no double-count.
- **Regression test:** `tests/test_catalog.py::test_plain_yaml_profile_is_catalogued` (+ a dedup guard test).

## BUG-042 — a global-region `read_only_trigger` downgrades a mutating subcommand → bypasses the approval gate (latent)
- **Status:** FIXED (security hardening — LATENT today; see the nuance below)
- **Severity:** medium (security defense-in-depth — auto-run bypass of the approval gate; NOT a live-execution
  bypass under the current pinned upstream — see the nuance)
- **Where:** `app/security/allowlist.py` — the read-only-trigger detection in `_walk` / `_walk_subcommand`
  (~lines 379 / 398).
- **Trigger:** the pre-region (global / leading) tokens were matched against the MERGED leaf flag-dict
  (`{**global_flags, **sub.flags}`), so a subcommand-OWN `read_only_trigger` (`-n` / `--dry-run`, declared
  only on the mutating subcommands `plan`/`standup`/`smoketest`/`run`/`teardown`/`experiment`) was honored
  when it sat in the GLOBAL pre-region → command downgraded to `read_only` → `requires_approval=False` →
  auto-run, bypassing the approval gate.
- **Root cause:** trigger detection was region-blind — a trigger declared on a subcommand was matchable from
  the global region because the matcher saw the merged flag-dict rather than each region's own flags.
- **Fix:** make detection region-aware — thread `pre_tokens` → `pre_regions: list[(tokens, effective_flags)]`;
  a trigger is honored only against its OWN region's flag-dict. This preserves (a) subcommand-region triggers
  still downgrade, (b) the intentional nested-subcommand propagation (each level carries its `merged_flags`),
  and (c) the genuine global `--version` trigger.
- **IMPORTANT NUANCE (recorded honestly):** under the CURRENT pinned upstream the subparser's `--dry-run`
  uses `default=argparse.SUPPRESS`, so a global-position `-n` IS honored as a real dry-run today → this is
  NOT a live-execution bypass currently. It is a defense-in-depth hardening that becomes a live bypass only
  if upstream drops `SUPPRESS`. Rationale: a security gate must not depend on an unobservable upstream default
  in a read-only repo we don't control. This RESOLVES the previously-logged **known-open #1** item (Round 8).
- **Regression tests:** `tests/test_allowlist.py::test_global_position_dry_run_does_not_bypass_approval`
  (7 parametrized subcommands), `::test_subcommand_region_trigger_still_downgrades`,
  `::test_nested_pre_token_propagation_is_region_aware`,
  `::test_version_after_other_global_flag_is_read_only`.

## BUG-043 — `_history_items` crashes (HTTP 500 + un-reopenable chat) on a non-dict transcript element
- **Status:** FIXED
- **Severity:** medium (a non-dict element → HTTP 500 on the share route AND tears down the WS history emit
  on reconnect, leaving the chat UN-REOPENABLE)
- **Where:** `app/main.py::_history_items` (~line 392; reached by `create_share` ~:656 and the WS history
  emit ~:996).
- **Trigger:** it walked `messages`/`approvals`/`in_flight_approvals`/`commands`/`card_results` + each
  `tool_calls` + each `tool_results.results` with `x.get()` and NO per-element type check. `SessionManager.load`
  rebuilds these from JSON unchecked, so a non-dict element (a torn write, a hand edit, a forward-incompatible
  format) raised an uncaught `AttributeError` → HTTP 500 on `create_share`, and on the WS path the crash
  PRECEDES the receive loop, so the chat became un-reopenable.
- **Root cause:** the render path trusted every persisted element to be a dict — the same class as
  BUG-011/020-023, here on the persisted-render path.
- **Fix:** a `_dicts()` filter applied to every source list + an `isinstance` guard on a non-dict
  `tool_durations`; a malformed row is skipped and the render degrades gracefully.
- **Regression test:** `tests/test_share.py::test_share_survives_corrupt_session_transcript`.

## BUG-044 — `restore_pending` crashes the WS reconnect on a non-dict `in_flight_approvals` element (bricks reload of that chat)
- **Status:** FIXED
- **Severity:** medium-high (a SIBLING of BUG-043 on the very WS-reconnect path BUG-043 claimed to close —
  a single torn element permanently bricks reload of that one chat)
- **Where:** `app/agent/channel.py::restore_pending` (~line 242).
- **Trigger:** `restore_pending` did `entry.get("request_id")` for each element of `in_flight_approvals`.
  `in_flight` is loaded straight off disk (`Session.load` → `data.get("in_flight_approvals", [])`,
  `session.py:300`) with NO per-element type check, so a non-dict element (a torn string/scalar in a
  corrupt / hand-edited / forward-incompatible `state.json`) raised an `AttributeError` on the WS reconnect
  path (`main.py:845`) BEFORE the history emit AND the receive loop — permanently bricking reload of that
  one chat.
- **Root cause:** BUG-043 hardened `_history_items` / share but its regression test only exercised the share
  route (which never calls `restore_pending`), so this EARLIER reconnect path stayed unguarded — the same
  trust-every-persisted-element class as BUG-043, one call-site upstream of it.
- **Fix:** coerce a non-list `in_flight` to `[]` and skip non-dict elements (mirrors BUG-043's `_dicts`
  guard); a genuine gate among garbage is still restored. Found by an ADVERSARIAL META-REVIEW of this
  session's nine fixes.
- **Regression test:** `tests/test_ws.py::test_channel_restore_pending_survives_corrupt_in_flight`.

## BUG-045 — `aclose()` leaks the prewarmed Claude CLI subprocess on graceful shutdown
- **Status:** FIXED
- **Severity:** medium (one orphaned/connected CLI subprocess leaked per SIGTERM on graceful shutdown)
- **Where:** `app/llm/agent_sdk_provider.py::aclose()` (~line 339).
- **Trigger:** `aclose()` delegated to `_discard_prewarm`, which schedules the spare's disconnect as a bare
  untracked `asyncio.create_task` and returns immediately. `graceful_shutdown` (`main.py:170`) does
  `await provider.aclose()` and the event loop then tears down WITHOUT pumping again, so the deferred
  disconnect never ran — one orphaned/connected CLI subprocess per SIGTERM. The bare `create_task` was also
  a BUG-033-class GC hazard (weakly referenced, cancellable mid-disconnect).
- **Root cause:** `aclose()` deferred the disconnect to a fire-and-forget task on a path where nothing pumps
  the loop afterward; the pre-existing `test_aclose_disconnects_spare` passed only because it added a trailing
  `await asyncio.sleep(0.01)` the real shutdown path lacks.
- **Fix:** `aclose()` now awaits the spare's disconnect INLINE (new `_disconnect_spare` helper) and drains any
  in-flight background cleanups via `gather`; the hot-path `_discard_prewarm` keeps its non-blocking cleanup
  but tracks the task in a strong-ref `self._cleanup_tasks` set with a self-discarding done-callback (closes
  the GC hazard).
- **Regression test:** `tests/test_agent_sdk_provider.py::test_aclose_awaits_disconnect_before_returning`.

### Verified clean (no bug) — round 16
- **`app/llm/` layer full review** — usage accounting, SDK message threading, and context-compaction pairing
  were reviewed end-to-end: NO bug.
- **FastAPI REST surface re-fuzzed** — history/jobs/share with empty/traversal/over-long/NUL inputs returned
  clean 4xx/200 with no 500s; the one real defect on the persisted-render path is BUG-043.
- **`deploy/` swept clean** — Dockerfile / helm / kustomize / scripts / CSS reviewed with NO defect (helm lint
  + `kubectl --dry-run` + RBAC empirically tested on a throwaway namespace).
- **Adversarial meta-review** of the session's fixes confirmed the other 8 fixes (BUG-035..043) sound; it
  surfaced BUG-044 (the unguarded `restore_pending` sibling of BUG-043).

### Noted, not filed (accepted design tradeoff) — round 16
- **`SessionManager._sessions` is never evicted from memory** — slow unbounded growth over process lifetime.
  Reads as an accepted design tradeoff (sessions are bounded in practice and pruned on disk), not a discrete
  defect; recorded here so a future hunt doesn't re-discover it as new.

## Round 17 (2026-06-21) — subagent wave 5 (WS race / DoE / dispatch): 3 fixed (BUG-046 low-med, BUG-047/048 med-high)
A fifth subagent wave over three disjoint areas — the DoE cross-product treatment-dedup, the tool-dispatch
validation-error serialization path (loop-poisoning), and the WS mid-turn-reconnect missed-tail window vs
the bounded live-buffer ring eviction. Three real defects fixed + merged to `main`. BUG-048 is the INVERSE
of BUG-032 (and distinct from BUG-036) on the same reconnect path; BUG-047 escapes the per-tool `_invoke`
guard from the loop's result-clamp step.

## BUG-046 — DoE level-dedup collapses distinct levels that are equal-but-different-type in Python (`1`/`1.0`/`True`)
- **Status:** FIXED
- **Severity:** low-medium (a factor with equal-valued cross-type levels silently sweeps a SMALLER matrix
  than the agent requested — a content-dedup drop, no error, treatment NAMES stay distinct)
- **Where:** `app/validation/doe.py::_hashable` (~line 166), consumed by the dedup signature in `_expand`
  (~line 155).
- **Trigger:** the cross-product treatment-payload dedup built a signature `tuple(sorted((k, _hashable(v))))`
  and skipped a combo whose signature was already seen; `_hashable` returned the raw value, but in Python
  `1 == 1.0 == True` with EQUAL hashes, so signatures `("a.b",1)` / `("a.b",1.0)` / `("a.b",True)` collide.
  A factor with `levels=[1, True]` or `[1, 1.0]` silently collapsed two genuinely-distinct levels into ONE
  treatment.
- **Root cause:** the dedup key paired each value by Python value/hash identity, which conflates `int`/`float`/
  `bool` instances that compare and hash equal — so distinct levels signed to the same tuple.
- **Fix:** pair each value with its concrete type name — return `(type(base).__name__, base)` — so
  `("int",1)` / `("float",1.0)` / `("bool",True)` are distinct; a true repeat (same type AND value, e.g.
  `[10, 10]`) still dedupes. Safe direction (only ever MORE treatments, never re-introduces a real dup).
- **Regression test:** `tests/test_doe.py::test_distinct_levels_equal_in_python_are_not_deduped`.

## BUG-047 — un-serializable Pydantic `ctx` in a validation error poisons the loop with an ORPHANED tool_call
- **Status:** FIXED
- **Severity:** medium-high (a custom-validator `ValueError`/`AssertionError` on a tool's input model →
  `TypeError` in the loop's result-clamp, OUTSIDE the per-tool guard → an orphaned `tool_call` in
  `session.messages` that poisons the NEXT turn with a provider error)
- **Where:** `app/tools/registry.py::dispatch` (~line 710), surfacing as a `TypeError` at
  `app/agent/loop.py:287` (`clamp_tool_result_content` → `json.dumps`), OUTSIDE the per-tool `_invoke` guard.
- **Trigger:** `dispatch` returned `{"error":"invalid arguments","details": exc.errors(include_url=False)}`.
  When a tool's Pydantic input model has a custom field/model validator that raises `ValueError`/
  `AssertionError`, Pydantic embeds the raised EXCEPTION OBJECT in each error entry's `ctx` — not
  JSON-serializable. The loop clamps every tool result with `json.dumps`, which is outside `_invoke`'s
  `try/except` (that only wraps `dispatch`), so the `TypeError` escaped `run_turn` AFTER the assistant message
  with its `tool_calls` was appended (`loop.py:217`) but BEFORE the matching `tool_results` block
  (`loop.py:301`) → an ORPHANED `tool_call` in `session.messages` that poisons the next turn (provider error).
  Concrete repro: `propose_session_plan` with an autotune knob where `max <= min`
  (`AutotuneKnob._check`, `session_plan.py:62`, raises `ValueError`). The existing invalid-args tests only hit
  the missing-field path (no `ctx`, serializes fine) — why it slipped.
- **Root cause:** `dispatch`'s docstring promises validation errors are returned-not-raised for
  self-correction, but the returned value carried Pydantic's non-serializable `ctx` (the raised exception
  object), which then detonated at the un-guarded `json.dumps` clamp step.
- **Fix:** `exc.errors(include_url=False, include_context=False)` drops the non-serializable `ctx` (the
  human-readable `msg` still carries the validator message for self-correction) + a defensive
  `json.loads(json.dumps(details, default=str))` roundtrip + a `msg`-only last-resort fallback.
- **Regression test:** `tests/test_loop.py::test_schema_validation_error_does_not_break_the_loop`.

## BUG-048 — mid-turn reconnect drops the missed tail evicted from the bounded live-buffer ring during attach (inverse of BUG-032)
- **Status:** FIXED
- **Severity:** medium-high (a permanent content gap in the reconnecting client's cached pane — a missed
  `tool_result` / report card / streaming chunk / even `done` — with no de-dup, no re-fetch, no
  full-rebuild fallback)
- **Where:** `app/main.py` ws handler (~line 853 capture + ~1047 replay calls) ↔
  `app/agent/channel.py::replay_live` (~line 146).
- **Trigger:** the INVERSE of BUG-032 (and distinct from BUG-036). On mid-turn reconnect the handler captures
  `replay_cutoff = channel.cur_seq` synchronously (BUG-032), then awaits `emit("ready")` (+ `emit("history")`
  on the full path) BEFORE calling `replay_live(after_seq, through_seq=replay_cutoff)`. The per-turn live
  buffer is a bounded `deque(maxlen=LIVE_BUFFER_MAX)`. A still-running chatty background turn emitting a burst
  DURING those awaits appends to the ring (delivered live to the just-attached socket, `seq > replay_cutoff`)
  and EVICTS the oldest entries — which can include the FRONT of the missed window
  `(after_seq, replay_cutoff]`. By the time `replay_live` reads `self._buffer` those frames are gone: never
  delivered live (they predate this socket's attach) and now unreplayable → a permanent content gap.
- **Root cause:** `replay_live` read the live ring LAZILY (after the eviction-prone awaits), so a burst during
  attach could evict the front of the missed window before it was replayed — the snapshot of what to replay
  was taken too late.
- **Fix:** snapshot `channel.buffered_events` synchronously at attach (alongside `replay_cutoff`, before the
  eviction-prone awaits) and pass it to `replay_live(..., frames=replay_snapshot)` on BOTH the incremental
  and full mid-turn paths; `replay_live` gained an optional `frames` param (falls back to the live buffer when
  omitted), preserving the `through_seq` cap + `after_seq` cursor.
- **Regression test:** `tests/test_ws.py::test_reconnect_midturn_does_not_drop_missed_tail_evicted_during_attach`.

## Round 18 (2026-06-21) — subagent wave 6 (provenance / units / cancel): 3 fixed (BUG-049 med, BUG-050 high, BUG-051 med-high)
A sixth subagent wave over three disjoint areas — the content-addressed provenance bundle-id basis, the A/B +
sweep summary-comparison unit normalization, and the WS per-turn cancel-vs-queued-steer backstop. Three real
defects fixed + merged to `main`. BUG-050 is on a SEPARATE code path from `evaluate_slo` (which already
canonicalizes units); BUG-051 is distinct from the cancel handler's intentional inline await.

## BUG-049 — content-addressed bundle id collides for two different runs at the same report path (optional `run_uid`)
- **Status:** FIXED
- **Severity:** medium (two genuinely-different validated runs collapse onto ONE provenance node — a
  silently overwritten bundle, a broken/lost lineage; no error)
- **Where:** `app/storage/provenance.py::_compute_bundle_id` (~line 180), call site (~line 234), overwrite at
  `BundleStore.write`/`list` (~line 294 / ~line 320).
- **Trigger:** the content-addressed `bundle_id` was keyed only on `{run_uid, report_path, repo_shas}`, but
  `run_uid` (= `report_summary.get("run_uid")` = `run.get("uid")`) is OPTIONAL / not schema-required. With
  `run_uid=None`, two genuinely-different validated runs written to the SAME `report_path` (a re-benchmark, or
  an autotune trial overwriting the same run dir) with the same repo SHAs produced an IDENTICAL `bundle_id`
  despite differing content/digest, so `BundleStore.write` silently OVERWROTE the first bundle — two distinct
  runs collapsing onto one provenance node. The module docstring explicitly promises "a genuinely different
  run does not collide." The sibling `history.compute_record_id` folds in metric values so it lacks this hole.
- **Root cause:** the id basis omitted the run's content fingerprint, relying on an optional `run_uid` to
  distinguish runs; when absent, path+SHAs alone are not unique across distinct runs at one path.
- **Fix:** mix the already-computed `report_digest` (sha256 of validated report bytes + summary) into the id
  basis — same run → same digest (idempotent), different run → different digest (collision broken); no new I/O.
- **Regression test:** `tests/test_provenance.py::test_build_bundle_id_no_collision_for_different_runs_at_same_path`.

## BUG-050 — A/B + sweep `compare_summaries` does NO unit conversion → crowns the slower config as the winner
- **Status:** FIXED
- **Severity:** high (the headline A/B + sweep comparison can declare the WRONG winner — the user is told the
  slower config is faster — with a nonsense delta percentage)
- **Where:** `app/validation/report.py::compare_summaries` (per-metric value loop ~line 570) +
  `_build_metric_row` (best chooser ~line 509, `delta_pct` ~line 504).
- **Trigger:** the BR v0.2 `Units` enum permits the SAME latency/throughput metric in either unit (e.g.
  latency in `s` OR `ms`; both schema-valid). `evaluate_slo` converts to canonical ms via `_TO_MS`, but
  `compare_summaries` (a SEPARATE code path — the headline A/B + sweep comparison) did NO conversion: it fed
  each run's raw number straight into the min/max winner chooser and the `(v-base)/base` delta. So a run
  reporting TTFT `0.5` (seconds = 500 ms) compared against one reporting `200` (ms) yields `0.5 < 200` → the
  500 ms run is crowned the WINNER (best) over the 200 ms run, with a `+39900%` nonsense delta (truth: the
  200 ms run is faster, correct delta `-60%`).
- **Root cause:** `compare_summaries` compared raw metric numbers without normalizing the per-run units onto a
  canonical unit, conflating values reported in different (both schema-valid) units.
- **Fix:** add a canonical unit per metric to `_COMPARE_METRICS` (latency→ms, token throughput→tokens/s,
  request rate→queries/s) + a `_CANONICAL_CONVERSIONS` table + a `_to_canonical()` helper; normalize every
  run's value onto the metric's canonical unit BEFORE the winner/delta math. The row's `units` is set to
  canonical only when at least one value was actually normalized (else `None` — a non-conforming report
  degrades to raw numbers rather than a falsely-asserted unit). Also fixed the now-4-tuple unpack at the
  cross-harness name lookup.
- **Regression test:** `tests/test_sweep.py::test_compare_summaries_normalizes_mixed_latency_units_for_winner`.

## BUG-051 — STOP is resurrected by a queued steer: the cancel backstop spawns a FRESH turn after a hard-abort
- **Status:** FIXED
- **Severity:** medium-high (the agent keeps working — another full LLM turn + tool dispatch, concurrency
  slot, tokens/quota, possibly a new cluster op — right after the user clicked STOP; the opposite of the
  requested hard-abort)
- **Where:** `app/main.py` `/ws` per-turn `run_turn` `finally` steer-backstop (~line 949), interleaving the
  `except asyncio.CancelledError` handler (~line 919) and the `CancelIn` control-frame handler (~line 1151) →
  `app.state.runs.cancel(session.id)`.
- **Trigger:** when a steer (`session.ctx.steer_messages`) is queued while the loop is busy in a step, it
  isn't drained until the next step boundary (`loop.py:321`). A cancel control frame raises `CancelledError`
  into the running turn mid-step, BEFORE that drain. `run_turn`'s `finally` runs for every exit (incl.
  cancellation), saw leftover steers non-empty + `channel.ws` not `None` (the cancel arrived over the
  still-attached socket), and spawned `asyncio.create_task(run_turn(followup))` — a FRESH turn running the
  queued steer. So the agent keeps working right after the user clicked STOP.
- **Root cause:** the `finally` steer-backstop did not distinguish a cancellation from a normal turn exit, so
  leftover queued steers survived the abort and were resurrected as a follow-up turn.
- **Fix:** a `was_cancelled` flag set in the `CancelledError` handler; in `finally`, when cancelled, drop the
  leftover steers (`steer_messages=[]`) and SKIP the backstop entirely (forgetting the channel if nothing
  attached/pending) instead of spawning a follow-up. Distinct from the "Considered but NOT changed" note about
  the cancel handler's intentional inline `await`.
- **Regression test:** `tests/test_ws.py::test_ws_cancel_with_queued_steer_does_not_resurrect_the_turn`.

## Round 19 (2026-06-21) — subagent wave 7 (meta-review sibling / report precedence / tracing flags): 3 fixed (BUG-052 high, BUG-053 med, BUG-054 med)
A seventh subagent wave over three disjoint areas — a 2nd adversarial meta-review of the newer fixes (which
surfaced an unguarded sibling of BUG-044), the report-locator's search-root precedence vs a global mtime pick,
and the unrecognized-flag advisory's handling of the soft-optional `tracing.*` knob family. Three real defects
fixed + merged to `main`. The meta-review otherwise verified the six newer fixes 042/045/048/049/050/051 sound.

## BUG-052 — corrupt non-dict in-flight-approval element crashes both approval mutators (unguarded sibling of BUG-044)
- **Status:** FIXED
- **Severity:** high (one user click after a BUG-044-survived corrupt element re-bricks the very chat BUG-044
  kept usable — an `AttributeError` tears down the whole `/ws` handler, which only catches `WebSocketDisconnect`)
- **Where:** `app/agent/session.py::record_in_flight_approval` (~line 177) + `clear_in_flight_approval`
  (~line 183); unwrapped `channel.resolve` call sites in `app/main.py` `/ws` (~line 1134 / 1140 / 1163).
- **Trigger:** sibling of BUG-044. Both mutators did `a.get("request_id")` over the raw `in_flight_approvals`
  list, which `SessionManager.load` rebuilds from disk with NO per-element type check. BUG-044 guarded only
  `Channel.restore_pending` (so the reconnect HANDSHAKE survives a corrupt non-dict element), but the corrupt
  element REMAINS in `session.in_flight_approvals` and crashes these mutators one user-click later: the user
  clicks Approve → `/ws` receive loop calls `channel.resolve(rid)` → `p["restored"]` true →
  `session.clear_in_flight_approval(rid)` → `"TORN".get(...)` → `AttributeError`, and the `channel.resolve`
  call sites in `/ws` are UNWRAPPED (only `WebSocketDisconnect` is caught), so it tears the whole handler down —
  re-bricking the very chat BUG-044 kept usable. `record_in_flight_approval` independently crashes a resumed
  turn that surfaces a NEW gate.
- **Root cause:** the corrupt-element guard was applied at only ONE of the three consumers of the raw list; the
  two approval mutators still iterated it assuming every element is a dict.
- **Fix:** skip non-dict elements in both scans (the BUG-043 `_dicts` class); `clear_in_flight_approval` also
  self-heals garbage out of the list while preserving a real gate. Found by a 2nd adversarial meta-review (the
  other six newer fixes 042/045/048/049/050/051 verified sound).
- **Regression test:** `tests/test_sessions.py::test_in_flight_approval_mutators_survive_corrupt_non_dict_element`.

## BUG-053 — report locator overrides an explicit `results_dir` with a NEWER report elsewhere in the workspace
- **Status:** FIXED
- **Severity:** medium (an explicit `results_dir` (run A) is silently overridden by any newer report in the
  broader workspace (run B) — the caller gets run B's metrics under the dir it explicitly pointed at; silent
  wrong-data, no error)
- **Where:** `app/tools/report_locate.py::_find_report` (~line 187), reached from `locate_and_parse_report`
  (~line 30).
- **Trigger:** `locate_and_parse_report` builds `search_roots` most-specific-first (`[explicit results_dir,
  explicit session_id dir, broad ctx.workspace]`). `_find_report` flattened ALL roots into one candidate pool
  and returned `max(candidates, key=mtime)` — so an explicit `results_dir` (run A) was silently overridden by
  any NEWER `benchmark_report_v0.2*` elsewhere in `ctx.workspace` (a later run B in the same session),
  returning run B's metrics under the dir the caller explicitly pointed at.
- **Root cause:** the locator collapsed the precedence-ordered roots into a single global newest-by-mtime pick,
  discarding the most-specific-first ordering the caller had encoded into `search_roots`.
- **Fix:** iterate roots in precedence order; return the newest-by-mtime from the FIRST root that contains any
  report; broader roots are consulted only as a fallback (preserves "newest within a run dir"). (Two lower-value
  items in this path were surfaced-not-filed: `LocateReportInput.session_id` `..`-traversal — being fixed
  separately — and non-deterministic mtime ties.)
- **Regression test:** `tests/test_report_validation.py::test_find_report_honours_root_precedence_over_global_mtime`.

## BUG-054 — unrecognized-flag advisory falsely flags every valid `tracing.*` sub-leaf as a bogus knob
- **Status:** FIXED
- **Severity:** medium (the tool tells users their valid, documented Phase-54 OpenTelemetry `tracing.*` config
  is a bogus/typo flag — eroding trust in the advisory and nudging users to delete working config)
- **Where:** `app/tools/config_artifact.py::unrecognized_flags` (~line 176), called from `author_scenario`
  (~line 325 / 374).
- **Trigger:** the advisory warns the agent when a scenario override key looks fabricated/unrecognized.
  `_SOFT_OPTIONAL_KNOBS={"tracing"}` widened only the TOP-LEVEL name `tracing` into the validator's known keys,
  but `unrecognized_flags` keys on each dotted key's LEAF segment. The `tracing.*` family (Phase-54
  OpenTelemetry config) is rendered by upstream modelservice jinja behind `{% if is defined %}` guards and
  appears in NO scenario example or stock `defaults.yaml` BY DESIGN, so its sub-leaves (`otlpEndpoint`,
  `samplerArg`, `vllmDecode`, `vllmPrefill`, `routingProxy`, `sampler`, `collectDetailedTraces`) were never in
  `known_leaf_keys` and got flagged — making the tool tell users their valid, documented tracing config is a
  bogus/typo flag. Phase 54 fixed the validator but missed this parallel advisory.
- **Root cause:** the soft-optional widening was matched against the top-level name while the advisory keyed on
  leaf segments, so the family's sub-leaves fell outside the known-key set.
- **Fix:** skip any dotted key whose ROOT segment is in `_SOFT_OPTIONAL_KNOBS` (`dotted.split(".",1)[0]`);
  genuinely fabricated flags outside the family still surface.
- **Regression test:** `tests/test_tracing_config.py::test_tracing_subkeys_are_not_falsely_flagged_as_unrecognized`.

## Round 20 (2026-06-21) — subagent wave 8 (traversal / readiness / stale-catalog): 3 fixed (BUG-055 high, BUG-056 low-med, BUG-057 med)
An eighth subagent wave over three disjoint areas — the report-locator's `session_id` containment (an item
surfaced-not-filed in Round 19's BUG-053), the serving-readiness restart-count coercion, and the repo-clone
catalog-refresh fall-through. Three real defects fixed + merged to `main`.

## BUG-055 — unvalidated `session_id` path traversal → agent-controllable arbitrary-file read of any benchmark report
- **Status:** FIXED
- **Severity:** high (security — an agent-supplied `session_id` of `../…` or an absolute path escaped the
  per-session sessions root, yielding arbitrary-file read of any `benchmark_report_v0.2*.yaml/json` on disk)
- **Where:** `app/tools/report_locate.py::locate_and_parse_report` (~line 33) + the
  `LocateReportInput.session_id` schema (`app/tools/schemas.py` ~line 141, unvalidated).
- **Trigger:** `session_id` is an UNVALIDATED agent-supplied string joined onto `ctx.workspace.parent` (the
  per-session sessions root) to form a search root; `_find_report` then globs `**/benchmark_report_v0.2*`
  under it and reads the newest match. A `session_id` of `../…` (or an absolute path) escaped the sessions
  root → agent-controllable arbitrary-file read of any `benchmark_report_v0.2*.yaml/json` anywhere on disk.
  Same class as BUG-028 (`probe.py` spec-path containment).
- **Root cause:** the composed search root was never checked for containment within the sessions root before
  being globbed — an agent-supplied id could traverse out of (or escape entirely) the per-session tree.
- **Fix:** a new `_session_root(ctx, session_id)` helper resolves the composed path and requires
  `candidate == sessions_root or candidate.is_relative_to(sessions_root)`, raising `ToolError` on escape
  (the loop relays `ToolError` as a clean `{"error": …}`, never a raw exception); legitimate ids unchanged.
- **Regression test:** `tests/test_report_validation.py::test_locate_rejects_session_id_path_traversal` (+
  `test_locate_accepts_legitimate_session_id`).

## BUG-056 — non-numeric `restartCount` in forged pods JSON raises out of the read-only readiness gate
- **Status:** FIXED
- **Severity:** low-medium (a forged/corrupt `kubectl get pods -o json` makes the read-only readiness gate
  raise an opaque error instead of degrading — violating the module's "garbage pods_json degrades, never
  raises" invariant)
- **Where:** `app/readiness/diagnostics.py::classify_serving_readiness` (~line 467).
- **Trigger:** the per-container restart tally did `int(cs.get("restartCount") or 0)` — the LONE unguarded
  numeric coercion in that scan (`containerStatuses` are `isinstance(cs, dict)`-filtered and the port loop is
  `isinstance(cp, int)`-guarded). A forged/corrupt `kubectl get pods -o json` with a non-numeric
  `restartCount` (e.g. `"lots"`) raised `ValueError`, which escapes `_serving_readiness` /
  `check_endpoint_readiness` (no `try/except` around the `classify_*` call) as an opaque error out of the
  read-only readiness gate. Same hardening class as BUG-023/029/037.
- **Root cause:** a single unguarded `int(...)` coercion in an otherwise type-guarded scan, with no
  exception handling around the classify call upstream — one bad field broke the whole gate.
- **Fix:** a local `_as_int()` helper (non-numeric → 0, clamped non-negative, `except (TypeError,
  ValueError)`) mirroring the orchestrator's BUG-023; a bad count reads as `0`, and a real count among
  malformed siblings is still read.
- **Regression test:** `tests/test_serving_readiness.py::test_classify_survives_non_numeric_restart_count`
  (+ `test_classify_real_count_still_read_among_malformed_siblings`). (`kube.py` itself audited clean: argv
  assembly under `shell=False`, mutating-vs-readonly routing, label-scoped cleanup delete.)

## BUG-057 — a rejected/denied later clone skips the catalog refresh, bricking every spec/ref check for the turn
- **Status:** FIXED
- **Severity:** medium (an early repo cloned OK but a later clone rejected at the gate skips the catalog
  refresh, leaving the per-context catalog cache stale → `validate_plan` and the allowlist check reject every
  valid spec/harness/workload/ref for the rest of the turn even though the repo is on disk)
- **Where:** `app/tools/repos.py::ensure_repos` (~line 58).
- **Trigger:** `ctx.catalog(refresh=True)` was the function's last, fall-through-only line, but
  `ctx.run_command` raises mid-loop on `ApprovalRejected` (user rejects a clone at the gate), `ToolError`
  (allowlist denial), or `QuotaError` — propagating straight out and SKIPPING the refresh. An EARLIER repo in
  the same call (`llm-d-benchmark` is first in `_KNOWN_REPOS`) may already have cloned successfully, so the
  per-context catalog cache (`ToolContext._catalog`) is left at its stale pre-clone snapshot (`present=False`,
  empty lists). `plan.validate_plan` and the `catalog_for_allowlist` check (read inside EVERY
  `run_command`/`run_readonly` allowlist validation) read `ctx.catalog()` WITHOUT refresh → they reject every
  valid spec/harness/workload/ref for the rest of the turn even though the repo is on disk. Exactly the
  `app/tools/CLAUDE.md:28` stale-catalog hazard, realized via an early-exit path.
- **Root cause:** the refresh sat on the function's fall-through line, so any mid-loop raise (rejection,
  denial, quota) bypassed it, leaving an already-cloned repo invisible to the catalog cache.
- **Fix:** wrap the clone loop in `try/finally` and refresh the catalog in `finally` so it always runs before
  return/raise.
- **Regression test:**
  `tests/test_repos.py::test_catalog_refreshed_even_when_a_later_clone_is_rejected` (+
  `test_successful_clone_refreshes_catalog`).

### Verified clean (no bug) — round 20
- **`app/observability`** (`instrument.py` / `metrics.py` / `logctx.py` / `logging.py` / `cot_trace.py`):
  metric names match the deploy alerts/scrape/dashboard, label cardinality is bounded, no double-count,
  emission is exception-safe, `corr_id` contextvars are asyncio-isolated, and no secret leak.
- **`app/orchestrator/kube.py`**: argv assembly / mutating-vs-readonly routing / label-scoped cleanup all
  sound.

---

## Round 21 (2026-06-21) — subagent wave 9 (storage sort-key) + quota wall: 1 fixed (BUG-058 med)
Wave 9 launched 3 hunters (knowledge/prompt, session-plan, storage/retention). The shared Claude Max-plan
quota was EXHAUSTED mid-wave ("You've hit your session limit · resets 11am Asia/Jerusalem"), killing all
three. The storage/retention hunter had written the `_as_num` helper + regression test but hit the wall
before wiring it into the sort site; the orchestrator (main loop, git/edit ops only) completed the wiring
and merged it. The knowledge/prompt + session-plan hunters left no committed work (re-run when quota resets).

## BUG-058 — corrupt non-numeric `updated_at` 500s the whole session sidebar list
- **Status:** FIXED
- **Severity:** medium (a single corrupt/hand-edited/forward-incompatible `state.json` crashes
  `GET /api/sessions` for EVERY chat, not just the corrupt one — the whole sidebar list disappears)
- **Where:** `app/agent/session.py::SessionManager.list` sort key (~line 383).
- **Trigger:** `out.sort(key=lambda s: s.get("updated_at") or 0, reverse=True)`. The `or 0` rescues only a
  FALSY value (None/0); a TRUTHY non-number — a corrupt/hand-edited `state.json` carrying a STRING timestamp
  (e.g. `"2026-06-21T10:00:00"`), read straight off disk with no per-field type check — sails through and
  makes `sorted(...)` compare `str` against a healthy record's `float`:
  `TypeError: '<' not supported between instances of 'float' and 'str'`. That raise is on the NON-best-effort
  sort (the per-record `try/except` only guards the JSON parse), so it 500s the whole list.
- **Root cause:** a falsy-only guard on a disk-loaded numeric used as a sort key — same class as
  BUG-020/021/022/040; this is the `SessionManager.list` sibling flagged-but-deferred back in wave 2.
- **Fix:** a crash-proof `_as_num()` helper (`v if isinstance(v,(int,float)) and not isinstance(v,bool) else
  0.0`) used as the sort key, so a corrupt value coerces to 0.0 (sorted oldest, still visible) instead of
  crashing; `bool` is excluded (an `int` subclass, never a valid timestamp).
- **Regression test:** `tests/test_sessions.py::test_list_survives_corrupt_non_numeric_updated_at`.

### Note — session paused (quota, not bug-supply)
The hunt was paused here by Max-plan quota exhaustion (resets 11am Asia/Jerusalem), NOT by running out of
bugs. To resume: re-launch the knowledge/prompt, session-plan, and storage/retention hunters (no committed
work was lost), then continue with fresh lenses (command/shell tool, auth/rate-limiter, a systematic
grep-sweep for the recurring unguarded-coercion-on-disk-JSON vein that produced BUG-023/029/037/040/043/044/052/056/058).

---

## Round 22 (2026-06-21) — subagent wave 10 (subprocess stdout robustness; resumed after quota reset): 1 fixed (BUG-059 med-high)
This round RESUMED the hunt after the shared Claude Max-plan quota (which had paused wave 9 — see Round 21)
reset; a single probe hunter confirmed quota restored and found BUG-059.

## BUG-059 — un-terminated >64 KiB subprocess line crashes the runner AND orphans the child process group
- **Status:** FIXED
- **Severity:** medium-high (realistic, non-adversarial output crashes command execution AND leaks a live
  child process group while the server keeps running)
- **Where:** `app/security/runner.py::CommandRunner.execute`, the `_pump` coroutine (~line 224).
- **Trigger:** stdout was drained via `async for raw in proc.stdout` (line-buffered
  `StreamReader.readline()`), which raises `ValueError: Separator is not found, and chunk exceed the limit`
  the instant a single un-terminated line passes asyncio's default 64 KiB StreamReader cap. The trigger is
  realistic output, not adversarial: a large one-line `kubectl get … -o json`, a base64 blob, or a minified
  log line — anything ≥65,536 bytes on one line with no `\n`.
- **Root cause:** that `ValueError` is in NEITHER the `except TimeoutError` nor the `except
  asyncio.CancelledError` handler, so it escaped `execute()` raw (`command_exec.py`'s `run_command`/
  `run_readonly` pass it straight through; only the broad tool-dispatch guard turns it into an opaque "tool
  raised"). Worse, the exception path never ran `_kill_process_group`, so a still-alive child + its process
  group is ORPHANED while the server keeps running.
- **Fix:** drain stdout in fixed-size chunks (`proc.stdout.read(_READ_CHUNK=65536)`) with a `codecs`
  incremental UTF-8 decoder (keeps a multibyte char whole across a chunk boundary; `errors="replace"`
  tolerates non-UTF-8); flush each complete (newline-terminated) line to `on_line` + the bounded capture
  tail, and flush a never-terminated line in bounded `_MAX_LINE_CHARS` segments so a runaway child can't grow
  an unbounded buffer. Normal output is unchanged (line-splitting, multibyte-across-boundary, and a trailing
  newline-less line all verified). Also hardens the `run_shell` UNRESTRICTED_TOOLS POC (it delegates to
  `execute`) without weakening its intentional no-allowlist behavior.
- **Regression test:** `tests/test_qafix_infra_runner.py::test_huge_single_line_does_not_crash_the_runner`
  (+ `::test_huge_line_then_alive_child_is_reaped_not_leaked`).

---

## Round 23 (2026-06-21) — subagent wave 10 (knowledge limit / plan harness-pairing / classify non-dict): 3 fixed (BUG-060 med, BUG-061 med-high, BUG-062 low-med)

## BUG-060 — `limit=1` knowledge search reserves the only slot for a repo pointer, evicting the top-ranked guide
- **Status:** FIXED
- **Severity:** medium (a `limit=1` knowledge query that matches both a strong own-guide and a weaker
  upstream repo pointer returns ONLY the repo pointer — the primary help is silently dropped)
- **Where:** `app/tools/knowledge_access.py::search_knowledge` (~line 396, the `repo_quota` computation).
- **Trigger:** `repo_quota = min(len(repo_hits), max(1, limit // 3)) if repo_hits else 0` reserves a slice of
  the result page for curated upstream repo-doc pointers. At `limit=1`, `max(1, 1//3) == 1`, so
  `k_take = limit - repo_quota = 0` — zero knowledge slots — and a query matching both a strong knowledge
  guide and a lower-scoring repo pointer returned ONLY the repo pointer, dropping the top guide entirely.
- **Root cause:** the final score-sort can't recover the guide — it was excluded from `chosen` before the
  sort ran. This contradicts the function's documented invariant ("the agent's OWN guides are the primary
  help, so they lead").
- **Fix:** after computing `repo_quota`, `if knowledge_hits: repo_quota = min(repo_quota, limit - 1)` so the
  reserved repo slice can never evict the top-ranked knowledge guide; `limit >= 2` is unchanged, and backfill
  still fills unused slots.
- **Regression test:** `tests/test_new_tools.py::test_search_knowledge_limit_one_keeps_top_hit`.

## BUG-061 — plan validation checks `workload` against the FLAT UNION of harnesses, never the chosen harness
- **Status:** FIXED
- **Severity:** medium-high (approval-integrity — a plan with a cross-harness `(harness, workload)` pairing
  validates clean, then the executed run differs from the APPROVED plan and fails downstream)
- **Where:** `app/validation/session_plan.py::validate_plan` (~line 153).
- **Trigger:** it validated `plan.workload` against `set(catalog.get("workloads", []))` — the FLAT UNION
  across all harnesses — never against the chosen harness. The catalog also carries
  `workloads_by_harness` (a real partition: `dataset.yaml` is aiperf-only, `sharegpt.yaml`
  vllm-benchmark-only, `agentic_code_generation.yaml` inference-perf-only), and
  `inspect_workload_profile` already resolves per-harness, but the gate didn't. The allowlist `ref_catalog`
  check is also union-only, so NO layer enforced the `(harness, workload)` pairing.
- **Root cause:** a plan with `harness="inference-perf"`, `workload="dataset.yaml"` (an aiperf-only profile in
  the union) validated clean, then the run uses `-w dataset` against inference-perf which has no such profile —
  the executed run differs from the APPROVED plan and fails downstream.
- **Fix:** when `workloads_by_harness` is present AND lists the chosen harness, scope the suffix-tolerant
  workload membership check to that harness's profiles (rejecting cross-harness mismatches); fall back to the
  union otherwise (partial/absent catalog — preserves prior behavior).
- **Regression test:** `tests/test_schemas.py::test_session_plan_rejects_workload_from_wrong_harness`.

## BUG-062 — a non-dict top-level Job element crashes `classify_job_status`, aborting the recovery loop
- **Status:** FIXED
- **Severity:** low-medium (a forged or forward-incompatible `items` array element raises `AttributeError`
  and aborts the stateless `watch()`/`reconstruct()` loop the orchestrator's recovery depends on)
- **Where:** `app/orchestrator/job.py::classify_job_status` (~line 380).
- **Trigger:** `app/orchestrator/kube.py::parse_items` (line 44) returns `list(data.get("items") or [])` with
  NO per-element type filter (unlike its siblings `readiness`/`diagnostics._parse_items` and
  `tools/probe._items_from_json`, which both `isinstance(i, dict)`-filter). Its output feeds `list_jobs`, and
  `controller.status()`/`reconstruct()` pass each element straight to `classify_job_status`, whose first line
  `job_obj.get("metadata", ...)` assumes a dict. A non-dict element (bare string / null / number from a forged
  or forward-incompatible `items` array) raised `AttributeError` and aborted the recovery loop.
- **Root cause:** `classify_job_status` had been hardened only for malformed CHILDREN (BUG-023 counts,
  BUG-037 conditions); the sibling `classify_failure` already filters non-dict pods at the top (BUG-029) —
  this was the missing twin.
- **Fix:** a top-level `if not isinstance(job_obj, dict): return JobStatus(name="", phase=ABSENT)` guard so a
  malformed top-level Job degrades to ABSENT instead of crashing. (Deeper root cause = `kube.parse_items` not
  filtering; the fix is at the only unguarded consumer — `list_pods->classify_failure` and
  `list_configmaps->checkpoint` already filter — to keep blast radius minimal and match the BUG-029 pattern; a
  future defense-in-depth pass could filter in `parse_items` itself.)
- **Regression test:** `tests/test_orchestrator_controller.py::test_classify_survives_non_dict_job_obj`.

---

## Round 24 (2026-06-21) — subagent wave 11 (meta-review sibling): 1 fixed (BUG-063 med)
A 3rd adversarial meta-review re-audited the prior wave's eight fixes (BUG-055..062), verified seven of them
sound, and surfaced the still-unfixed SIBLING of BUG-062 in the checkpoint path — filed and fixed below as
BUG-063.

> **Noted, not yet filed (being fixed separately):** the meta-review also flagged that
> `app/tools/report_locate.py`'s `results_dir` parameter — like the now-fixed `session_id` (BUG-055) — is
> still joined as a **raw agent-supplied path with no containment**, a path-traversal sibling. Tracked for a
> separate fix, not part of this round.

## BUG-063 — a non-dict top-level ConfigMap element crashes `parse_checkpoint`, aborting sweep recovery
- **Status:** FIXED
- **Severity:** medium (a forged/corrupt `configmaps` array element raises `AttributeError` and aborts
  `reconstruct_sweep` — the restarted-orchestrator sweep-recovery path — breaking the function's documented
  "never an error" contract)
- **Where:** `app/orchestrator/checkpoint.py::parse_checkpoint` (~line 159).
- **Trigger:** `app/orchestrator/kube.py::parse_items` does NOT filter non-dict elements from its `items`
  list, and `CheckpointStore.load` hands `cms[0]` (the first element of that unfiltered list) straight to
  `parse_checkpoint`, which had only a FALSY `if not configmap:` guard. A non-dict element (bare string /
  number / list from a forged or corrupt `kubectl get configmaps -o json`) is TRUTHY, sails past the falsy
  guard, and `configmap.get("data")` raises `AttributeError`.
- **Root cause:** the unfixed SIBLING of BUG-062 (and the same robustness class as BUG-029/062) — the falsy
  guard caught the absent/None case but not a truthy non-dict, so a malformed top-level ConfigMap crashed the
  recovery path the restarted orchestrator depends on.
- **Fix:** replace `if not configmap:` with `if not isinstance(configmap, dict):` — this subsumes the
  absent/None case AND degrades any non-dict to an empty checkpoint, restoring the "never an error" contract.
- **Regression test:** `tests/test_orchestrator_checkpoint.py::test_parse_checkpoint_survives_non_dict_configmap`
  (the existing `::test_parse_checkpoint_tolerates_absent_or_corrupt_configmap` only covered
  None/empty-data/bad-JSON, never a non-dict).

---

## Round 25 (2026-06-21) — subagent wave 12 (parse_items source-filter / cross-harness unknown) + 1 abandoned false-positive
Two fixes (BUG-064 closes the non-dict-`items`-element robustness class at its source; BUG-065 stops the
`unknown` pseudo-harness from polluting cross-harness metric attribution) plus one investigated-and-dropped
false positive recorded so it is not re-chased.

## BUG-064 — `parse_items` lacks a per-element type filter, leaving an unguarded non-dict-element residual consumer
- **Status:** FIXED
- **Severity:** low-medium (only reachable through the chaos drill against a real cluster returning corrupt
  JSON — double-gated behind `chaos_enabled=true` + the `run_resilience_drill` tool — but a genuine
  `AttributeError` crash there)
- **Where:** `app/orchestrator/kube.py::parse_items` (~line 44).
- **Trigger:** `parse_items` returned `list(data.get("items") or [])` with **no per-element type filter**,
  unlike its siblings (`readiness/diagnostics._parse_items`, `tools/probe._items_from_json`, which
  `isinstance`-filter). The crashing production consumers were already guarded one-by-one
  (`classify_failure` BUG-029, `classify_job_status` BUG-062, `parse_checkpoint` BUG-063), but the chaos
  decorator's `ChaosKubeClient._run_id_of` remained an **unguarded residual consumer**: a non-dict `items`
  element → `"forged".get("metadata")` → `AttributeError`.
- **Root cause:** the defensive guards had been applied per-consumer rather than at the shared source, so any
  not-yet-hardened consumer of `parse_items` (here `_run_id_of`) inherited the same non-dict-element crash
  class.
- **Fix:** Path A (source filter — the minimal complete closure of the non-dict-element class):
  `return [it for it in (data.get("items") or []) if isinstance(it, dict)]`. `kubectl` `items` are always
  JSON objects so no legitimate item is dropped, and the per-consumer guards (BUG-029/062/063) become
  belt-and-suspenders.
- **Regression test:** `tests/test_orchestrator.py::test_parse_items_drops_non_dict_elements` (+
  `::test_list_jobs_filters_forged_non_dict_items`).

## BUG-065 — the `unknown` pseudo-harness pollutes cross-harness metric attribution (false "cross-validated")
- **Status:** FIXED
- **Severity:** medium (reports a WRONG cross-harness fact — tells the agent a metric was cross-validated by
  ≥2 harnesses when only one REAL harness measured it)
- **Where:** `app/validation/report.py::compare_across_harnesses` (~line 694, the `measured_by` population
  loop), reached by the tool `app/tools/multiharness.py::compare_harness_runs`.
- **Trigger:** `compare_across_harnesses` groups runs by harness, using `"unknown"` for reports whose
  `scenario.load.standardized.tool` couldn't be read (kept VISIBLE by design, never dropped). But the
  metric-attribution map `measured_by` was built over EVERY group including `"unknown"`. So a metric M
  measured by one REAL harness (e.g. `inference-perf`) AND one unknown-harness report had
  `len(measured_by[M]) == 2` → classified as `shared` (cross-validated by ≥2 harnesses) and excluded from
  `unique_metrics` (which needs `len == 1`); the unknown report also showed up as a `per_harness` entry under
  that metric's `cross_metrics` row. Realistic trigger: a session that ran 2+ harnesses where one
  BR-v0.2-valid report has `scenario.load: null`/no tool (the schema permits a null load), so its harness
  reads as `unknown`.
- **Root cause:** `"unknown"` is a visibility placeholder for un-attributable reports, not a real harness, but
  the attribution loop treated it as one — inflating the measured-by count and mislabeling single-harness
  metrics as cross-validated.
- **Fix:** skip the `"unknown"` group when populating `measured_by`, so `shared`/`unique`/`cross_metrics` are
  over real harnesses only; `"unknown"` stays fully visible in `harness_view`/`harness_names`/`runs`. Distinct
  from BUG-031 (skip-routing) and BUG-050 (unit normalization).
- **Regression test:** `tests/test_multiharness.py::test_cross_harness_unknown_does_not_pollute_shared_metrics`.

### Abandoned false-positive — `report_locate.py` `results_dir` containment (investigated, DROPPED, NOT shipped)
A hunter proposed containing `report_locate.py`'s agent-supplied `results_dir` to `ctx.workspace` (a sibling of
BUG-055's `session_id` `_session_root` containment). It was investigated and **dropped, not shipped**: unlike
`session_id` (an identifier always within the sessions root), `results_dir` is a **free-form path argument by
design** — the existing test `tests/test_simulate.py::test_locate_report_synthesizes_in_simulate` sets
`ctx.workspace=<tmp>/ws/sessions/sim` and passes `results_dir=<tmp>/results` (deliberately OUTSIDE the
workspace) and asserts `locate_and_parse_report` handles it gracefully (synthesize in SIMULATE / `found=False`
in real), NOT raise. The containment made the full-suite hook go RED (the merge was correctly aborted, not
bypassed). The read primitive is narrow anyway (only files literally named `benchmark_report_v0.2*.{yaml,json,yml}`).
Net: treating `results_dir` as arbitrary is intended behavior; locking it down is a maintainer decision, not a
bug. (The `session_id` containment, BUG-055, remains correct — it passed the full suite.)

---

## Round 26 (2026-06-21) — subagent wave 13 (mid-turn compaction)
One fix (BUG-066 closes a transcript-compaction timing gap: compaction ran exactly once at turn start and was
never re-evaluated as a single long turn grew, so the replayed context could overflow the provider *within* the
same turn). Plus the saturation note below: the 4th adversarial meta-review returned the first NO-BUG result.

**Round 26 — saturation note:** the 4th adversarial meta-review (of fixes 059-065, incl. the BUG-064
`parse_items`-shorter-list ripple — all `[0]` indexers verified length-guarded) was the FIRST meta-review to
return NO BUG (the prior three each caught a real sibling: BUG-044/052/063), and fresh-area hunters are now
overwhelmingly NO-BUG (ui, llm, deploy, instrument, auth, converters, workload_profile) — coverage across
logic/security/concurrency/infra/correctness is effectively exhaustive; remaining yield is deep long-tail.

## BUG-066 — transcript compacted only once at turn start, never re-evaluated as a long turn overflows mid-turn
- **Status:** FIXED
- **Severity:** medium (a single long tool-heavy turn can re-send a growing transcript that crosses the
  threshold, eventually a provider context-overflow that aborts the turn)
- **Where:** `app/agent/loop.py` (~line 122, the single `compact_messages` call sitting BEFORE the
  `for _ in range(MAX_STEPS)` loop).
- **Trigger:** the transcript was compacted exactly ONCE, at turn start — when it's still below the
  `_COMPACT_THRESHOLD_CHARS` (48000) threshold, so a no-op — and never re-evaluated as the turn grows. But a
  single `run_turn` replays the WHOLE transcript to the provider on EVERY step, and one turn can append up to
  `MAX_STEPS × (several × _TOOL_RESULT_BUDGET=6000)` chars of new tool results, so the replayed context blows
  past the threshold WITHIN the same turn. Reproduced hermetically: a 20-step tool-every-step turn grows the
  per-call transcript 363 → 120447 chars, crossing 48000 at step 8, while `compact_messages` is invoked exactly
  once and old large tool results are never elided.
- **Root cause:** the mechanism documented to "keep the replayed transcript from growing without bound" was
  structurally unable to fire during the very turn that overflows it → the growing history is re-sent in full to
  the provider each step, eventually a provider context-overflow (400 input too long) that `loop.py:167-169` can
  only catch by ABORTING the turn.
- **Fix:** hoist the compaction into a local `_compact()` helper and call it before EVERY step (top of the loop
  body, after the abandoned-turn guard) in addition to turn start; `compact_messages` is idempotent and a cheap
  no-op below the threshold, so the per-step check only acts once the transcript actually crosses it, and pairing
  + the recent window are untouched (compaction only shrinks content strings, never drops/reorders messages).
- **Regression test:**
  `tests/test_context_mgmt.py::test_compaction_runs_mid_turn_when_a_long_turn_crosses_the_threshold`.

---

## Round 27 (2026-06-21) — subagent wave 14 (shutdown-sweep race)
One fix (BUG-067 closes a graceful-shutdown race: the SIGTERM turn-cancellation sweep took a single snapshot,
so a sibling turn finishing normally during the awaited cancellation could register a fresh follow-up turn that
the one-pass sweep never sees → it survives SIGTERM and orphans its Job/subprocess). Plus a cross-cutting
time/clock/TTL/age/deadline sweep that returned NO BUG.

**Verified clean (no bug) — round 27:** a cross-cutting time/clock/TTL/age/deadline sweep (prewarm TTL, watch
`max_wait`, quota UTC-day window, rate-limiter refill, retention age, runner timeout, pod age, channel timer,
persisted timestamps) found every site uses the correct clock (monotonic vs wall), units, comparison direction,
and rollover/skew handling — NO bug.

## BUG-067 — graceful-shutdown turn-cancellation sweep snapshots once, missing a turn registered mid-sweep
- **Status:** FIXED
- **Severity:** medium-high (a turn can survive SIGTERM and orphan its K8s Job / leak its subprocess — the exact
  failure `graceful_shutdown` exists to prevent)
- **Where:** `app/agent/lifecycle.py::RunRegistry.shutdown` (~lines 151-170).
- **Trigger:** the graceful-shutdown handler (Phase 16) cancels every in-flight turn so a SIGTERM doesn't orphan
  K8s Jobs / leak subprocesses. It took a SINGLE snapshot `handles = self.active_handles()`, then cancelled each
  with `await self._cancel_handle(...)`. That await yields the event loop, and `run_turn`'s `finally`
  steer-backstop (`app/main.py` ~967-973) calls `app.state.runs.register(session.id, backstop)` whenever a turn
  ends NORMALLY with a queued steer — so a sibling turn finishing normally DURING the awaited cancellation of
  another turn registers a fresh follow-up turn that is NOT in the one-time snapshot. The single-pass sweep never
  cancels it → it survives SIGTERM and orphans its Job/subprocess. Reproduced deterministically (no sleeps): turns
  A,B in-flight at SIGTERM; shutdown snapshots [A,B], cancels A; during A's unwind await, B finishes normally and
  its finally spawns follow-up C via `register()`; C is registered after the snapshot, so the sweep (reaching B,
  now a no-op) never sees C → C survives.
- **Root cause:** a one-time snapshot of the active set cannot see a handle registered after the snapshot is taken
  but before the sweep finishes — and the steer-backstop deliberately registers such a handle as a normally-ending
  sibling turn unwinds during the awaited cancellation of another.
- **Fix:** re-sweep until a pass finds nothing active (an `id()`-keyed `seen` set avoids re-counting; the active set
  strictly shrinks each pass — a cancelled handle's task is done and won't reappear, and a backstop only spawns from
  a turn ending normally which can't happen once every live turn is cancelled, so it terminates); the end-of-sweep
  cleanup now forgets EVERY done handle (not just the cancelled names) so a same-session handle replaced mid-sweep
  leaves nothing stale. Found by a dedicated app-level concurrency/shared-state hunt (which confirmed
  `SessionManager._sessions`, the running check-then-register, the `run_semaphore` async-with, and
  channels/background_tasks all sound).
- **Regression test:**
  `tests/test_run_lifecycle.py::test_shutdown_cancels_run_registered_during_the_sweep`.

---

## Round 28 (2026-06-21) — subagent wave 15 (shutdown ordering / restart-proof)
Two fixes. BUG-068 closes a graceful-shutdown ordering gap: the teardown swept the run registry + the LLM
spare but never the BUG-033 read-only env pre-probe (tracked only in `app.state.background_tasks`), so a
SIGTERM landing mid-probe orphaned its `kubectl get nodes` / `kind get clusters` process group — the exact
leak `graceful_shutdown` exists to prevent. BUG-069 closes a restart-DURABILITY false negative: the
restart-recovery proof's duplicate check counted a per-attempt retry (a transient-fault `-a2` Job) as a
duplicate_apply, flipping the headline `no_duplicates` durability verdict to a false "NOT durable" and
inflating `run_after`. The retry state machine itself was re-verified sound.

## BUG-068 — graceful-shutdown never cancels the read-only env pre-probe, orphaning its subprocess group
- **Status:** FIXED
- **Severity:** medium (a SIGTERM mid-probe orphans the probe's process group — the exact subprocess leak
  `graceful_shutdown` exists to prevent)
- **Where:** `app/main.py::graceful_shutdown` (~line 164).
- **Trigger:** the teardown swept only the run registry (`await runs.shutdown()`) + the LLM spare
  (`await provider.aclose()`). But the read-only environment pre-probe (`_prewarm_env`, the BUG-033 task,
  kicked off on a brand-new chat connect at `main.py` ~1056) is tracked ONLY in
  `app.state.background_tasks` and is never `runs.register`'d — so `runs.shutdown()` never reaches it, and
  nothing else in shutdown/lifespan touches `background_tasks`. The probe runs real subprocesses
  (`kubectl get nodes`, `kind get clusters`) via `ctx.run_readonly`, and the runner reaps the child's
  process group only on `CancelledError` (`runner.py` ~283). So a SIGTERM landing mid-probe ORPHANS that
  process group.
- **Root cause:** BUG-033 made the pre-probe a tracked task so it survives GC, but nobody made shutdown
  cancel it — `background_tasks` is touched by no teardown path, so the probe is never awaited or cancelled
  on SIGTERM and its `CancelledError`-gated reaper never fires.
- **Fix:** between `runs.shutdown()` and `provider.aclose()`, cancel every still-live task in
  `app.state.background_tasks` and `await asyncio.gather(..., return_exceptions=True)` their unwind (so the
  runner's `CancelledError` path SIGKILLs the child group and shutdown waits for the reap); wrapped in
  `contextlib.suppress` so a misbehaving probe can't abort the later provider close. Distinct from BUG-067
  (the run-sweep itself) and BUG-045 (`aclose` awaits).
- **Regression test:**
  `tests/test_run_lifecycle.py::test_graceful_shutdown_cancels_background_pre_probe`.

## BUG-069 — restart-recovery proof counts a per-attempt retry as a duplicate, flipping the durability verdict
- **Status:** FIXED
- **Severity:** medium (a false "NOT durable" report — the resilience drill's restart-durability proof
  wrongly fails, and `run_after` is inflated, on any treatment that retried during the resume pass)
- **Where:** `app/orchestrator/restart.py::prove_restart_recovery` (~line 123, sweep mode).
- **Trigger:** `_applied_job_run_ids` strips each Job's `-a<N>` attempt suffix back to the logical treatment
  id (so an attempt-2 retry is the same logical treatment), but the duplicate check did
  `all_applied.count(t) > 1` over those stripped ids. A treatment that RETRIED during the resume pass
  (transient fault → fresh `-a2` Job) appears once PER ATTEMPT in `all_applied`, so `count>1` → it is wrongly
  counted as a `duplicate_apply` → flips the headline `no_duplicates` durability verdict to False (a false
  "NOT durable" report) and inflates `run_after` (each attempt counted as a separate treatment run). A retry
  is one logical treatment, not a duplicate.
- **Root cause:** the duplicate invariant was expressed as "appears more than once in the stripped applied
  list," but the stripped list legitimately contains one entry per attempt; a retry within a single resume
  pass is the same logical treatment run once, not a re-run after completion.
- **Fix:** count DISTINCT logical treatments applied during the resume pass for `run_after`
  (`sorted(set(all_applied[len(before):]))`), and redefine a duplicate as a treatment re-run AFTER it was
  already applied pre-restart (`before_ids = set(before); duplicates = sum(1 for t in newly_applied if t in
  before_ids)`) — the true durability invariant (the checkpoint must skip an already-completed treatment,
  never re-run it). All existing restart tests used `max_attempts=1` so no retry could occur during the
  proof's resume pass — the bug was never exercised. The retry state machine ITSELF was verified sound (the
  `range(1, max_attempts+1)` budget loop has no off-by-one; `-aN` DNS-1123 overflow is unreachable since
  `max_attempts` is schema-bounded `le=5`; resubmit always uses a distinct `-aN`; dead-letter retains
  `final_failure`; sweep checkpoint keys on the stable base `run_id`).
- **Regression test:**
  `tests/test_orchestrator_restart.py::test_sweep_resume_retry_is_not_counted_as_duplicate_job`.

---

## Round 29 (2026-06-21) — subagent wave 16 (public-share path leak)
One fix. BUG-070 closes an info-disclosure leak in the public, unauthenticated share path: a card tool's
result was frozen verbatim into the share snapshot, baking the server's filesystem layout + username AND the
owning session id into the publicly-readable link. Plus two clean sub-sweeps (the approval-gate lifecycle and
a 5th adversarial meta-review of BUG-066..069) that returned NO bug.

**Verified clean (no bug) — round 29:** the approval-gate lifecycle (quota→approval→run→record ordering,
double-resolve guard, restored-future path, orphaned-in_flight/half-run on reject, gate_surfaced/working-timer,
multiple gates per turn, resolve-vs-create race, reconnect reemit) was thoroughly traced + probed — NO bug. And
the 5th adversarial meta-review of BUG-066..069 (compaction/shutdown/lifecycle/restart) returned NO bug, with a
full enumeration confirming EVERY `asyncio.create_task` in `app/` is tracked (run registry / background_tasks /
owned inline via async-with-or-async-gen `finally`) so nothing is orphaned on shutdown.

**Noted, not yet filed (→ BUG-071):** `shares/` is NOT in `retention.MANAGED_AREAS`, so share snapshots + their
`.gist` token mappings accumulate without GC despite `share.py`'s docstring — being addressed separately (BUG-071).

## BUG-070 — public share snapshot freezes server-internal report paths into an unauthenticated link
- **Status:** FIXED
- **Severity:** medium (info-disclosure — a public, unauthenticated share link discloses the server's
  filesystem layout + username AND the owning session id, with no UI purpose)
- **Where:** `app/main.py::create_share` (~line 716).
- **Trigger:** a public, UNAUTHENTICATED share (`GET /api/share/{token}` and the offline export
  `GET /api/share/{token}/page.html`) froze each card tool's result verbatim into the snapshot, including
  `locate_and_parse_report`'s `report_path = <sessions_root>/<session_id>/.../benchmark_report_v0.2.json`
  (`report_locate.py:67`) and a not-found probe's `searched` dirs (`report_locate.py:59`). Those were persisted
  in `session.card_results` (`loop.py` ~289), replayed by `_history_items` → `_card_result_items`, and
  `create_share` never scrubbed them — so the link disclosed the server's filesystem layout + username AND the
  owning session id baked into the path, even though `read_share` strips `source_session_id` and
  `shared_chat._PUBLIC_FIELDS` omits it. The read-only viewer renders only summary/charts (charts are already
  session-relative), so the path fields were a pure leak with no UI purpose.
- **Root cause:** `create_share` snapshotted card tool results verbatim and never scrubbed the path-bearing keys,
  while the rest of the share path was already careful to omit the owning session id (`read_share` strips
  `source_session_id`; `_PUBLIC_FIELDS` omits it) — the report-path fields were the one place the session id +
  server filesystem layout still leaked into the unauthenticated snapshot.
- **Fix:** `_redact_share_items()` in `app/main.py`, called in `create_share` after building items, returns NEW
  item dicts (the live session is never mutated) with the path-bearing keys (`report_path`, `searched`) removed
  from any `tool_result` `result`, preserving `summary`/`charts`/`metrics`; scoped to the share path only (owner
  resume legitimately shows the owner their own paths).
- **Regression test:**
  `tests/test_share.py::test_share_snapshot_redacts_internal_report_paths`.

---

## Round 13 (2026-06-21) — LIVE agent-flow testing (real LLM, SIMULATE=1): clean, + 1 SIMULATE-scope observation
The closest substitute for the (unavailable) Chrome UI drive: a throwaway instance with the REAL
`claude-agent-sdk` provider (Max plan) but `SIMULATE=1` + a temp workspace + `UNRESTRICTED_TOOLS=0`
(real LLM turns, faked command execution, no user data, no real cluster) on :8012, driven over `/ws`.

**Two full agent flows driven end-to-end — both ran CLEAN (0 error events, 0 server tracebacks, coherent
summaries, correct event ordering, SessionPlan gate approved → flow proceeded → `done`):**
- Flow 1 (single-run): `fetch_key_docs → propose_session_plan →[approve]→ execute_llmdbenchmark ×3 →
  locate_and_parse_report → suggest_next_steps`. Report rendered via the frontend's `renderReportSummary`
  from the `tool_result` (NOT a `results_card` — by design, see results_card.py:9-16; debunked as a non-bug).
- Flow 2 (sweep+analyze): `…→ generate_doe_experiment → execute_llmdbenchmark ×2 → analyze_results →
  compare_reports → locate_and_parse_report → suggest_next_steps`. Also clean to `done`.
This validates the live turn loop, tool dispatch, streaming deltas, the approval gate, session persistence,
and the BUG-019/032 socket/replay paths in a real end-to-end run.

### SIMULATE-mode OBSERVATION (not patched — fix is a feature/design decision)
- **What:** in SIMULATE mode, `analyze_results` and `compare_reports` (and therefore sweep analysis) return
  empty — `{"analyzed": false, "reason": "no valid benchmark report to analyze", "skipped": []}` /
  `{"compared": false, "reason": "need at least two valid reports…"}` — so a SIMULATE dry-run/demo of the
  ANALYZER/sweep path shows no results card and no analysis.
- **Root cause:** only `locate_and_parse_report` has a synthetic-report branch (`report_locate.py:42`,
  `if ctx.settings.simulate: return <fabricated summary>`). `SimRunner` writes NO report files, and
  `analyze_results`/`compare_reports` read report FILES from disk (`find_reports`/`load_report`) → they find
  none. So SIMULATE's "walk the WHOLE workflow, synthetic report produced" promise covers the single-run
  report but NOT the analyzer/comparison/sweep paths.
- **Reachability:** SIMULATE-only (real mode writes real reports the analyzer reads fine) — a dry-run/demo
  degradation, not a real-operation correctness bug.
- **Why NOT patched:** the fix is to fabricate synthetic ANALYZER output (SLO verdicts + Pareto frontier in
  the validated BR v0.2 shape) for SIMULATE — a non-trivial feature with real judgment (what synthetic
  verdict values?), and whether SIMULATE should cover the analyzer path is a maintainer scope decision (the
  synthetic branch was added to `locate_and_parse_report` only, possibly deliberately). Recommended minimal
  step if desired: a SIMULATE-aware `reason` ("simulate mode produces no analyzer report files — use
  SIMULATE=0 for real analysis") so the empty result doesn't read as a failure during a demo.

---

## Round 14 (2026-06-21) — REAL Chrome browser drive of the UI at http://127.0.0.1:8000 (the goal's modality)
The goal asked specifically to use the Claude Code Chrome integration against the app. That literal feature
is **officially unsupported here**: Anthropic's docs (code.claude.com/docs/en/chrome) state *"WSL … is also
not supported,"* and it requires an interactively-installed Chrome **extension** + native-messaging host + a
`claude --chrome` session — none of which exist in this headless WSL bg job. Faithful substitute that DOES
work: a **real Google Chrome 149** (present on the box; driven via Playwright `channel="chrome"` headless —
the earlier failure was only Playwright's *bundled chromium* host-validation on Ubuntu 26.04, unrelated to
the real Chrome channel) pointed at the app on `:8000` (WSL-local, trivially reachable).

**Drove the actual rendered UI end-to-end — ALL CLEAN (0 console errors, 0 uncaught JS exceptions throughout):**
- Page load + visual inspection (screenshot): brand/sidebar/header/welcome card/suggestion chips/composer all render correctly.
- Interactions: theme toggle (dark↔light), composer accepts text, new-chat, send→error path renders a clean error bubble ("LLM provider not configured").
- Responsive: mobile (420×520) layout clean; the **builder dialog on a SHORT (900×400) viewport fits within the viewport with its action row reachable** — geometrically confirms the BUG-006/015 dialog-clipping fix holds (dlgBottom 380 ≤ vh 400, "Send to assistant" visible).
- **Full provider-backed agent flow (real LLM, SIMULATE=1) rendered in Chrome:** SessionPlan **approval card** (use-case/spec/namespace/harness/workload/steps/notes + `mutating` badge + Approve/Reject) → clicked **Approve** → the 7-step workflow rail advanced to **all ✓** → **Report-Summary metrics card** rendered (Requests 120, Success 100%, throughput 5,000 tok/s, TTFT p50 130ms / p90 210ms, ITL 47ms — the `renderReportSummary` path) → suggested-next-steps chips + per-turn token footer. Validates the frontend fixes (BUG-019/024/025/032) in a real browser.
- **Non-bugs (verified):** two missing-glyph `□` boxes in the headless screenshots are EMOJI the headless Linux Chrome has no font for — a chip's leading `✨` (U+2728) and a `✅`-like glyph after "100%". Confirmed via codepoints; they render fine on real user browsers (Win/Mac with emoji fonts). NOT product bugs.

**Result:** the rendered UI + a complete live agent flow are clean in a real Chrome browser — no new bugs. The Chrome-interaction modality is now satisfied as faithfully as a headless WSL environment permits (the literal extension feature being Anthropic-documented-impossible here).

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
- **`JobSpec.labels()` / `build_job_manifest` don't validate label VALUES against the K8s 63-char
  label-value limit** (`app/orchestrator/job.py`) — `session_id` / `sweep_id` are stamped into label
  values with no length check. Not cleanly triggerable today: normal sessions use a 12-char uuid hex,
  and sweeps validate the `sweep_id`, so no current caller can exceed 63 chars. Left unfixed — noted
  here for completeness (a future longer id source would need a length guard / truncation before this
  becomes a real `kubectl apply` rejection). (Surfaced in Round 15, wave 1.)

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
- **`ui/app.js` full re-read (Round 15, wave 2)** — a hunter did a line-by-line re-read of all 3266 lines
  of `ui/app.js` plus the WS/event contract and found NO new defect; the BUG-019/024/025/026 fixes are all
  present and hold.

## HTTP API probe — robustness confirmed (no bugs)
Drove the REST surface like a user; all behaved correctly:
- `GET /api/sessions/{bad}/artifact` → 422 (FastAPI requires the `path` query param before the
  handler runs). Not a bug — the UI always supplies `path`; a bare hit is simply rejected.
- `GET /api/share/{bad}` → 404; `DELETE /api/sessions/{bad}` → 404; `DELETE /api/share/{bad}` → 404.
- `GET /api/history/trend` with no `metric` → 422; with an unknown metric → 200 + `{error, available_metrics}`.
- Full share lifecycle (create → read → page.html (257 KB) → revoke → read=404) works end to end.
- `DELETE /api/namespaces/no_namespace` correctly removes all no-namespace chats (documented sentinel).
