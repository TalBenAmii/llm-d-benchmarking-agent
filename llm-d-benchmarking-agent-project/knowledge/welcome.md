# Welcome message — the deterministic start-of-chat greeting

This is the JUDGMENT content for the welcome card the backend shows at the start of a FRESH
chat (no history yet). It is emitted by CODE, not the LLM — consistent every time, with no
token cost — so the wording lives here (knowledge, editable) while the event/UI scaffolding
stays mechanism. Keep it aligned with the "What can you do?" bullets in
conversation_style.md so the deterministic welcome matches the agent's own voice.

The loader (app/agent/cards.py) parses this file deterministically:
- the **last** `## ` heading's text becomes the card **heading** (the first `## ` is the
  file's own title above, so put the greeting in the last `## `);
- every `- ` bullet under the `### Capabilities` section becomes a capability bullet;
- the first non-empty line under `### Nudge` becomes the closing nudge.
Missing pieces degrade gracefully (no bullets → no card; heading/nudge fall back to empty).

**This card is the ONLY greeting.** It is shown by code at connect time, BEFORE the user's first
message. So the agent must NOT repeat a capabilities splash in response to the user's first
message when that message has real content — it should engage the request (or engage-and-refuse)
exactly as on any later turn. Re-stating the capabilities summary in chat is reserved for when
the user's message is itself empty or a bare greeting/"what can you do?". See conversation_style.md.

## Hi! I'm the llm-d Benchmarking Assistant — here's how I can help.

### Capabilities
- Deploy a model stack on the local quickstart (a kind cluster with a CPU-simulated engine).
- Run a capacity pre-flight so a model/config is confirmed to fit before anything is stood up.
- Run benchmarks against a fresh or already-running stack.
- Compare configurations and run parameter sweeps to see what wins.
- Read and explain Benchmark Reports in plain language, tied to your goal.
- Track results and trends over time so you can spot regressions and wins.
- Co-author a custom spec and workload with you, step by step, then validate it before running.

### Nudge
Tell me what you'd like to benchmark, or pick one of the suggestions below to get started.
