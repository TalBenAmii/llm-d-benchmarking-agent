# Welcome message — the deterministic start-of-chat greeting

This is the JUDGMENT content for the welcome card the backend shows at the start of a FRESH
chat (no history yet). It is emitted by CODE, not the LLM — consistent every time, with no
token cost — so the wording lives here (knowledge, editable) while the event/UI scaffolding
stays mechanism. Keep it aligned with the "What can you do?" bullets in
conversation_style.md so the deterministic welcome matches the agent's own voice.

The loader (app/agent/welcome.py) parses this file deterministically:
- the first `## ` heading's text becomes the card **heading**;
- every top-level `- ` bullet under the `### Capabilities` section becomes a capability bullet;
- the first paragraph under `### Nudge` becomes the closing nudge line.
Missing pieces degrade gracefully (the UI falls back to its plain note / chips).

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
