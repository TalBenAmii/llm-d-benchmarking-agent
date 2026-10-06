# SDK-native engine design

Approved on 2026-07-14; implemented on 2026-07-19.

The Claude Agent SDK replaced the app's custom agent loop and context management. This
document records the design agreed before implementation and the findings from Phases 3-5.
The current engine is `app/agent/engine.py`; the old loop and provider layer have been removed.

## Why

The old context-management mechanisms reduced token usage but caused several problems.
History compaction and elision could remove established facts mid-session. The 6k tool-result
limit hid parts of long outputs, and document deduplication could point the model to content
that had already been removed. `MAX_STEPS=24` also paused long workflows, as observed in the
2026-07-13 showcase. The SDK and CLI provide auto-compaction, prompt caching, session
transcripts, and partial reads through the Max subscription, without raw-API billing.

## Approved decisions

- **SDK-only.** `anthropic_provider.py`, `provider.py`, and the old loop are deleted. The old implementation remains available in git history.
- **No tool-result clamp anywhere.** Results enter context whole; CLI auto-compaction is the bound.
  (If the Phase-4 cost baseline regresses >~20%, the fallback is one PostToolUse
  `updatedToolOutput` hook with a generous cap, subject to user approval.)
- **`max_turns=60`** replaces `MAX_STEPS=24`; the bridge maps `error_max_turns` to the same
  "reached the step limit; pausing." ERROR event.
- **Context chip** feeds from `client.get_context_usage()` + a `compacted` marker on
  `compact_boundary`; the char/4 estimator is removed.
- Connect-per-turn + `resume=sdk_session_id` (no persistent per-chat subprocess, no prewarm pool).
- `session.messages` remains a display-only copy (persist/share/title unchanged); it never feeds the model.
- All roughly 35 domain tools remain app-owned MCP tools, including `run_shell`. Native Bash
  and Read tools are disabled. The security model (command policy, classifier, gated-access + skill gates, env-scrubbed runner,
  SIMULATE ordering) survives untouched inside handlers.
- Approval gates stay inside handlers via `channel.request_approval` (auto-approve = commands only,
  never session_plan). `can_use_tool` is a thin gatekeeper: allow `mcp__benchtools__*`, deny rest.
- Lazy tool groups, doc dedup, catalog/env injection workarounds, and byte-stability machinery are
  deleted; the system prompt stays a custom stable string with `setting_sources=[]`.

## Phase-0 findings (CLI 2.1.209, SDK 0.2.110)

| Question | Verdict |
|---|---|
| V1: does a parked approval survive? (MCP handler held 15 min, `MCP_TOOL_TIMEOUT=86400000`) | **Yes:** result accepted, turn ended `success` |
| V2: resume past a dangling `tool_use` after interrupt mid-tool | **Yes** |
| V3: does `can_use_tool` context carry `tool_use_id`? | **Yes** (`ToolPermissionContext.tool_use_id`) |
| V4: session id across `resume` | **Stable** (same id re-issued) |
| Steer: mid-turn `client.query()` | **Silently dropped** → steers queue app-side and are sent as an immediate follow-up `query()` on the same session after the current `ResultMessage`; decline-open-gates is unchanged (gates live in our handlers) |

SDK protocol facts the engine relies on (verified in `tests/_sdk_fake.py`'s canary test):
`Query._handle_sdk_mcp_request` dispatches `tools/call` directly to in-process
`server.request_handlers` (no MCP handshake); `can_use_tool` allow-responses always carry
`updatedInput` (= the executed args); tool names are `mcp__<server>__<tool>`.

## Target architecture

- **`app/agent/engine.py`** (replaces `loop.py`): for each user turn, build `ClaudeAgentOptions` →
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
  effects → TOOL_RESULT full → `CARD_RESULT_TOOLS` capture → RESULTS_CARD → return the full result).
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
  `tests/flows/baselines/*.events.json`, containing normalized old-engine WS event streams for 6
  representative flows (plan-only, full deploy walk, decline+steer, safety refusal, error path,
  knowledge-heavy). Phase 4 diffs the new engine against these; token/usage fields are normalized
  out, event order and semantic payloads are pinned.
- Phases: 1 engine skeleton + bridge (behind a branch-only `AGENT_ENGINE` flag) → 2 feature parity
  (lifecycle, steer, cancel, SIMULATE, picker, preamble, compaction surfacing, usage) → 3 flow-
  harness migration (both engines parametrized) → 4 verification (corpus dual-run, WS parity diff,
  resume/restart battery, cost check; one user-approved live smoke test) → 5 cutover + deletion + docs.
  The flag never ships; the old path is deleted before merge.

## Risks being tracked

Approval-park longevity beyond 15 min (V1 tested one interval, not days; check in Phase 4's
restart battery) · compaction dropping injected catalog/env context (durable facts live in
state.json, not the conversation) · cost regression from unclamped results (baseline-gated) ·
Transport ABC drift (version pin + canary test) · subprocess leaks (connect-per-turn + finally-
disconnect + leak canary).


## Implementation findings (Phases 3-5, 2026-07-16 → 2026-07-19)

- **Event-order parity needed one engine-side fix**: the SDK dispatches a tool while its
  introducing assistant message still sits in the consumer queue, so `tool_call` could precede
  the text bubble. Fixed in-engine (`LiveTurn.wait_mirrored`): tool execution waits until the
  consumer copied and emitted the introducing message. This preserves the original WS event order.
- **Wire parity (Phase 4)**: all 6 baseline flows are byte-identical to the old-engine pins
  after dropping `usage` events, the one accepted difference (old: usage per LLM call; new: one
  per SDK response). Both baseline sets stay committed (`tests/flows/baselines/`), with a guard
  test diffing them modulo usage.
- **Cost gate (Phase 4-live, sonnet-5/effort high): PASSED at 0.344** weighted per-token cost
  ratio new/old (gate was ≤1.2). The old engine paid cache writes per session; the CLI prefix is
  a shared cache read. Raw context ~2× larger but ~10× cheaper per token; turn-2 steady state
  ~34% cheaper. The PostToolUse-clamp fallback was never needed.
- **Resume battery**: CLI `resume=` survives server restarts; a dead/unknown session id falls
  back to a fresh SDK session seeded from the `session.messages` mirror. This uses plain-text narration because
  the CLI rejects synthetic tool_use/tool_result blocks.
- **Stream watchdog** (`agent_stream_watchdog_s`, 900s default): no-progress stall → interrupt +
  clean ERROR; tool execution is exempt so parked approval gates can wait indefinitely. The live
  eval reuses it as its per-flow fail-fast (`LLM_EVAL_CALL_TIMEOUT`).
- **Deleted at cutover** (Phase 5): `loop.py`, `context_mgmt.py`, `provider.py`,
  `agent_sdk_provider.py`, `anthropic_provider.py`, `tool_loader.py`, lazy tool groups +
  `load_tools`, doc dedup, budget clamps (except the 4k env-preamble clamp, now in `engine.py`),
  the char/4 context estimator, and the `AGENT_ENGINE` flag. Tests script the engine through
  `tests/_scripted.py` (AssistantTurn scripts → FakeTransport) instead of a fake provider.
