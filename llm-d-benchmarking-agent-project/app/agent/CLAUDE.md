# app/agent/ — the LLM agent loop + system-prompt assembly

The agent loop (`loop.py`) calls the LLM, dispatches tool calls (approval-gated), and feeds
results back. `prompt.py` assembles the system prompt. Decision logic lives in `knowledge/`
and the model's reasoning — **never** in `if/elif` here.

## ⚠️ Headline invariant: the system-prompt prefix is BYTE-STABLE and prompt-cached
`build_system_prompt()` (in `prompt.py`) must return a **byte-identical** prefix across
turns so the provider prompt-cache keeps hitting (first turn = cache write, later turns =
~10% cache reads). The cached prefix = `ROLE` + `HARD_RULES` + inlined `CORE_KNOWLEDGE` +
on-demand knowledge index + `CATALOG_POINTER` + `GROUP_CATALOG_NOTE` (+ conditional `SIMULATE_NOTE`).

**Anything that varies per turn must NOT go in the prefix.** The live catalog snapshot and
the environment pre-probe are injected as **synthetic per-turn user messages** (`loop.py`
+ `catalog_brief_message` in `prompt.py`), precisely so they don't mutate the cached
prefix. If you append dynamic text to `build_system_prompt()`, you bust the cache on every
turn. `tests/test_context_mgmt.py` enforces byte-stability + "catalog body never in the
prefix" + "catalog injected exactly once".

## Other local invariants
- **CORE_KNOWLEDGE inlining** (`prompt.py:` the `CORE_KNOWLEDGE` tuple): only the early-phase
  guides named in that tuple are inlined verbatim; everything else is indexed and pulled on demand
  via `read_knowledge("<topic>")`. Adding a file to CORE inflates **every** call — see
  `knowledge/CLAUDE.md` for the cost rule. Don't add to CORE casually (`key_docs.yaml` is
  deliberately NOT in CORE — `fetch_key_docs` already delivers its content live).
- **Phase-group tools loaded on demand** (`loop.py` + `registry.tool_definitions(loaded=...)`):
  most tool schemas ride in the cached prefix on EVERY step, so only the `registry.STARTER_KIT` is
  shown by default; the rest are in named groups (`registry._TOOL_GROUPS`: setup/run/analyze/
  advanced) hidden until the model calls `load_tools(['<group>'])` (which folds the group(s) into the
  persisted `session.loaded_groups`). The unlock is MODEL-DRIVEN, not a phase gate — a user can enter
  directly at the sweep/analyze/reproduce phase with no in-session deploy, so only the model reliably
  knows which group a request needs. `run_turn` detects the set change between steps and RE-OPENS the
  provider turn with the expanded set (the tool list is part of the provider cache key, so a changed
  set needs a fresh turn) so the group's tools are callable the SAME turn; re-open happens once per
  distinct `load_tools` call (typically 2-3/session). `prompt.GROUP_CATALOG_NOTE` tells the model the
  groups + how to load; keep it and `_TOOL_GROUPS` in sync (a bidirectional test enforces it), and
  `LoadToolsInput`'s `Literal` group names too. Mechanism only — capability scoping, no judgment.
- **Compaction** (`context_mgmt.py`): old tool results are compacted to save context. Only
  mutate content strings — never break tool-call/result pairing, and keep the most recent
  messages (`_RECENT_MESSAGES_KEPT`).
- **One-shot session state** (`session.py`): `catalog_injected`, `prewarmed`, and `loaded_groups`
  are **persisted** (a resumed chat must not re-inject the catalog / env pre-probe, and keeps its
  loaded tool groups); a pre-feature snapshot's old `advanced_tools_enabled: True` migrates to
  `{"advanced"}` on load. `env_snapshot` is **runtime-only** (deliberately not persisted — a resume
  re-probes fresh).

## Key files
- `loop.py` — the turn loop: LLM call → tool dispatch (approval gating) → result feedback.
- `prompt.py` — byte-stable cached prefix + per-turn catalog/env synthetic messages.
- `context_mgmt.py` — compaction of old tool results + `clamp_tool_result_content` (caps tool-result feedback via a valid-JSON truncation envelope).
- `session.py` — per-session state, persistence, one-shot flags, title derivation.
- `cards.py` — deterministic, knowledge/data-sourced UI content (mechanism only): the welcome card, the post-run results card, and the `suggestions.yaml` start-of-chat chips (data only; deliberately outside `knowledge/`).
- `channel.py` — per-session turn↔socket link: turns survive disconnects; buffers/replays live events + pending approvals.
- `events.py` — WS event-type constants + the documented server↔client event contract.
- `lifecycle.py` — `RunRegistry`: cancel / reattach / graceful-shutdown of in-flight turn tasks (frees the semaphore slot).
- `ws_schemas.py` — Pydantic validation of inbound WS frames (tagged union on `type`) + the `outbound` envelope.
- `transcript.py` — `history_items`: pure replay transcript for resumed/shared chats (session → WS item shape).

## Scoped tests
```bash
pytest tests/test_context_mgmt.py tests/test_loop.py tests/test_deterministic_msgs.py
```
(`test_context_mgmt.py` is the cache-stability guard — run it after ANY change to `prompt.py`.)
