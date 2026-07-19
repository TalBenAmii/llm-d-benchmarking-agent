# app/agent/ — the SDK-native engine + system-prompt assembly

`engine.py` is THE agent engine: the Claude Agent SDK/CLI runs the model→tool→model loop
natively, and the engine bridges its stream onto the app's WS event contract (same events,
approvals, steer, persistence as always). `prompt.py` assembles the system prompt. Decision
logic lives in `knowledge/` and the model's reasoning — **never** in `if/elif` here.

## ⚠️ Headline invariant: the system-prompt prefix is BYTE-STABLE and prompt-cached
`build_system_prompt()` (in `prompt.py`) must return a **byte-identical** prefix across turns
so the CLI's prompt cache keeps hitting. The cached prefix = `ROLE` + `HARD_RULES` + inlined
`CORE_KNOWLEDGE` + on-demand knowledge index + `CATALOG_POINTER` (+ config-stable `SIMULATE_NOTE`).

**Anything that varies per turn must NOT go in the prefix.** The live catalog snapshot and the
environment pre-probe are injected as **synthetic user messages** once per session (`engine.py`
`_first_query` + `catalog_brief_message` in `prompt.py`), precisely so they don't
mutate the cached prefix. `tests/agent/test_prompt_stability.py` enforces byte-stability +
"catalog body never in the prefix" + "catalog injected exactly once".

## How the engine works (the non-obvious parts)
- **One CLI connection per user turn**, resumed via the persisted `session.sdk_session_id`
  (`ClaudeAgentOptions(resume=...)`); a dead/unknown id falls back to a fresh SDK session seeded
  from the `session.messages` mirror (`_mirror_replay_text` + `app/llm/sdk_options.py` renderers).
- **Tools = in-process MCP** (`app/tools/mcp_server.py`): the wrapper emits `tool_call`/`tool_result`,
  runs `registry.dispatch()` (schema gate intact), handles approvals, durations, cards. The engine's
  `can_use_tool` gatekeeper stashes the `tool_use_id`; `LiveTurn.wait_mirrored` keeps the WS order
  (text bubble, then tool row).
- **No result clamping**: tool results enter the model context whole — CLI auto-compaction is the
  bound. The ONE surviving clamp is the env-preamble's 4k `clamp_tool_result_content` (in `engine.py`).
- **Steer** (`engine.steer()`): a mid-turn user message is injected into the live SDK turn;
  between turns it falls back to `ctx.steer_messages` (drained into the next turn's message).
- **Stream watchdog**: no stream progress for `agent_stream_watchdog_s` (900s default; `<=0`
  disables; tool execution exempt — parked gates can wait forever) → interrupt + clean ERROR.
- **`MAX_TURNS = 60`** maps the CLI's `error_max_turns` to the "step limit; pausing" error.

## Other local invariants
- **CORE_KNOWLEDGE inlining** (`prompt.py`): only the guides named in that tuple are inlined
  verbatim; everything else is indexed and pulled via `read_knowledge("<topic>")`. Adding a file
  to CORE inflates every call — see `knowledge/CLAUDE.md` for the cost rule.
- **One-shot session state** (`session.py`): `catalog_injected` + `prewarmed` are **persisted**
  (a resumed chat must not re-inject the catalog / env pre-probe); `env_snapshot` and the model
  picker's `model_override`/`effort_override` are **runtime-only** (a resume re-probes fresh /
  resets to the configured model).

## Key files
- `engine.py` — `SdkNativeEngine`: options assembly, the stream→WS bridge, LiveTurn registry (steer/interrupt/mirror ordering), resume fallback, usage/context accounting, the env-preamble clamp.
- `prompt.py` — byte-stable cached prefix + the per-session catalog/env synthetic messages.
- `session.py` — per-session state, persistence, one-shot flags, title derivation.
- `cards.py` — deterministic, knowledge/data-sourced UI content (mechanism only): the welcome card, the post-run results card, and the `suggestions.yaml` start-of-chat chips (data only; deliberately outside `knowledge/`).
- `channel.py` — per-session turn↔socket link: turns survive disconnects; buffers/replays live events + pending approvals.
- `events.py` — WS event-type constants + the documented server↔client event contract.
- `lifecycle.py` — `RunRegistry`: cancel / reattach / graceful-shutdown of in-flight turn tasks (frees the semaphore slot).
- `ws_schemas.py` — Pydantic validation of inbound WS frames (tagged union on `type`) + the `outbound` envelope.
- `transcript.py` — `history_items`: pure replay transcript for resumed/shared chats (session → WS item shape).

## Scoped tests
```bash
pytest tests/agent/test_sdk_engine.py tests/agent/test_prompt_stability.py tests/agent/test_deterministic_msgs.py
```
(`test_prompt_stability.py` is the cache-stability guard — run it after ANY change to `prompt.py`.)
