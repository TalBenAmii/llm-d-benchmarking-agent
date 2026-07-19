# SDK-native engine — design (approved 2026-07-14) — **IMPLEMENTED 2026-07-19**

Replace the app's own agent loop and hand-rolled context management with the Claude Agent SDK
running the loop natively, the way Claude Code does. This doc was the implementation contract for
the refactor (written after Phase 0, before Phase 1); the cutover is now complete — the engine is
`app/agent/engine.py`, the old loop/provider layer is deleted, and the "Implementation findings"
section at the bottom records what Phases 3–5 measured.

## Why

Every context-management mechanism we built exists to save tokens, and each one damages answer
quality: history compaction/elision loses established facts mid-session; the 6k tool-result clamp
blinds the model to long output; the doc-dedup back-reference dead-loops after elision (the agent is
told content "is above" when it was elided); `MAX_STEPS=24` pauses long flows (hit in the 2026-07-13
showcase). The SDK/CLI already solves all of this (auto-compaction, prompt caching, session
transcripts, partial reads) — on the Max subscription, with no raw-API billing.

## Decisions (user-approved — do not re-litigate)

- **SDK-only.** `anthropic_provider.py`, `provider.py`, and the old loop are deleted. Rollback = git.
- **No tool-result clamp anywhere.** Results enter context whole; CLI auto-compaction is the bound.
  (If the Phase-4 cost baseline regresses >~20%, the fallback is ONE PostToolUse
  `updatedToolOutput` hook with a generous cap — a user decision at that point.)
- **`max_turns=60`** replaces `MAX_STEPS=24`; the bridge maps `error_max_turns` to the same
  "reached the step limit; pausing." ERROR event.
- **Context chip** feeds from `client.get_context_usage()` + a `compacted` marker on
  `compact_boundary`; the char/4 estimator dies.
- Connect-per-turn + `resume=sdk_session_id` (no persistent per-chat subprocess, no prewarm pool).
- `session.messages` stays a render MIRROR (persist/share/title unchanged); it never feeds the model.
- All ~35 domain tools stay OUR MCP tools (incl. `run_shell`) — no native Bash/Read. The whole
  security model (command policy, classifier, gated-access + skill gates, env-scrubbed runner,
  SIMULATE ordering) survives untouched inside handlers.
- Approval gates stay INSIDE handlers via `channel.request_approval` (auto-approve = commands only,
  never session_plan). `can_use_tool` is a thin gatekeeper: allow `mcp__benchtools__*`, deny rest.
- Lazy tool groups, doc dedup, catalog/env injection contortions, and byte-stability machinery are
  deleted; the system prompt stays a custom stable string with `setting_sources=[]`.

## Phase-0 spike verdicts (CLI 2.1.209, SDK 0.2.110 — measured, not assumed)

| Question | Verdict |
|---|---|
| V1: does a parked approval survive? (MCP handler held 15 min, `MCP_TOOL_TIMEOUT=86400000`) | **Yes** — result accepted, turn ended `success` |
| V2: resume past a dangling `tool_use` after interrupt mid-tool | **Yes** |
| V3: does `can_use_tool` context carry `tool_use_id`? | **Yes** (`ToolPermissionContext.tool_use_id`) |
| V4: session id across `resume` | **Stable** (same id re-issued) |
| Steer: mid-turn `client.query()` | **Silently dropped** → steers queue app-side and are sent as an immediate follow-up `query()` on the same session after the current `ResultMessage`; decline-open-gates is unchanged (gates live in our handlers) |

SDK protocol facts the engine relies on (verified in `tests/_sdk_fake.py`'s canary test):
`Query._handle_sdk_mcp_request` dispatches `tools/call` directly to in-process
`server.request_handlers` (no MCP handshake); `can_use_tool` allow-responses always carry
`updatedInput` (= the executed args); tool names are `mcp__<server>__<tool>`.

## Target architecture

- **`app/agent/engine.py`** (replaces `loop.py`): per user turn — build `ClaudeAgentOptions` →
  connect (`resume=session.sdk_session_id`) → send preamble (first turn of a session only: the same
  bracket-tagged env-preprobe + catalog-brief user messages as today) + the user text → consume the
  stream, translating to the existing WS events (`assistant_delta` from StreamEvents,
  `assistant_text`, `usage`, `done`; `tool_call`/`tool_result`/`command`/`output`/`results_card`
  come from the MCP wrapper) and mirroring into `session.messages` → persist `sdk_session_id` from
  `ResultMessage` → `finally: interrupt() + disconnect()`. Holds `{session_id: LiveTurn}` for
  steer/cancel; checks the abandoned-turn predicate at stream-message boundaries.
- **`app/tools/mcp_server.py`**: `create_sdk_mcp_server("benchtools")` from the registry; one
  wrapper per ToolSpec = everything `loop.py` did per tool call (TOOL_CALL emit → `dispatch()` with
  the verbatim ApprovalRejected/ToolError except-ladder → duration record → plan/namespace side
  effects → TOOL_RESULT full → `CARD_RESULT_TOOLS` capture → RESULTS_CARD → return the FULL result).
  A per-session `asyncio.Lock` serializes tool execution (ToolContext assumes sequential dispatch).
- **Options**: `tools=[]`, `allowed_tools=[]` (nothing skips `can_use_tool`),
  `permission_mode="default"`, `setting_sources=[]`, `include_partial_messages=True`,
  `cwd=<workspace>` (stable transcript home), env blanks `ANTHROPIC_API_KEY`/`AUTH_TOKEN` (Max-plan
  billing guard) + `MCP_TOOL_TIMEOUT`/`MCP_TIMEOUT` very large, model/effort from the per-session
  override.
- Resume failure (transcript GC'd) → fresh SDK session, seeded once from the mirror.
- `suggest_next_steps` terminality = prompt rule + engine suppresses trailing assistant text.

## What gets deleted (≈ −1,650 LOC net)

`app/agent/loop.py` · `app/agent/context_mgmt.py` (compaction, elision, clamp, estimator) ·
`app/llm/agent_sdk_provider.py` (the deny-all inversion + prewarm pool) · `app/llm/anthropic_provider.py`
· `app/llm/provider.py` (constants → `model_catalog.py`) · `app/tools/tool_loader.py` + the
registry's `_TOOL_GROUPS`/`STARTER_KIT`/`loaded=` filter + `GROUP_CATALOG_NOTE` · the
`ctx.fetched_docs` dedup and `_annotate_budget_overflow` in `knowledge_access.py` ·
`session.loaded_groups` (+ migration). The knowledge 6KB size rule becomes soft editorial guidance.

## Test & verification strategy

- **Hermetic seam = `tests/_sdk_fake.py` FakeTransport** (committed, 8 conformance tests + a
  protocol canary): scripts drive `ClaudeSDKClient` through the SDK's real parsing, permission
  bridge, and real in-process MCP handlers. The golden flow corpus (`tests/flows/flows.py`) survives
  as data; the harness re-targets this seam in Phase 3.
- **Parity baselines** (committed): `scripts/eval/capture_ws_baseline.py` +
  `tests/flows/baselines/*.events.json` — normalized old-engine WS event streams for 6
  representative flows (plan-only, full deploy walk, decline+steer, safety refusal, error path,
  knowledge-heavy). Phase 4 diffs the new engine against these; token/usage fields are normalized
  out, event order and semantic payloads are pinned.
- Phases: 1 engine skeleton + bridge (behind a branch-only `AGENT_ENGINE` flag) → 2 feature parity
  (lifecycle, steer, cancel, SIMULATE, picker, preamble, compaction surfacing, usage) → 3 flow-
  harness migration (both engines parametrized) → 4 verification (corpus dual-run, WS parity diff,
  resume/restart battery, cost check; ONE user-approved live smoke) → 5 cutover + deletion + docs.
  The flag never ships; the old path is deleted before merge.

## Risks being tracked

Approval-park longevity beyond 15 min (V1 tested one interval, not days — watch in Phase 4's
restart battery) · compaction dropping injected catalog/env context (durable facts live in
state.json, not the conversation) · cost regression from unclamped results (baseline-gated) ·
Transport ABC drift (version pin + canary test) · subprocess leaks (connect-per-turn + finally-
disconnect + leak canary).


## Implementation findings (Phases 3–5, 2026-07-16 → 2026-07-19)

- **Event-order parity needed one engine-side fix**: the SDK dispatches a tool while its
  introducing assistant message still sits in the consumer queue, so `tool_call` could precede
  the text bubble. Fixed in-engine (`LiveTurn.wait_mirrored`): tool execution waits until the
  consumer mirrored+emitted the introducing message — the WS transcript keeps the old order.
- **Wire parity (Phase 4)**: all 6 baseline flows are byte-identical to the old-engine pins
  after dropping `usage` events — the ONE adjudicated diff (old: usage per LLM call; new: one
  per SDK response). Both baseline sets stay committed (`tests/flows/baselines/`), with a guard
  test diffing them modulo usage.
- **Cost gate (Phase 4-live, sonnet-5/effort high): PASSED at 0.344** weighted per-token cost
  ratio new/old (gate was ≤1.2). The old engine paid cache WRITES per session; the CLI prefix is
  a shared cache READ. Raw context ~2× larger but ~10× cheaper per token; turn-2 steady state
  ~34% cheaper. The PostToolUse-clamp fallback was never needed.
- **Resume battery**: CLI `resume=` survives server restarts; a dead/unknown session id falls
  back to a fresh SDK session seeded from the `session.messages` mirror (plain-text narration —
  the CLI rejects synthetic tool_use/tool_result blocks).
- **Stream watchdog** (`agent_stream_watchdog_s`, 900s default): no-progress stall → interrupt +
  clean ERROR; tool execution is exempt so parked approval gates can wait indefinitely. The live
  eval reuses it as its per-flow fail-fast (`LLM_EVAL_CALL_TIMEOUT`).
- **Deleted at cutover** (Phase 5): `loop.py`, `context_mgmt.py`, `provider.py`,
  `agent_sdk_provider.py`, `anthropic_provider.py`, `tool_loader.py`, lazy tool groups +
  `load_tools`, doc dedup, budget clamps (except the 4k env-preamble clamp, now in `engine.py`),
  the char/4 context estimator, and the `AGENT_ENGINE` flag. Tests script the engine through
  `tests/_scripted.py` (AssistantTurn scripts → FakeTransport) instead of a fake provider.
