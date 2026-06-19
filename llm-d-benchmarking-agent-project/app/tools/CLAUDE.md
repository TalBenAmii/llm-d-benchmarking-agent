# app/tools/ — the 35 agent tools (mechanism layer)

Tools validate their args against Pydantic schemas, gate mutating commands, run allowlisted
argv through the executor, and return a flat JSON-serializable dict to the agent. `registry.py`
is the authoritative list. Judgment about *what to do with* results lives in `knowledge/`, not here.

## How to add a tool (the pattern to copy)
1. **Handler** — `app/tools/<name>.py`: `async def my_tool(ctx: ToolContext, *, arg: str) -> dict[str, Any]`.
2. **Input schema** — a Pydantic model in `app/tools/schemas.py`; every `Field(..., description=...)`
   is **exposed to the LLM** in the JSON Schema, so write the description for the model (point it at
   `read_knowledge('<topic>')` for judgment), not as an impl note.
3. **Register** — in `registry.py::build_registry()` add a `ToolSpec(name, _DESCRIPTIONS[name],
   InputModel, handler)` and a `_DESCRIPTIONS[name]` entry. `dispatch()` (`registry.py`) validates
   `raw_input` against the model and **returns** `{"error": ...}` on a `ValidationError` (the agent
   self-corrects) — it does **not** raise.

## Local invariants
- **Read-only vs mutating is decided by the executor, not the tool.** Call `ctx.run_readonly(argv)`
  for probes (auto-runs) and `ctx.run_command(argv)` for mutations (routes through approval; rejection
  raises `ApprovalRejected`). A handler that calls neither just auto-runs (pure Python, e.g. analyze).
- **Raise `ToolError` for any non-retryable failure** (bad input, missing repo, allowlist denial) — the
  loop turns it into a clean `{"error": ...}`. Never raise *other* exceptions (they break the session).
- **Allowlist denial is not a bug, it's defense.** Don't work around it (don't shell out to bypass the
  approval gate / quota / observability). Widen capability in `security/allowlist.yaml` (data) instead.
- **Write only to `ctx.workspace`** (per-session). Never write into the READ-ONLY repos or `/tmp`.
- **Return flat, JSON-serializable, secret-free dicts.** Pass HF tokens via `ctx.run_command(..., env=…)`,
  never in argv or the result. Emitted command events carry argv only, never env.
- **After cloning repos, call `ctx.catalog(refresh=True)`** or later tools see the stale (empty) catalog.

## Key files
- `registry.py` — `build_registry()` (name→`ToolSpec`) + `dispatch()` (validate → handler). Authoritative.
- `context.py` — `ToolContext` DI container + thin `run_command`/`run_readonly` delegators; `ToolError`/`ApprovalRejected`/`QuotaError`.
- `command_exec.py` — `CommandExecutor`: validate → quota → approval → run → record. Tools don't touch it directly.
- `schemas.py` — every tool's Pydantic input model.
- `execute.py` · `orchestrate.py` · `analyze.py` · `capacity.py` · `config_artifact.py` · `repos.py` · `command.py` · `probe.py` · `knowledge_access.py` — the individual tools.

## Gotchas
- Schema validation errors are **returned, not raised** — surface your own enum/range errors as a dict with `"error"`, don't raise mid-handler.
- A `timeout_s` declared in `allowlist.yaml` **overrides** any `timeout=` you pass.
- Result dicts are not schema-checked — a typo'd key silently misleads the agent; assert key presence in tests.

## Scoped tests
```bash
pytest tests/test_new_tools.py tests/test_schemas.py tests/test_command_events.py tests/test_allowlist.py
```
