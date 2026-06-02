export const meta = {
  name: 'roadmap-v2-autopilot',
  description: 'Autonomously implement Roadmap v2 (Phases 11-18: production operability, trust & quality) for the llm-d-benchmarking-agent as fresh-context agents in isolated git worktrees, adversarially verify each (3 lenses), and serially integrate into feature/roadmap-v2 (NEVER main). Every phase is a separate agent context; the orchestrator holds only short summaries. Resumable via resumeFromRunId.',
  whenToUse: 'Continue the llm-d-benchmarking-agent with the v2 operability/quality roadmap autonomously without filling the main conversation context.',
  phases: [
    { title: 'Prep', detail: 'create the feature/roadmap-v2 integration worktree off main, copy .env, record baseline, detect already-done v2 phases' },
    { title: 'Wave 1', detail: 'P11 structured logging + correlation IDs (foundational) — implement+verify+integrate' },
    { title: 'Wave 2', detail: 'P12 auth/rate-limit/CORS + P13 allowlist timeouts/quotas — parallel implement+verify, serial integrate' },
    { title: 'Wave 3', detail: 'P15 WS protocol+live buffer + P18 workspace retention/GC+self-check — parallel implement+verify, serial integrate' },
    { title: 'Wave 4', detail: 'P16 run cancel/reattach+graceful shutdown+/readyz + P17 ops docs+alerts — parallel implement+verify, serial integrate' },
    { title: 'Wave 5', detail: 'P14 quality gates (ruff+mypy+coverage in CI) — solo, lints the final integrated tree' },
  ],
}

// ----------------------------------------------------------------------------
// Constants (verified against on-disk state)
// ----------------------------------------------------------------------------
const MONO  = '/home/tal/kind-quickstart-guide'          // main checkout: shares .git, has POPULATED read-only sibling repos
const PROJ  = 'llm-d-benchmarking-agent-project'
const INTEG = 'feature/roadmap-v2'                        // integration branch (NEVER main) — branched off main
const HOME  = '/home/tal/kqg-v2-home'                     // integration worktree — CREATED in Prep (v1's worktree is gone)
const PDIR  = HOME + '/' + PROJ                           // integration project dir
const VENV  = MONO + '/' + PROJ + '/.venv/bin/python'     // reuse the existing venv for ALL runs

// phase id -> slug; WAVES encode order. 11 is foundational (first); 15 lands before 16
// (16's reattach builds on 15's live buffer); 14 is solo LAST so it lints the final tree.
const SLUG  = { 11:'logging', 12:'authz', 13:'allowlist-gov', 14:'quality-gates', 15:'ws-protocol', 16:'run-lifecycle', 17:'ops-docs', 18:'workspace' }
const TITLE = {
  11:'Structured logging + correlation IDs',
  12:'API trust: auth + rate-limit + CORS',
  13:'Allowlist governance: per-command timeouts + quotas',
  14:'Quality gates: ruff + mypy + coverage',
  15:'WebSocket protocol hardening + live event buffer',
  16:'Run lifecycle & readiness',
  17:'Operability docs + alert rules',
  18:'Workspace lifecycle: retention/GC + startup self-check',
}
const WAVES = [ [11], [12,13], [15,18], [16,17], [14] ]

const wt = (id) => '/home/tal/kqg-v2-p' + id + '-' + SLUG[id]
const br = (id) => 'feature/roadmap-v2-p' + id + '-' + SLUG[id]

// ----------------------------------------------------------------------------
// Schemas (structured agent returns -> no parsing, validated at the tool layer)
// ----------------------------------------------------------------------------
const STATUS_SCHEMA = { type:'object', additionalProperties:false,
  properties:{ done:{type:'array', items:{type:'integer'}}, notes:{type:'string'} }, required:['done'] }

const IMPL_SCHEMA = { type:'object', additionalProperties:false, properties:{
  phase:{type:'integer'}, branch:{type:'string'}, worktree:{type:'string'},
  summary:{type:'string'}, filesChanged:{type:'array', items:{type:'string'}},
  passCount:{type:'integer'}, skipCount:{type:'integer'}, failCount:{type:'integer'},
  ok:{type:'boolean'}, blocker:{type:'string'} },
  required:['phase','branch','worktree','summary','passCount','failCount','ok'] }

const VERDICT_SCHEMA = { type:'object', additionalProperties:false, properties:{
  lens:{type:'string'}, acceptable:{type:'boolean'},
  blocking:{type:'array', items:{type:'string'}}, notes:{type:'string'} },
  required:['lens','acceptable','blocking'] }

const INTEG_SCHEMA = { type:'object', additionalProperties:false, properties:{
  phase:{type:'integer'}, merged:{type:'boolean'}, fullSuitePassed:{type:'boolean'},
  passCount:{type:'integer'}, skipCount:{type:'integer'}, failCount:{type:'integer'},
  timedOut:{type:'boolean'}, notes:{type:'string'} },
  required:['phase','merged','fullSuitePassed','passCount','failCount'] }

// ----------------------------------------------------------------------------
// The per-worktree test command. THE CRITICAL FIX vs v1: every run sets
// PYTHONPATH="$PWD" so the worktree's `app` wins over the shared venv's editable
// finder (which hardcodes app -> MONO/app); REPOS_DIR=MONO so sibling-repo tests
// run (not fail empty) inside a worktree; `timeout 600` so a hung test returns 124
// instead of wedging the pipeline (124 is treated as a failure to make hermetic).
// ----------------------------------------------------------------------------
const TESTCMD = (projDir) =>
  'cd ' + projDir + ' && REPOS_DIR=' + MONO + ' PYTHONPATH="$PWD" timeout 600 ' + VENV + ' -m pytest tests/ -q'

const TRAILER = 'Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>'

// ----------------------------------------------------------------------------
// Per-phase specs (authoritative; carried inline so impl/verify do NOT depend on
// ROADMAP.md pre-containing the new phase). NO backticks / no ${ } inside these.
// ----------------------------------------------------------------------------
const SPEC = {
11: `Phase 11 — Structured logging + correlation IDs.
GOAL: structured, correlated logs so a run can be traced after the fact.
BUILD (NO new runtime dependency — implement the JSON formatter yourself):
- A logging setup module (e.g. app/observability/logging.py) that configures Python stdlib logging with a JSON formatter (one JSON object per line). Do NOT add structlog/python-json-logger to pyproject.
- Settings in app/config.py: LOG_LEVEL (default "INFO") and LOG_FORMAT ("json" default, "text" for dev). Initialize logging once at startup (the FastAPI lifespan) in app/main.py.
- A correlation id via contextvars (e.g. app/observability/logctx.py with a ContextVar for corr_id, plus session_id/run_id). Set corr_id at the WebSocket handshake in app/main.py (one id per connection/turn). Thread the context so log records from the agent loop (app/agent/loop.py), tool dispatch (app/tools/context.py), and the command runner (app/security/runner.py) automatically include corr_id + session_id + run_id + tool. A logging.Filter that injects the contextvars is the clean mechanism.
- Replace any ad-hoc prints in the app package with logger calls. Emit structured lines at: turn start/end, each tool call start/result, each command exec (mode + exe + duration + exit code).
ACCEPTANCE: every emitted log line is valid JSON with the standard fields; a corr_id set at the WS boundary propagates to records from the loop, a tool, and the runner within one turn; no bare print() remains in the app package.
HERMETIC TEST: pytest caplog / a captured handler. Assert (a) the JSON formatter renders the expected keys; (b) within one simulated turn the same corr_id appears on records from the loop, a tool, and the runner; (c) the LOG_FORMAT=text path works. No network/cluster.
THIN-CODE NOTE: pure plumbing/mechanism (acceptable); put NO agent decision logic in Python.`,

12: `Phase 12 — API trust: optional auth + rate-limit + CORS.
GOAL: make the FastAPI surface safe to expose, while staying frictionless for local use.
BUILD (all FastAPI/stdlib — NO new dependency):
- New app/security/auth.py: optional Bearer-token auth. Settings in app/config.py: AUTH_ENABLED (default False) and AUTH_TOKEN (secret, default ""). When enabled, a FastAPI dependency guards the HTTP routes AND the /ws endpoint; compare with secrets.compare_digest (constant-time); return 401 on missing/bad token. When disabled (default), everything is open exactly as today.
- An in-memory token-bucket rate limiter that takes an INJECTABLE CLOCK (a callable returning a monotonic float) so tests are deterministic with NO sleeps. Settings: RATE_LIMIT_ENABLED (default False), RATE_LIMIT_RPS, RATE_LIMIT_BURST. Apply to the HTTP API / message intake; return HTTP 429 when the bucket is empty.
- CORS via fastapi.middleware.cors.CORSMiddleware with a configurable origins setting (CORS_ALLOW_ORIGINS, default empty = today's behavior). Wire the middleware in app/main.py.
ACCEPTANCE: with AUTH_ENABLED, protected routes/ws return 401 without a valid token and 200 with it; with RATE_LIMIT_ENABLED, over-budget requests get 429; CORS headers present when origins are configured. All three default OFF/open so existing flows and tests are unchanged.
HERMETIC TEST: FastAPI TestClient. Cover 401-without/200-with-token, the 429 over-budget path (advance the injectable clock — no real sleep), CORS headers, and that defaults keep the API open.
THIN-CODE NOTE: pure mechanism. Keep the token-bucket math small and clock-injected.`,

13: `Phase 13 — Allowlist governance: per-command timeouts + quotas (policy as DATA).
GOAL: move execution limits out of Python into the allowlist data, and add per-session/day usage quotas — WITHOUT putting decision logic in Python.
BUILD:
- Extend security/allowlist.yaml so an executable (and/or a subcommand) may carry an optional timeout_s (int) and an optional quota block with per_session and/or per_day integer caps. Update the allowlist loader/validator (app/security/allowlist.py) to parse and schema-validate these fields AT STARTUP (reject a malformed allowlist with a clear error).
- The command runner (app/security/runner.py) must enforce the per-command timeout_s sourced FROM the policy data. This SUPERSEDES the hardcoded app/tools/execute.py::_TIMEOUTS dict — remove that dict and source timeouts from the allowlist (ONE mechanism, not two). Keep a sane global default when a command has no timeout_s.
- Quota: a per-session usage COUNTER (mechanism) whose CAP comes from the YAML (data). When a command would exceed its per_session (or per_day) cap, refuse it with a clear structured error BEFORE execution. Counting/state is mechanism; the limit is data.
ACCEPTANCE: timeouts and quotas live purely in security/allowlist.yaml; _TIMEOUTS is gone; a malformed allowlist is rejected at load; over-quota commands are refused pre-exec; no new per-command knowledge in Python if/elif.
HERMETIC TEST: (a) a trivially-short fake command with a tiny timeout_s is killed and reported as a timeout; (b) the timeout value is honored FROM the YAML, not a Python constant; (c) a command with per_session quota N is refused on call N+1 within a session; (d) a malformed allowlist raises at load. Use the existing CaptureRunner/fake patterns + tiny timeouts or a fake clock — no real long sleeps.
THIN-CODE NOTE: this is the canonical "policy as data" win — keep ALL limits in YAML.`,

14: `Phase 14 — Quality gates: ruff + mypy + coverage in CI (RUNS LAST, SOLO).
GOAL: enforce code quality on the FINAL integrated tree. No behavior change to the app.
BUILD:
- Add ruff, mypy, pytest-cov to the dev extras in pyproject.toml and add tool config: [tool.ruff] with a sensible ruleset that matches the existing style (avoid churn-heavy rules), [tool.mypy] at a meaningful but ACHIEVABLE level (do not impose --strict on the whole tree if it would require a rewrite; scope strictness to core modules, relax third-party-shaped code), and pytest-cov/[tool.coverage] config.
- Install the tools into the shared venv (you are the only agent running — no race): VENV -m pip install ruff mypy pytest-cov  (or pip install -e ".[dev]" after editing pyproject).
- MAKE THE TREE CLEAN: run ruff check and fix all findings; run mypy app and fix/resolve type errors with precise annotations (avoid blanket type: ignore — use targeted ignores WITH error codes only where a dependency lacks stubs).
- Coverage: measure current coverage, then set --cov-fail-under to a threshold a few points BELOW the measured baseline (do NOT hardcode 80%). Record the measured number in your summary.
- Wire it up: Makefile targets lint (ruff), typecheck (mypy), coverage (pytest --cov --cov-fail-under=N); add steps to .github/workflows/agent-flow-validation.yml that run ruff + mypy + the coverage-gated suite. KEEP the existing hermetic flow-validation job.
ACCEPTANCE: ruff check exits clean, mypy app exits clean, the full suite passes with the coverage gate, and CI runs all three. The functional test count must NOT drop (never delete tests to pass coverage).
HERMETIC TEST: the gates ARE the test — the integrator re-runs ruff/mypy/the covered suite. Optionally add a tiny test asserting the config/targets exist.
THIN-CODE NOTE: config + cleanup only; no app behavior change.`,

15: `Phase 15 — WebSocket protocol hardening + live event buffer.
GOAL: validate the WS wire protocol and let a reconnecting client see the LIVE stream, not just a replayed end-state (resolves the v1 Phase-2 deferral).
BUILD:
- Pydantic models for ALL inbound WS messages (user_message, approval, ping, ...) and ideally typed envelopes for outbound events, in a new module (e.g. app/agent/ws_schemas.py). Validate inbound frames; on a malformed frame send a structured error event back and KEEP the socket alive (do NOT crash the handler). Wire validation into the /ws handler in app/main.py.
- A per-session LIVE event buffer / simple pub-sub (in app/agent/channel.py and/or app/agent/session.py): while a turn runs, events are appended to a BOUNDED ring buffer and fanned out to any connected client. On reconnect, replay the buffered events for the active turn so a client that dropped mid-run catches up to the LIVE stream (not only the final result). Cap the buffer size to avoid unbounded memory.
ACCEPTANCE: malformed inbound frames are rejected with a structured error and the connection survives; a client that disconnects and reconnects mid-turn receives the events it missed and then continues live; existing happy-path WS behavior unchanged.
HERMETIC TEST: extend tests/test_ws.py (FastAPI TestClient WS). Cover (a) a malformed frame -> structured error, socket still usable; (b) reconnect mid-turn replays buffered live events; (c) the buffer is bounded. No real cluster.
THIN-CODE NOTE: mechanism only; encode no agent decisions. INTEGRATOR NOTE: app/agent/channel.py and the app/main.py /ws handler are structural-wiring files — reconcile, do not blind-concatenate.`,

16: `Phase 16 — Run lifecycle & readiness.
GOAL: never let an abandoned run hold a concurrency slot; shut down gracefully; expose readiness (resolves the v1 Phase-2/3 deferral). Builds on Phase 15's live buffer for reattach.
BUILD:
- CANCEL: a way to cancel a running benchmark/turn that RELEASES its concurrency-cap semaphore slot and cleans up (terminate the subprocess / await-cancel the task, mark the run cancelled). Surface it as a NEW agent tool (register in app/tools/registry.py + app/tools/schemas.py) and/or a control message, plus a knowledge/*.md note on WHEN to cancel (judgment lives in knowledge).
- REATTACH: allow a client/session to reattach to a still-running background run and stream its events via Phase 15's per-session buffer.
- GRACEFUL SHUTDOWN: a SIGTERM handler (registered in the app lifespan) that, on shutdown, cancels or cleanly detaches in-flight runs rather than orphaning K8s Jobs / leaking subprocesses. The handler MUST be a plain function/coroutine a test can invoke DIRECTLY (do NOT require sending a real OS signal in tests).
- READINESS: split /healthz (liveness, keep minimal) from a NEW /readyz (readiness) that reports per-component status (provider configured, repos present, runner ok, workspace writable).
ACCEPTANCE: cancelling a run frees the semaphore slot (a subsequent run can start); the SIGTERM handler, called directly, cancels in-flight tasks without orphaning; /readyz returns structured component readiness; reattach streams buffered events.
HERMETIC TEST: use the fake runner / FakeKubeClient / CaptureRunner. Assert (a) cancel releases the concurrency semaphore (capacity restored); (b) calling the shutdown handler cancels tracked tasks; (c) /readyz reports components (TestClient); (d) reattach replays buffered events. No real signals, no live cluster.
THIN-CODE NOTE: cancel/shutdown are mechanism; the "when to cancel" guidance is knowledge. INTEGRATOR NOTE: app/main.py lifespan/health and app/agent/channel.py|session.py are structural — reconcile with the Phase 11/15 edits.`,

17: `Phase 17 — Operability docs + Prometheus alert rules.
GOAL: document the security posture and operations, and ship alert rules. Near-zero code risk; no app behavior change.
BUILD (match the existing convention — check where README/docs live, likely docs/):
- SECURITY.md: threat model — trust boundaries, the allowlist/approval model, secret handling/scrubbing, network-exposure guidance that pairs with Phase 12 (auth/rate-limit/CORS), what requires isolation.
- TROUBLESHOOTING.md: common failures -> what to check; debug mode; which logs to read (reference Phase 11 structured logs + corr_id).
- CONTRIBUTING.md: how to add a tool/flow/phase; the hermetic-test rule; the thin-code / allowlist-as-data law.
- CHANGELOG.md: Keep-a-Changelog format; summarize v1 phases 0-10 and the in-progress v2 operability work.
- A Prometheus alert-rules file under deploy/observability/ (e.g. alerts.rules.yaml) with a few meaningful rules over the EXISTING metrics (slow agent turns, elevated tool-error rate, elevated command failures). Valid Prometheus rule YAML referencing REAL metric names from app/observability.
ACCEPTANCE: the four docs exist and are accurate to THIS codebase (no invented features); the alert-rules file is valid Prometheus rule YAML referencing real metric names.
HERMETIC TEST: a docs-presence/structure test (like tests/test_packaging.py) asserting the files exist and contain expected sections; a test that the alert-rules YAML parses and references known metric names (skip an external promtool check if the binary is absent).
THIN-CODE NOTE: docs + data only.`,

18: `Phase 18 — Workspace lifecycle: retention/GC + startup self-check.
GOAL: stop unbounded growth of per-session/run scratch, and surface misconfiguration early.
BUILD:
- New app/storage/retention.py: a retention/GC pass over workspace/sessions/, workspace/runs/, and the history store, governed by CONFIG caps (mechanism reads caps from settings/data): RETENTION_MAX_AGE_DAYS, RETENTION_MAX_ITEMS, RETENTION_MAX_BYTES (choose safe, documented defaults; treat 0/None as unlimited). GC removes the OLDEST items beyond the caps and must NEVER delete an active/running session's data. Run GC at startup and/or on a periodic hook.
- Startup config self-check: validate workspace paths are writable, provider config is coherent, repos resolvable; return a STRUCTURED status object. Feed this status into the /readyz endpoint from Phase 16 (import/extend it; if /readyz is not yet present in your branch base, expose the self-check function and a minimal readiness contribution that the integrator/Phase 16 can compose).
- Settings in app/config.py for the retention caps + any self-check toggles.
ACCEPTANCE: GC prunes scratch beyond the configured caps and never touches active runs; the self-check returns a structured pass/fail with reasons and is reflected in readiness; defaults do not surprise existing users (document the default policy).
HERMETIC TEST: seed fake old session/run directories (controlling mtimes/sizes) in a tmp workspace and assert GC prunes exactly per policy and preserves a marked-active one; assert the self-check returns the expected structured status for a good and a broken config. Pure filesystem + tmp dirs; no network.
THIN-CODE NOTE: caps are data/config; the GC walk + counter are mechanism. No decision logic in if/elif.`,
}

// ----------------------------------------------------------------------------
// Prompts
// ----------------------------------------------------------------------------
const PREP_PROMPT = `Prepare a FRESH integration worktree for Roadmap v2 so phase branches merge cleanly. Use Bash. main is NEVER touched. Do EXACTLY:

1. Sanity: note (do NOT switch) the main branch with  git -C ${MONO} rev-parse --abbrev-ref HEAD . Run  git -C ${MONO} status --porcelain  (MONO should be clean; if there is unrelated uncommitted work, leave it and mention it in notes).

2. Create (or reuse) the integration branch ${INTEG} off main and its worktree ${HOME}:
   - If ${HOME} already exists as a worktree (git -C ${MONO} worktree list shows it): put it on ${INTEG} (git -C ${HOME} checkout ${INTEG}) and report reuse.
   - Else if branch ${INTEG} already exists:  git -C ${MONO} worktree add ${HOME} ${INTEG}
   - Else:  git -C ${MONO} worktree add -b ${INTEG} ${HOME} main
   Verify:  git -C ${HOME} rev-parse --abbrev-ref HEAD  ==  ${INTEG}

3. Make the integration worktree runnable + record the baseline:
   - Copy env if missing:  cp -n ${MONO}/${PROJ}/.env ${HOME}/${PROJ}/.env 2>/dev/null || true
   - Confirm the venv:  ${VENV} --version
   - Confirm the worktree's app wins on the path:  cd ${PDIR} && PYTHONPATH="$PWD" ${VENV} -c "import app; print(app.__file__)"  (MUST print a path under ${PDIR})
   - Run the suite to record the green baseline:  ${TESTCMD(PDIR)}   (record pass/skip counts)

4. Open the Roadmap v2 section in ${PDIR}/ROADMAP.md if absent: append a short header '## Roadmap v2 — production operability, trust & quality (Phases 11-18)' with one intro line (integration branch ${INTEG}, never main). Commit only if changed:
   git -C ${HOME} add ${PROJ}/ROADMAP.md && git -C ${HOME} commit -m "docs: open Roadmap v2 section" -m "${TRAILER}"

5. Detect resume state: read ${PDIR}/ROADMAP.md and report which of phases 11..18 are ALREADY marked DONE.

Return {done:[...ids already DONE...], notes:"baseline pass/skip counts + anything notable"}.`

function implPrompt(id) {
  return `You implement EXACTLY ONE roadmap phase — Phase ${id} "${TITLE[id]}" (${SLUG[id]}) — in your OWN isolated git worktree, then commit on its branch. Other agents may be implementing other phases in parallel in DIFFERENT worktrees — stay strictly inside yours. Favor correctness over speed. main is NEVER touched.

== Repo facts ==
- Main checkout (shares .git; has the POPULATED read-only sibling repos llm-d/ and llm-d-benchmark/): ${MONO}
- Integration branch (never merge anywhere yourself): ${INTEG}
- Project dir name inside any worktree: ${PROJ}
- Conventions/law: read ${PDIR}/CLAUDE.md and ${PDIR}/PROGRESS.md (thin code/thick agent; allowlist-as-data; hermetic tests; secrets; determinism; the current test baseline).

== The phase spec (AUTHORITATIVE; it is NOT yet in ROADMAP.md — implement from here) ==
${SPEC[id]}

== Step 1: create YOUR worktree off the integration branch ==
If ${wt(id)} already exists from a prior run:  git -C ${MONO} worktree remove --force ${wt(id)} ; git -C ${MONO} branch -D ${br(id)}  (ignore errors). Then:
  git -C ${MONO} worktree add -b ${br(id)} ${wt(id)} ${INTEG}
Verify:  git -C ${wt(id)} rev-parse --abbrev-ref HEAD  ==  ${br(id)} . Do ALL edits inside ${wt(id)}/${PROJ} ONLY.

== Step 2: implement per the spec. Hard rules ==
- THIN CODE, THICK AGENT: no decision logic in Python if/elif; mechanism in Python, judgment in knowledge/*.md|yaml.
- SECURITY: the allowlist is DATA (security/allowlist.yaml) — widen via YAML, never per-command Python. Commands are argv lists, shell=False. Read-only auto-runs; mutating needs approval.
- Read repo truth at runtime; NEVER edit the sibling repos llm-d/ or llm-d-benchmark/.
- NO new runtime dependency unless the spec explicitly allows it (only Phase 14 adds DEV tools).
- Tests: add/extend pytest under tests/ that MEANINGFULLY cover the feature (no vacuous asserts, no skip-to-pass, no xfail). HERMETIC ONLY — no live cluster, no GPU, no network, no long real runs; use the existing fakes (FakeKubeClient, CaptureRunner, the tests/test_ws.py TestClient harness, fake clocks).
- DO NOT edit ROADMAP.md or PROGRESS.md (the integrator owns those). You MAY add knowledge/*.md|yaml and docs.

== Step 3: run the suite from YOUR worktree ==
First confirm your app wins on the path:
  cd ${wt(id)}/${PROJ} && PYTHONPATH="$PWD" ${VENV} -c "import app; print(app.__file__)"   (MUST be under ${wt(id)})
Then:
  ${TESTCMD(wt(id) + '/' + PROJ)}
ZERO failures, and your NEW tests must pass. Iterate until green. (timeout 600: exit 124 = a hung test reached a real resource — make it hermetic, do NOT skip.)

== Step 4: commit (do NOT push, do NOT merge) ==
git -C ${wt(id)} add -A
git -C ${wt(id)} commit -m "<clear scoped Phase ${id} message>" -m "${TRAILER}"

Return the structured result. ok=true ONLY if the suite is green (failCount=0) AND the spec's ACCEPTANCE is genuinely met. summary <= 6 lines. branch=${br(id)}, worktree=${wt(id)}.`
}

function fixPrompt(id, feedback) {
  return `Phase ${id} ("${SLUG[id]}" — ${TITLE[id]}) FAILED review. Fix it IN PLACE in the existing worktree ${wt(id)} on branch ${br(id)} — do not create a new worktree.

Reviewer feedback (address EVERY blocking item):
${feedback}

Re-read the spec if needed:
${SPEC[id]}

Keep the hard rules (thin code/thick agent; allowlist-as-data; hermetic tests only; no new runtime dep except Phase 14; do NOT edit ROADMAP.md/PROGRESS.md). Then re-run, asserting your app wins on the path:
  cd ${wt(id)}/${PROJ} && PYTHONPATH="$PWD" ${VENV} -c "import app; print(app.__file__)"   (under ${wt(id)})
  ${TESTCMD(wt(id) + '/' + PROJ)}
It must be green (failCount=0). Amend or add a commit on ${br(id)} ending with:
${TRAILER}
Return the structured result with ok=true only if green and the blocking items are resolved.`
}

function verifyPrompt(id, lens) {
  const head = `Adversarially review Phase ${id} ("${SLUG[id]}" — ${TITLE[id]}) implemented on branch ${br(id)} in worktree ${wt(id)}. Be skeptical; default to acceptable=false if unsure. You are ONE of three independent lenses. Read-only review (re-running pytest is allowed). Inspect the diff:  git -C ${wt(id)} diff ${INTEG}...HEAD

The AUTHORITATIVE phase spec (NOT yet in ROADMAP.md):
${SPEC[id]}

`
  const lenses = {
    'acceptance':
      head + `LENS = ACCEPTANCE. Does the implementation ACTUALLY deliver the spec's BUILD + ACCEPTANCE (the real feature, not a stub)? Coherent and complete? List anything missing/incorrect as blocking.`,
    'tests-real':
      head + `LENS = TESTS-ARE-REAL. RE-RUN the suite yourself. First assert app origin:
  cd ${wt(id)}/${PROJ} && PYTHONPATH="$PWD" ${VENV} -c "import app; print(app.__file__)"   (must be under ${wt(id)})
  ${TESTCMD(wt(id) + '/' + PROJ)}
Confirm GREEN (0 failures, NOT a 124 timeout) and the NEW tests genuinely exercise the feature (meaningful assertions, not skipped/vacuous/tautological, matching the spec's HERMETIC TEST). If red, vacuous, or fake coverage, list it as blocking with the weak/failing test names.`,
    'philosophy-security':
      head + `LENS = PHILOSOPHY+SECURITY. Enforce: (a) thin code/thick agent — NO decision logic in Python if/elif; judgment in knowledge/*.md|yaml; (b) the allowlist stays DATA in security/allowlist.yaml, widened via YAML not Python, commands argv-only shell=False; (c) NO new runtime dependency (except Phase 14 dev tools); (d) NO edits to sibling repos llm-d/ or llm-d-benchmark/; (e) tests hermetic (no live cluster/GPU/network/long runs); (f) did NOT edit ROADMAP.md or PROGRESS.md. List every violation as blocking.`,
  }
  return lenses[lens]
}

function integratePrompt(id) {
  const slug = SLUG[id]
  const lintGate = id === 14
    ? `
3b. QUALITY GATES (Phase 14 only): from ${PDIR} run, using the shared venv,  ${VENV} -m ruff check . ,  ${VENV} -m mypy app , and the coverage-gated suite. ALL must pass (ruff clean, mypy clean, coverage >= the configured fail-under). If any fail, fix in the integration tree before proceeding.`
    : ''
  return `You are the SERIAL integrator (only one runs at a time). Merge the verified Phase ${id} ("${slug}") branch into ${INTEG}, gate on the FULL suite, write the state docs, and clean up. Use Bash. main is NEVER touched.

== Context ==
- Integration worktree (on ${INTEG}; runnable): ${HOME}   (project dir ${PDIR})
- Phase branch to merge: ${br(id)}   (implemented in worktree ${wt(id)})
- Shared venv python: ${VENV}

== Steps ==
1. Ensure the integration worktree is on ${INTEG} and clean:
   git -C ${HOME} rev-parse --abbrev-ref HEAD   (if not ${INTEG}:  git -C ${HOME} checkout ${INTEG})
   git -C ${HOME} status --porcelain   (must be clean; commit/stash stray WIP sensibly first)

2. Merge (no fast-forward):
   git -C ${HOME} merge --no-ff ${br(id)} -m "Merge Phase ${id} (${slug}) into ${INTEG}"
   CONFLICT POLICY (v2 hot files differ from v1):
   - ADDITIVE-REGISTRATION files (app/tools/registry.py, app/tools/schemas.py, app/agent/prompt.py, security/allowlist.yaml): KEEP BOTH SIDES' entries — never drop an existing tool/field/policy line.
   - STRUCTURAL-WIRING files (app/main.py, app/config.py, app/agent/loop.py, app/agent/channel.py, app/agent/session.py, app/security/runner.py, app/tools/execute.py): COMPOSITION-RECONCILE — merge both sides' logic into ONE coherent function / lifespan / middleware stack / handler that runs BOTH behaviors. Read both versions and write the deliberate union; do NOT blind-concatenate duplicate blocks.
   Then:  git -C ${HOME} add <files> && git -C ${HOME} commit --no-edit

3. AUTHORITATIVE full suite — from the integration worktree, TIMEOUT-bounded, worktree app on the path:
   cd ${PDIR} && PYTHONPATH="$PWD" ${VENV} -c "import app; print(app.__file__)"   (MUST be under ${PDIR})
   ${TESTCMD(PDIR)}
   Exit 124 = the suite HUNG (a test reached a real resource) — make that test hermetic; do NOT skip/xfail to hide it (set timedOut=true and merged=false if you cannot). Require 0 failures and a pass count >= the prior baseline in PROGRESS.md. If red, FIX the real integration problem (resolve the merge composition) — do NOT delete/weaken tests.${lintGate}

4. Update state docs ON ${INTEG} (you are the ONLY writer — no conflicts). Append to ${PDIR}/ROADMAP.md:
   "## Phase ${id} — ${TITLE[id]} — DONE" with a 2-4 line result (what shipped) and the new pass/skip counts.
   Append a short Phase ${id} entry to ${PDIR}/PROGRESS.md (what shipped + counts).
   git -C ${HOME} add ${PROJ}/ROADMAP.md ${PROJ}/PROGRESS.md && git -C ${HOME} commit -m "docs: mark Phase ${id} (${slug}) done" -m "${TRAILER}"

5. Cleanup:
   git -C ${MONO} worktree remove --force ${wt(id)}
   git -C ${MONO} branch -D ${br(id)}
   git -C ${MONO} worktree prune

Return {phase:${id}, merged, fullSuitePassed, passCount, skipCount, failCount, timedOut, notes}. Set merged=false (and leave the branch+worktree intact for review) if you could not land it cleanly.`
}

// ----------------------------------------------------------------------------
// Per-phase pipeline: implement -> 3-lens parallel verify -> (bounded fix+reverify)
// -> return readiness. Integration is done separately and SERIALLY by the caller.
// Wrapped so one agent error can't abort the wave.
// ----------------------------------------------------------------------------
async function preparePhase(id, waveTitle) {
  const Aj = (prompt, label, schema) => agent(prompt, { label, phase: waveTitle, agentType: 'general-purpose', schema })
  try {
    let impl = await Aj(implPrompt(id), 'impl:p' + id, IMPL_SCHEMA)
    if (!impl) return { phase: id, ready: false, reason: 'impl-null' }

    const lenses = ['acceptance', 'tests-real', 'philosophy-security']
    let verdicts = (await parallel(lenses.map(L => () =>
      Aj(verifyPrompt(id, L), 'verify:' + L + ':p' + id, VERDICT_SCHEMA)))).filter(Boolean)
    let blocking = verdicts.filter(v => !v.acceptable || (v.blocking && v.blocking.length))

    if (!impl.ok || blocking.length) {
      const feedback = JSON.stringify({ implOk: impl.ok, blocker: impl.blocker, verdicts }).slice(0, 6000)
      const fixed = await Aj(fixPrompt(id, feedback), 'fix:p' + id, IMPL_SCHEMA)
      if (fixed) impl = fixed
      verdicts = (await parallel(lenses.map(L => () =>
        Aj(verifyPrompt(id, L), 'reverify:' + L + ':p' + id, VERDICT_SCHEMA)))).filter(Boolean)
      blocking = verdicts.filter(v => !v.acceptable || (v.blocking && v.blocking.length))
    }

    const ready = !!(impl && impl.ok && !blocking.length)
    return { phase: id, slug: SLUG[id], ready, impl, blocking: blocking.map(v => ({ lens: v.lens, blocking: v.blocking })) }
  } catch (e) {
    return { phase: id, ready: false, reason: 'exception: ' + (e && e.message ? e.message : String(e)) }
  }
}

// ----------------------------------------------------------------------------
// Main
// ----------------------------------------------------------------------------
phase('Prep')
const status = await agent(PREP_PROMPT, { label: 'prep', phase: 'Prep', agentType: 'general-purpose', schema: STATUS_SCHEMA })
const done = new Set((status && status.done) || [])
log('Prep complete. Already DONE: [' + [...done].join(',') + ']' + (status && status.notes ? ' — ' + status.notes : ''))

const integrated = []
const skipped = []

for (let w = 0; w < WAVES.length; w++) {
  const waveTitle = 'Wave ' + (w + 1)
  phase(waveTitle)
  const ids = WAVES[w].filter(id => !done.has(id))
  if (!ids.length) { log(waveTitle + ': all phases already done — skipping'); continue }
  log(waveTitle + ': implementing phases [' + ids.join(',') + '] in parallel isolated worktrees')

  // PARALLEL implement + verify (each phase in its own worktree -> no collision)
  const prepared = (await parallel(ids.map(id => () => preparePhase(id, waveTitle)))).filter(Boolean)

  // SERIAL integrate (ascending phase order) -> no merge-time collision on hot files
  for (const p of prepared.sort((a, b) => a.phase - b.phase)) {
    if (!p.ready) {
      skipped.push({ phase: p.phase, reason: p.reason || 'failed-verification', blocking: p.blocking })
      log('SKIP Phase ' + p.phase + ' — not ready (' + (p.reason || 'verification') + '); branch+worktree left for review')
      continue
    }
    let r = null
    try {
      r = await agent(integratePrompt(p.phase), { label: 'integrate:p' + p.phase, phase: waveTitle, agentType: 'general-purpose', schema: INTEG_SCHEMA })
    } catch (e) {
      r = null
    }
    if (r && r.merged && r.fullSuitePassed && r.failCount === 0) {
      integrated.push({ phase: p.phase, pass: r.passCount, skip: r.skipCount })
      log('INTEGRATED Phase ' + p.phase + ' — suite ' + r.passCount + ' passed / ' + r.skipCount + ' skipped')
    } else {
      skipped.push({ phase: p.phase, reason: 'integration-failed', notes: r && r.notes })
      log('SKIP Phase ' + p.phase + ' — integration did not land cleanly; left for review')
    }
  }
}

const summary = {
  integrated: integrated.map(i => i.phase),
  skipped,
  finalSuite: integrated.length ? integrated[integrated.length - 1] : null,
  note: 'All work is on ' + INTEG + ' only — main was never touched. Skipped phases retain their branch+worktree for review.',
}
log('Roadmap v2 autopilot finished. Integrated: [' + summary.integrated.join(',') + ']  Skipped: [' + skipped.map(s => s.phase).join(',') + ']')
return summary
