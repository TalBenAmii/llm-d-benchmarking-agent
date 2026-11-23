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

**This card is the ONLY greeting** — shown by code at connect time, BEFORE the user's first
message. Full first-message / no-re-greet rule → conversation_style.md §"First message".

## Hi! I'm the llm-d Benchmarking Assistant.

### Capabilities
- Tell me about your app and what you want to measure. I’ll choose settings for llm-d-benchmark and show you the plan for approval.
- I can benchmark a model you already serve, or deploy a CPU simulator for a local trial.
- I can explain how quickly your model responded and whether it met your target.
- I can compare configurations, track performance trends, or help you design a custom workload.
- If you’re planning a deployment, I can estimate whether the model will fit on your hardware.

### Nudge
Try asking “How quickly will my chat app start replying when it gets busy?”
