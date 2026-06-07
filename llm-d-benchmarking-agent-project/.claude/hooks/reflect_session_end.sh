#!/usr/bin/env bash
# SessionEnd hook — a once-per-session reflection nudge (NOT per-turn; Stop would spam every turn).
# Fires at session teardown (clear/logout/other). It cannot change the model's behavior — it just
# writes a reminder to stderr so durable conclusions get captured while the context is fresh.
cat >&2 <<'NUDGE'

──────── session reflection ────────
Did this session surface anything reusable? Capture it now while it's fresh:
  • env/test/build setup, repo gotchas, locked design decisions → 1–2 tight lines in
    llm-d-benchmarking-agent-project/CLAUDE.md (or the relevant subsystem CLAUDE.md)
  • who-you-are / how-to-work / project-state facts → a memory file under
    ~/.claude/projects/-home-tal-kind-quickstart-guide/memory/ (+ a line in MEMORY.md)
Consolidate, don't duplicate. Skip if nothing durable came up.
─────────────────────────────────────
NUDGE
exit 0
