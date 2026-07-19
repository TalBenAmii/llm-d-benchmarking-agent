"""Claude-Agent-SDK option + replay-rendering helpers for the SDK-native engine.

Relocated from the deleted provider shim at the Phase 5 cutover: the engine builds its
ClaudeAgentOptions from these, and the resume-fallback (a fresh SDK session seeded from the
``session.messages`` mirror) renders prior turns with the same faithful narration shapes the
old provider replayed history with.
"""
from __future__ import annotations

import json
from typing import Any

# Effort levels the SDK/CLI accepts. Anything else falls back to {} (the CLI's own default),
# so a typo can never crash a turn — it just declines to override the effort. The model
# picker's per-model efforts (app/llm/model_catalog.py) are pinned as subsets of this set.
EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})


def thinking_options(thinking: str) -> dict[str, Any]:
    """Translate the ``AGENT_SDK_THINKING`` setting into ClaudeAgentOptions thinking kwargs.

    ``"adaptive"`` → ``thinking={"type": "adaptive"}`` (Claude decides depth — what Sonnet in
    Claude Code does); a positive integer string → a fixed per-turn budget that forces thinking
    every turn; ``"off"``/``"disabled"`` (or anything unrecognized) → ``{}`` (no extended
    thinking). Returned as kwargs so an empty dict cleanly means "don't set the option"."""
    value = (thinking or "").strip().lower()
    if value == "adaptive":
        return {"thinking": {"type": "adaptive"}}
    if value.isdigit() and int(value) > 0:
        return {"thinking": {"type": "enabled", "budget_tokens": int(value)}}
    return {}


def effort_option(effort: str) -> dict[str, Any]:
    """Translate ``AGENT_SDK_EFFORT`` into an ``effort`` kwarg, or ``{}`` for an unknown value
    (so the CLI keeps its own default rather than erroring)."""
    value = (effort or "").strip().lower()
    return {"effort": value} if value in EFFORT_LEVELS else {}


def render_assistant_text(text: str, tool_calls: list[dict[str, Any]]) -> str:
    """Render a prior assistant turn (its text + the tool calls it made) as plain text. A
    replayed transcript can't carry native ``tool_use`` blocks, so the model re-reads its own
    past actions as a short, faithful narration."""
    parts: list[str] = []
    if text:
        parts.append(text)
    for tc in tool_calls:
        parts.append(f"[called tool {tc['name']} with {json.dumps(tc['input'], ensure_ascii=False)}]")
    return "\n".join(parts) or "(no output)"


def render_tool_results(results: list[dict[str, Any]]) -> str:
    """Render a ``tool_results`` turn as user text — the matching half of
    :func:`render_assistant_text`, since results can't be replayed as native blocks either."""
    lines = ["[tool results]"]
    for r in results:
        lines.append(f"{r['name']} → {r['content']}")
    return "\n".join(lines)
