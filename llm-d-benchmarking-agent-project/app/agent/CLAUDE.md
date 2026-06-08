# app/agent/ — the LLM agent loop + system-prompt assembly

The agent loop (`loop.py`) calls the LLM, dispatches tool calls (approval-gated), and feeds
results back. `prompt.py` assembles the system prompt. Decision logic lives in `knowledge/`
and the model's reasoning — **never** in `if/elif` here.

## ⚠️ Headline invariant: the system-prompt prefix is BYTE-STABLE and prompt-cached
`build_system_prompt()` (`prompt.py:127-141`) must return a **byte-identical** prefix across
turns so the provider prompt-cache keeps hitting (first turn = cache write, later turns =
~10% cache reads). The cached prefix = `ROLE` + `HARD_RULES` + inlined `CORE_KNOWLEDGE` +
on-demand knowledge index + `CATALOG_POINTER` (+ conditional `SIMULATE_NOTE`).

**Anything that varies per turn must NOT go in the prefix.** The live catalog snapshot and
the environment pre-probe are injected as **synthetic per-turn user messages** (`loop.py`
+ `catalog_brief_message` / `prompt.py:144-152`), precisely so they don't mutate the cached
prefix. If you append dynamic text to `build_system_prompt()`, you bust the cache on every
turn. `tests/test_context_mgmt.py` enforces byte-stability + "catalog body never in the
prefix" + "catalog injected exactly once".

## Other local invariants
- **CORE_KNOWLEDGE inlining** (`prompt.py:` the `CORE_KNOWLEDGE` tuple): only the 7 early-phase
  guides are inlined verbatim; everything else is indexed and pulled on demand via
  `read_knowledge("<topic>")`. Adding a file to CORE inflates **every** call — see
  `knowledge/CLAUDE.md` for the cost rule. Don't add to CORE casually.
- **Compaction** (`context_mgmt.py`): old tool results are compacted to save context. Only
  mutate content strings — never break tool-call/result pairing, and keep the most recent
  messages (`_RECENT_MESSAGES_KEPT`).
- **One-shot session state** (`session.py`): `catalog_injected` and `prewarmed` are **persisted**
  (a resumed chat must skip re-injecting the catalog / env pre-probe mid-transcript); `env_snapshot`
  is **runtime-only** (deliberately not persisted — a resume re-probes fresh).

## Key files
- `loop.py` — the turn loop: LLM call → tool dispatch (approval gating) → result feedback.
- `prompt.py` — byte-stable cached prefix + per-turn catalog/env synthetic messages.
- `context_mgmt.py` — compaction of old tool results.
- `session.py` — per-session state, persistence, one-shot flags, title derivation.
- `welcome.py` · `results_card.py` — deterministic, knowledge-sourced cards (mechanism only).

## Scoped tests
```bash
pytest tests/test_context_mgmt.py tests/test_loop.py tests/test_deterministic_msgs.py
```
(`test_context_mgmt.py` is the cache-stability guard — run it after ANY change to `prompt.py`.)
