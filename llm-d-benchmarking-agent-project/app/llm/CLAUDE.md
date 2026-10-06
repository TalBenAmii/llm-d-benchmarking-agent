# app/llm/ — SDK options + the switchable model catalog

Support for the Claude and Codex SDK engines. `model_catalog.py` holds the Claude
catalog and provider alias sets. `codex_options.py` configures the official Codex
SDK, requires a ChatGPT subscription login, disables independent execution tools,
and discovers model choices without exposing account identity. Unsupported providers
fail readiness; Codex readiness also checks the login at server startup.

## Key files
- `sdk_options.py` — pure helpers the engine builds its `ClaudeAgentOptions` from:
  `thinking_options` / `effort_option` (env-setting → SDK kwargs; unknown values degrade to the
  CLI's own default, never crash) and `render_assistant_text` / `render_tool_results` (the
  resume-fallback's faithful plain-text narration of prior turns — the CLI rejects synthetic
  `tool_use`/`tool_result` blocks, so history replays as text).
- `model_catalog.py` — pure/no-I/O switchable Anthropic model catalog for the chat-UI picker
  (`served_models`/`valid_selection`/`model_views` + `AGENT_SDK_PROVIDERS`); read by
  `app.web.provider_view` (`/api/provider`) + the `set_model` WS handler.

## Invariants (don't break)
- **Model + reasoning effort are per-session runtime-switchable** via the picker: the `set_model`
  WS frame validates against `valid_selection` and stores `session.model_override`/
  `effort_override`; the engine applies them per turn, never mutating global config or `.env`.
- **Per-model `efforts` stay subsets of `sdk_options.EFFORT_LEVELS`** (a test pins this so the
  two can't drift).
- **Keys stay blanked for the CLI child** (`engine._CLI_ENV`): `ANTHROPIC_API_KEY`/
  `ANTHROPIC_AUTH_TOKEN` are emptied so a stray key can't force per-token API billing over the
  subscription.

## Scoped tests
```bash
pytest tests/agent/test_sdk_engine.py tests/agent/test_model_picker.py tests/agent/test_provider_info.py
```
