---
version: 1
---

# Bug-hunter oracle policy

The exploratory bug-hunter drives the REAL app over its HTTP + WebSocket surface (the same
surface the deterministic self-play fuzzer drives) in an open-ended, LLM-guided way. This asset
is the POLICY the explorer/triage prompt embeds. It is DATA, versioned — NOT runtime
`knowledge/` (it must never reach the agent under test) and NOT Python `if/elif`.

## Two-tier bug definition

### 1. Deterministic oracle — AUTHORITATIVE (only this can fail a build)
The existing invariant battery (`tests/eval/app_driver.py`) runs after every action and after
every run. A non-empty return from any invariant is a REAL finding with NO false positives —
each asserts a proven structural truth:
- **crash / 5xx** — any `/api/*` response outside its allowed set; any unexpected `error` frame
  whose `kind != "protocol_error"`.
- **contract violation** — handshake frames missing/extra; a malformed frame not rejected as a
  `protocol_error` while the socket survives.
- **state corruption across chat-switch** (the historic bug class) — on-disk transcript AHEAD of
  memory; duplicate `in_flight_approvals`; a parked gate not persisted / not re-emitted on
  reconnect; an approval `request_id` shared across sessions; the synthetic pre-probe leaking
  into history or a sidebar title.

### 2. LLM oracle / triage — ADVISORY ONLY (NEVER fails a build)
After a run the explorer LLM reviews the trace + any suspicious-but-not-invariant states and
emits a triaged hypothesis. This is recorded in the `llm_triage` field of a finding ONLY. An
LLM hunch with NO deterministic invariant behind it MUST NOT flip a build red — it is a lead for
a human, guarded against LLM false-positive bug reports.

## Severity map (deterministic findings → severity)
Assigned from the invariant category:
- `state_corruption` → **high** (disk-ahead-of-memory, shared request_id, parked-gate loss,
  duplicate in-flight approvals — the historic, user-visible corruption class).
- `crash` / `5xx` → **high** (an unhandled server fault).
- `contract` → **medium** (a handshake / protocol-frame contract break; serious but bounded).
- `synthetic_leak` → **medium** (a synthetic pre-probe leaking into the rendered chat / title).
- anything LLM-only with no invariant → **info** (advisory; never gates).

A build FAILS only on a finding with `severity >= high` that is `deterministic: true`.

## Suspicion heuristics (for the LLM, advisory)
Flag for triage (NOT as a deterministic bug): a turn that ended without a `done` frame; a chat
whose title looks malformed; repeated identical errors; a namespace folder that lingers after a
delete. The LLM proposes a hypothesis + a minimal repro guess; the deterministic oracle decides
truth.

## Triage instructions (for the LLM)
Given the action trace + the run's suspicious states, respond with a SINGLE JSON object:
```
{ "triage": "<1-3 sentences: most likely explanation + whether it matches a known class>",
  "suspicions": ["<each suspicious-but-unproven observation>"] }
```
Be conservative: prefer "benign" when no invariant fired. Cite the action index when possible.

## Action-selection guidance (for the LLM explorer)
Choose the NEXT action to maximize the chance of surfacing a state-corruption bug: favor
interleaving `reconnect_midturn`, `switch_chat`, and `delete_namespace`/`delete_session` around
in-flight mutating turns (the chat-switch corruption class lives there). Return ONLY the chosen
action name + params as JSON; the deterministic `Player` executes it and the oracle checks it.
