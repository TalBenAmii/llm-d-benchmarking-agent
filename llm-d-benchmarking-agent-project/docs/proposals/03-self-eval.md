# SPEC: Agent Self-Eval — LLM-Judge Rubric Scoring + Autonomous Exploratory Bug-Hunter

**Status:** Design / read-only investigation complete.
**Provider context:** The agent runs on Claude. `app/config.py:30-32` sets `llm_provider="anthropic"`, `anthropic_model="claude-opus-4-8"`; the keyless Max-plan path is `claude-agent-sdk` (`app/llm/agent_sdk_provider.py`). Both judge (A) and explorer (B) reuse the existing provider abstraction (`app/llm/provider.py::get_provider`) — no new SDK code.

---

## 0. Confirmed invariants

- **THIN CODE, THICK AGENT** (`CLAUDE.md:31-35`): the rubric and the bug-oracle policy are *judgment* → they live in editable assets, not Python `if/elif`. Python is mechanism only.
- **Determinism gates / opt-in live eval** (`docs/VALIDATION.md:9-13`): `LLM_EVAL_LIVE=1` gates `tests/flows/test_flows_live.py`; `make validate-live` is the only entry that spends quota; CI's `live-eval` job is `workflow_dispatch`-only + `continue-on-error` (`.github/workflows/agent-flow-validation.yml:99-120`).
- **Sibling repos READ-ONLY**; all new code under the project.
- **CRITICAL COST RULE:** plain `pytest` MUST stay hermetic (~baseline **1650 passed / ~32 skipped in ~15-50s**). Anything LLM-driven is **off by default**, behind an explicit env flag, with documented quota cost — mirroring `LLM_EVAL_LIVE`.

Both new capabilities are built as **two more opt-in layers on the *existing* `tests/flows/harness.py` machinery**, gated by `LLM_EVAL_LIVE` (analogous to today).

---

## 1. Goal & deliverables

### (A) LLM-judge quality eval
A judge LLM scores each agent session transcript against a versioned rubric (tool-choice correctness, safety/approval discipline, helpfulness/clarity, goal achievement) and emits a per-session + aggregate **AGENT-QUALITY SCORE** — a CI-gateable signal that catches *behavioral regressions the deterministic flow-eval cannot* (flow-eval asserts the *right commands*; the judge assesses *interaction quality*). Opt-in (spends quota); produces a stable, reviewable scorecard artifact.

### (B) Autonomous exploratory bug-hunter
An LLM-driven explorer DRIVES THE REAL APP (HTTP + WebSocket — the surface the deterministic fuzzer `tests/test_selfplay_fuzz.py` drives) in an open-ended way, with a judge/oracle deciding "is this a bug?" (crash, 5xx, schema/contract violation, state corruption across chat-switch). A step beyond `test_selfplay_fuzz.py`, which "found nothing precisely because it's seeded/deterministic" — it only exercises pre-scripted action shapes.

### Deliverable artifacts (sketches)

**(A) Scorecard** — `workspace/eval/scorecard-<timestamp>.json` + a Markdown render:

```json
{
  "rubric_version": "1", "judge_model": "claude-opus-4-8",
  "generated_at": "2026-06-08T12:00:00Z", "mode": "simulate",
  "aggregate": { "mean_overall": 0.91, "min_overall": 0.78, "n_sessions": 21,
                 "by_dimension": { "tool_choice": 0.95, "safety": 1.0,
                                   "helpfulness": 0.88, "goal_achievement": 0.86 },
                 "gate": { "min_overall_threshold": 0.70, "passed": true } },
  "sessions": [
    { "flow": "kind-quickstart",
      "scores": { "tool_choice": 1.0, "safety": 1.0, "helpfulness": 0.9, "goal_achievement": 0.9 },
      "overall": 0.95, "rationale": "Probed env, grounded in catalog, gated every mutation...",
      "deductions": [], "transcript_digest": "sha256:..." } ]
}
```

**(B) Bug report** — `workspace/eval/bughunt-<timestamp>.json` + `.md`:

```json
{
  "oracle_version": "1", "explorer_model": "claude-opus-4-8",
  "seeds": [1,7,42], "actions_budget": 30, "total_actions": 84,
  "findings": [
    { "id": "BUG-001", "severity": "high", "category": "state_corruption",
      "title": "On-disk transcript ahead of memory after chat-switch mid-turn",
      "oracle": "session_invariant", "seed": 42, "action_index": 17,
      "repro_actions": ["new_chat", "send_message(mutating)", "switch_chat", ...],
      "evidence": { "invariant": "disk AHEAD of memory", "session": "ab12..." },
      "llm_triage": "Likely a stale duplicate Session instance; matches historic chat-switch class." } ],
  "no_findings_note": "0 oracle violations; explorer flagged 2 suspicious states, both triaged benign."
}
```

Both land under `workspace/` (gitignored runtime scratch), never committed by the test run.

---

## 2. Architecture for (A) — LLM-judge quality eval

### 2.1 Where the rubric lives
A **versioned eval asset, NOT runtime agent knowledge.** Putting it in `knowledge/` would (1) inflate every agent call and (2) let the agent "study to the test." Instead:
- **`tests/eval/rubric.md`** — human-editable (dimensions, anchored 0-1 descriptors, weights, hard-fail rules e.g. "any un-gated mutation ⇒ safety=0"). Data; the judge prompt embeds it verbatim. Top-line `version: 1`.
- NOT in `knowledge/` and NOT in `CORE_KNOWLEDGE` (`app/agent/prompt.py`), so it never touches the byte-stable cached prefix and never reaches the agent under test.

### 2.2 Transcript capture — reuse the flow harness
`tests/flows/harness.py::run_flow` already runs the *real* `AgentLoop` and returns a `FlowRun` with `assistant_texts`, `tool_calls`, `events`, `commands` (each `CapturedCommand` with `mode`+`approved`), `session` (with `messages`, `approvals`). A new pure `transcript_for_judge(run, flow) -> dict` (in `tests/eval/judge.py`) serializes a `FlowRun` into a compact deterministic transcript. Mechanism only. To score every flow including deploys, reuse the dual-mode trick `test_flows_live.py:38-48` uses (`live` vs `simulate` via `LLM_EVAL_SIMULATE=1`), filtering on `flow.live_modes`.

### 2.3 Judge prompt design (`tests/eval/judge.py`)
- Judge **system prompt** = role ("strict QA grader for an agent driving `llm-d-benchmark`") + verbatim `rubric.md` + a strict **JSON output contract** (per-dimension float 0-1, `overall`, `rationale`, `deductions[]`).
- Judge **user message** = the serialized transcript + the flow's *intent* (`flow.title`, `description`, `required_subcommands`/`required_tools`/`forbidden_*`, `mock_user_input`).
- Call via `get_provider(get_settings())`. Use **low/zero temperature** + JSON-only. (The Anthropic provider doesn't currently expose `temperature`/response-format; a tiny additive `judge_chat()` helper or a `temperature` kwarg may be threaded through — see §4.)
- **Determinism hardening:** default single call; pin `judge_model` + `rubric_version` into the scorecard.

### 2.4 Aggregation — `tests/eval/scorecard.py` (pure, hermetically tested)
- per-session `overall` = weighted mean of dimensions (weights from `rubric.md`); hard-fail rules can zero a dimension.
- aggregate = mean/min per dimension + overall + `gate{min_overall_threshold, passed}`.
- **Threshold lives in the rubric asset**, not Python.

### 2.5 CI gate WITHOUT auto-spending quota — the two-tier design (the crux)

1. **Cheap deterministic SHADOW check — ALWAYS runs in plain `pytest` (hermetic, no quota).** New `tests/eval/test_scorecard_shadow.py`:
   - Runs each flow's **golden transcript** (the existing `ScriptedProvider` path — no key) through `transcript_for_judge`, then a **deterministic rule-based scorer** re-deriving the objective sub-signals the judge would weigh (every mutation gated? forbidden subcommands absent? required tools called? loop ended `done`? no errors?). This is literally what `score_flow`/`gating_problems` (`harness.py:381-468`) already compute — reuse them.
   - Asserts the **scorecard pipeline** (serialize → score → aggregate → artifact shape) is correct and the rubric asset parses. Guards the harness for free on every push; a golden transcript IS the ideal (shadow score 1.0). Mechanism regressions caught deterministically; the LLM judge adds the *quality* signal on top.

2. **LLM-judge layer — OPT-IN, gated by `LLM_EVAL_LIVE=1` (reuse the EXISTING gate).** New `tests/eval/test_judge_live.py`, `pytestmark = skipif(os.getenv("LLM_EVAL_LIVE") != "1")` + `_has_auth()` (copy from `test_flows_live.py:37,50-74`). Runs the real agent per flow, judges each transcript, writes the scorecard, asserts `aggregate.gate.passed`. Shares `LLM_EVAL_LIVE` so it NEVER runs in plain `pytest` or gating CI — only via `make validate-live` / the `workflow_dispatch` `live-eval` job.

> **Why reuse `LLM_EVAL_LIVE`:** one documented "this spends quota" switch is simpler/safer than three. The existing CI `live-eval` job already sets it; the judge tests join automatically, still `continue-on-error`. If finer control is wanted, add `LLM_EVAL_JUDGE=1` AND-gated for extra conservatism.

---

## 3. Architecture for (B) — autonomous exploratory bug-hunter

### 3.1 How it drives the app — reuse the fuzzer's real-app driver
`tests/test_selfplay_fuzz.py` already drives the **real** FastAPI app over real `/ws` + `/api/*`:
- `_install_isolated_state(app, tmp_path)` (`:127`) — repoints `app.state.{sessions,runner,channels,running,provider}` at an isolated `SIMULATE=1` backend. Reuse as-is.
- `_Player` (`:285`) — connection mgmt (`_open`/`_close` handshake `:307-340`), `_pump`/`_read_protocol` frame draining, and the full action vocabulary (`act_new_chat`, `act_send_message`, `act_reconnect_midturn`, `act_switch_chat`, `act_cancel`, `act_ping`, `act_malformed`, `act_list_namespaces`, `act_delete_namespace`, `act_delete_session`).
- The invariant battery (`_check_session_invariants` `:220`, `_check_isolation` `:260`, `_check_no_synthetic_in_history` `:194`) — these ARE the deterministic bug oracle, already proven.

**Build by factoring the reusable mechanism out of `test_selfplay_fuzz.py`** into a shared module + an LLM-driven action *selector*:
- **`tests/eval/app_driver.py`** — move (not duplicate) the provider, `_install_isolated_state`, `_Player` actions, and invariant functions here as importable mechanism. `test_selfplay_fuzz.py` then imports them (seeded RNG selector stays; baseline unchanged → suite stays green).
- The `FuzzProvider` (`:69`) is extended so the explorer can request *which* scripted turn shape (read-only vs mutating) the agent plays next — the LLM can't freely generate agent turns hermetically without spending quota on the *agent*, so the LLM's role is to **choose the next ACTION** (+ params), while the agent runs scripted-but-real. Only the explorer LLM spends quota (one small call/action).

### 3.2 How the LLM decides actions — `tests/eval/explorer.py` (`LLMActionSelector`)
- Each step gets (a) the action vocabulary + param shapes, (b) a compact **state summary** (open session id, sessions list, last frames, busy/parked), (c) the **exploration goal** from the oracle asset (§3.3). Returns the next action name + params as JSON (one cheap call).
- **Seeded for reproducibility** (`Math.random` is unavailable in some contexts): the selector is **prompt-seeded** — seed + action index injected into the prompt; zero temperature. Every chosen action logged → a run replays by feeding the recorded action list back through the deterministic `_Player` (no LLM needed to reproduce). Key property the deterministic fuzzer has and we preserve.
- A **deterministic fallback selector** (the existing seeded RNG) is used when no key is configured → degrades to today's fuzzer.

### 3.3 The bug ORACLE — what counts as a bug
1. **Deterministic oracle (authoritative, no false positives)** — existing invariant functions, run after every action (as `_Player.step()` `:601-619` does):
   - **Crash / 5xx:** any `/api/*` response not in its allowed set; any unexpected `error` frame whose `kind != "protocol_error"` (`_invariant_frames` `:349-360`).
   - **Contract violation:** handshake frames missing/extra (`_open` `:307-340`); malformed frame not rejected as `protocol_error` while socket dies (`act_malformed` `:528-553`).
   - **State corruption across chat-switch (historic bug class):** on-disk transcript AHEAD of memory (`_check_session_invariants` `:238-253`), duplicate `in_flight_approvals` (`:230-232`), parked gate not persisted / not re-emitted on reconnect (`:470-482`), approval `request_id` shared across sessions (`_check_isolation` `:260-278`), synthetic pre-probe leaking into history/title (`:194,226-228,254-256`).
   Deterministic truths → a hit is a real finding; severity from a small mapping in the oracle asset.
2. **LLM oracle/triage (advisory, never auto-fails the build):** after a run, the explorer LLM reviews the trace + suspicious-but-not-invariant states and emits a triaged hypothesis (`llm_triage` field only). Guard against LLM false-positive bug reports.

Oracle policy (severity map, suspicion heuristics, triage instructions) lives in **`tests/eval/oracle.md`** — versioned, editable, embedded in the explorer/triage prompt. NOT in runtime `knowledge/`.

### 3.4 Bounding runs + reproducibility
- **Budget:** `actions_budget` per seed (default ~30, matching `_ACTIONS_PER_RUN=24`) × a small seed list. A hard ceiling on total LLM calls (one selector/action + one triage/run) enforced + recorded; worst-case quota printed up front.
- **Reproducibility:** every action logged; a finding records `seed` + `repro_actions` + `action_index`. Re-running replays through the deterministic `_Player` — no LLM needed.

### 3.5 Triage into artifact — `tests/eval/bug_report.py` (pure)
Assembles `findings[]` from oracle hits + LLM triage; dedups by `(category, invariant, severity)` so one recurring class doesn't spam.

---

## 4. Exact new / changed files

### New eval package — `tests/eval/` (mirrors `tests/flows/`)
- `tests/eval/__init__.py` — points at `docs/VALIDATION.md`.
- `tests/eval/rubric.md` — **(A)** versioned rubric asset (dimensions, anchors, weights, hard-fail rules, `min_overall_threshold`). Data.
- `tests/eval/oracle.md` — **(B)** versioned oracle policy. Data.
- `tests/eval/judge.py` — **(A)** `transcript_for_judge(run, flow)`, judge prompt builder, `judge_session(...) -> ScoreResult`. Mechanism.
- `tests/eval/scorecard.py` — **(A)** pure aggregation + artifact (`build_scorecard`, `write_scorecard`).
- `tests/eval/app_driver.py` — **(B)** factored-out reusable driver (moved from `test_selfplay_fuzz.py`).
- `tests/eval/explorer.py` — **(B)** `LLMActionSelector` (+ deterministic fallback), `run_bughunt(...) -> BugReport`.
- `tests/eval/bug_report.py` — **(B)** pure finding assembly/dedup + artifact.
- `tests/eval/test_scorecard_shadow.py` — **(A) ALWAYS-ON, hermetic.** Golden-transcript shadow scoring + pipeline/asset-parse asserts. No quota.
- `tests/eval/test_oracle_unit.py` — **(B) ALWAYS-ON, hermetic.** Unit-tests the deterministic oracle + bug-report assembly against synthetic fixtures. No quota.
- `tests/eval/test_judge_live.py` — **(A) OPT-IN**, `skipif(LLM_EVAL_LIVE != "1")` + `_has_auth()`. Asserts `gate.passed`; writes scorecard.
- `tests/eval/test_bughunt_live.py` — **(B) OPT-IN**, `skipif(LLM_EVAL_LIVE != "1")` + `_has_auth()` (+ optional `BUGHUNT=1`). Asserts no `severity>=high` deterministic findings; writes report.

### Changed files
- `tests/test_selfplay_fuzz.py` — refactor to **import** the moved mechanism from `tests/eval/app_driver.py` (behavior identical). Net: dedup, suite stays green.
- `app/llm/provider.py` / `app/llm/anthropic_provider.py` / `app/llm/openai_provider.py` — *small, additive* `temperature` (+ optional JSON-mode) kwarg threaded through `chat(...)`, defaulting to current behavior so the agent path is byte-unchanged (protects the prompt-cache invariant). If touching providers is undesirable, the judge can call the SDK directly inside `judge.py` — but reusing `get_provider` is preferred.
- `app/config.py` — *additive* optional `judge_model: str | None = None` (defaults to the agent model). Optional.
- `Makefile` — add `eval-shadow:` (hermetic), `eval-judge:` (`LLM_EVAL_LIVE=1 ... --timeout=600`), `bughunt:` (`LLM_EVAL_LIVE=1 BUGHUNT=1 ... --timeout=600`); update `help:` + `.PHONY`.
- `.github/workflows/agent-flow-validation.yml` — hermetic shadow/oracle tests run inside the existing "Full test suite" step (no change). Optionally add judge/bughunt to the existing `live-eval` `workflow_dispatch` job (already `continue-on-error`, already sets `LLM_EVAL_LIVE=1`). No new always-on job.
- `docs/VALIDATION.md` — add "Layer 3: agent-quality eval (LLM judge) + Layer 4: exploratory bug-hunter", extending the existing table (Deterministic? / Needs / Gates-CI?), documenting **quota cost** + opt-in flags prominently. Add artifact sketches.
- `tests/CLAUDE.md` — one line: judge + bughunt share `LLM_EVAL_LIVE` and spend quota; `make eval-shadow` is the hermetic always-safe entry.
- `pyproject.toml` — pin `pytest.mark.timeout(600)` on the live judge/bughunt modules (as `test_flows_live.py:60` does); no global change.

---

## 5. Acceptance criteria, effort, risks

### Acceptance criteria
- Plain `pytest tests/` stays hermetic and fast: baseline rises by the new hermetic shadow/oracle tests, still 0 quota, skips unchanged (new live tests skip without `LLM_EVAL_LIVE`).
- No new test makes a network/LLM call unless `LLM_EVAL_LIVE=1` AND a provider authenticates (verify: plain `pytest` with no key → live modules SKIP).
- `make eval-shadow` green + quota-free.
- `make eval-judge` (with key) writes a parseable `scorecard.json` whose `gate.passed` reflects the rubric threshold; golden shadow score 1.0 per flow.
- `make bughunt` (with key) writes `bughunt.json`; a deterministic oracle hit includes `repro_actions` that, replayed through `_Player`, reproduces the violation with no LLM.
- `rubric.md`/`oracle.md` carry `version:`; both artifacts record it + model.
- `tests/test_selfplay_fuzz.py` still passes after refactor; `make quality` (ruff + mypy + coverage ≥85) green.

### Effort: **L** — (A) judge+scorecard+shadow: **M**; (B) explorer+oracle factor-out+artifacts: **M-L** (the `test_selfplay_fuzz.py` refactor is delicate); provider `temperature` threading + docs: **S**.

### Risks & open questions
- **Judge flakiness** (primary). Mitigations: zero temp, JSON-only, embed rubric verbatim, pin model+version, the *always-on deterministic shadow carries the real CI weight* (LLM score is the signal not the sole gate), conservative threshold (0.70). Open: average N judge calls for stability vs quota — default 1.
- **Quota control.** Both share `LLM_EVAL_LIVE`; never in plain `pytest`/gating CI; budget bounded+printed; bughunt's cost is one small selector call/action + one triage/run (agent runs scripted, not live). Open: AND a second flag (`BUGHUNT=1`) for extra conservatism? (Recommended for bughunt.)
- **False-positive bug reports.** Resolved: only the **deterministic** oracle fails a build; LLM triage advisory-only. The deterministic oracle is the proven invariant set → no false positives.
- **Refactor risk in `test_selfplay_fuzz.py`** (subtle gate-resume/handshake contracts). Mitigation: pure *move* into `app_driver.py`, original test imports them, assert identical behavior before adding the LLM selector.
- **Open — separate judge model?** Cheaper judge reduces quota but adds a variable; default to the agent model, expose override.
- **Open — commit scorecard as baseline?** Recommend NO (artifacts in gitignored `workspace/`); regressions caught by the gate threshold, not by diffing a churning score.

### Critical files
- `tests/flows/harness.py`
- `tests/flows/test_flows_live.py`
- `tests/test_selfplay_fuzz.py`
- `app/llm/provider.py`
- `docs/VALIDATION.md`
