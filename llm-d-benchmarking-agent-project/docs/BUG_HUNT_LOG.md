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

### SECURITY OBSERVATION (latent, NOT exploitable today; NEEDS MAINTAINER DECISION — not patched)
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
