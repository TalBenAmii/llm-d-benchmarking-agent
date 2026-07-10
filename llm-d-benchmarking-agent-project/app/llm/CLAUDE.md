# app/llm/ — provider-agnostic LLM integration (native tool-calling)

A neutral message format (user / assistant+tool_calls / tool_results) converted to each backend's native
shape; the agent loop only ever sees an `AssistantTurn`. Backends: Anthropic API and the Claude Agent SDK
(subscription/Max plan via the local `claude` CLI). **ACTIVE provider = `claude-agent-sdk`** (Max plan, no
API key).

## The one judgment-shaped line (allowed)
`get_provider(settings)` in `provider.py` maps `LLM_PROVIDER` to a concrete provider (`anthropic` default;
`claude-agent-sdk`/`agent-sdk`/`claude-max`), importing lazily so an uninstalled SDK can't break the other.
This is **config dispatch, not agent judgment** — acceptable under thin-code. Don't grow it into
behavioural branching.

## Invariants (don't break)
- **`Usage` normalization contract:** `input_tokens` is NON-cached input ONLY (Anthropic reports cached
  separately). `thinking` is surfaced by the Claude Agent SDK provider only and is **NEVER fed back**
  into the conversation.
- **`agent_sdk_provider.py` is the deepest/most fragile module.** The app keeps its OWN agent loop: the SDK
  gets the app's tools as in-process MCP tools but a `can_use_tool` callback **DENIES every call** so the SDK
  never executes a handler; `max_turns=1` raises `error_max_turns` *after* delivering the tool-calling turn —
  that is the EXPECTED stop and is swallowed. `setting_sources=[]` (ignores `~/.claude/CLAUDE.md`);
  `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` are BLANKED for the child so a stray key can't force per-token
  API billing over the subscription. History replays as plain user/assistant TEXT (the CLI rejects synthetic
  `tool_use`/`tool_result` blocks → HTTP 400); content must be a list of blocks; consecutive same-role turns
  are COALESCED (the CLI drops all but the first). One warm CLI subprocess per user turn + a single-slot
  prewarm pool (`_PREWARM_TTL_S=120s`) — don't leak spares (BUG-033).
- **Model + reasoning effort are per-session runtime-switchable (agent-SDK only)** via the chat-UI picker:
  `open_provider_turn(model=, effort=)` applies a PER-TURN override that NEVER mutates the provider
  singleton or `.env` (`model_catalog.py` = the served allowlist + `valid_selection`). **Invariant:** the
  agent-SDK connection/prewarm `_fingerprint` MUST fold in model+reasoning, so a switch can't adopt a
  stale prewarmed connection built for a different model/effort.

## Key files
- `provider.py` — `LLMProvider` ABC, `AssistantTurn` / `ToolCall` / `Usage`, `open_provider_turn`, `get_provider`.
- `anthropic_provider.py` · `agent_sdk_provider.py` — the two backends.
- `model_catalog.py` — pure/no-I/O switchable Anthropic model catalog for the picker (`served_models`/`valid_selection`/`model_views`); read by `app.web.provider_view` (`/api/provider`) + the `set_model` WS handler. Agent-SDK only.

## Scoped tests
```bash
pytest tests/test_agent_sdk_provider.py tests/test_provider_pack.py tests/test_llm_caching_usage.py
```
