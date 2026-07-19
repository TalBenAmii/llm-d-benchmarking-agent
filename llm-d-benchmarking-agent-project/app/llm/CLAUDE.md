# app/llm/ — SDK options + the switchable model catalog

Small, pure support layer for the SDK-native engine (`app/agent/engine.py`). There is no
provider abstraction anymore: the engine IS the Claude Agent SDK/CLI (subscription/Max plan,
keyless — auth via the logged-in `claude` CLI). Anything else in `LLM_PROVIDER` fails app
readiness with a clear "unsupported provider" error (`AGENT_SDK_PROVIDERS` in
`model_catalog.py`; guard in `app/main.py` + `app/storage/retention.py`).

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
