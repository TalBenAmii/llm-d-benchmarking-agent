"""suggest_next_steps — render the agent's "what next?" offer as clickable buttons.

Mechanism only. The model decides WHAT to offer and WHEN (judgment lives in the prompt +
knowledge/conversation_style.md); this tool just hands the chosen options back so the UI can
draw them as the same floating suggestion pills the welcome chips use. Clicking one sends its
``prompt`` as the user's next message.

This is the structured analog of the approval-card rule: the agent stops asking "want me to…?"
in prose and instead CALLS this tool, exactly as it raises an Approve/Decline card by calling
run_shell / execute_llmdbenchmark / propose_session_plan. The chips ride the tool RESULT
(``suggestions``), so the
existing tool-result-card path renders them live AND replays them on resume/reload — no separate
event or persistence machinery needed (the payload is tiny and survives the feed-back clamp).
"""
from __future__ import annotations

from typing import Any

from app.tools.context import ToolContext


async def suggest_next_steps(
    ctx: ToolContext, *, suggestions: list[dict[str, str]]
) -> dict[str, Any]:
    """Return the chosen next-step options for the UI to render as clickable buttons.

    Args are already shape- and length-validated by ``SuggestNextStepsInput`` (1-4 items, each
    ``{label, prompt}``). Pure pass-through: no command, no side effect — the value IS the UI
    payload. ``ctx`` is unused but kept for the uniform handler signature."""
    chips = [
        {"label": s["label"], "prompt": s["prompt"]}
        for s in suggestions
        if s.get("label") and s.get("prompt")
    ]
    return {
        "suggestions": chips,
        "count": len(chips),
        # A terse confirmation for the MODEL (the chips themselves are for the UI). Calling this
        # tool ENDS the turn (the loop treats it as terminal), so there is no next step to narrate
        # into — never add a lead-in before this call or a closing line about the buttons; they
        # are already shown and speak for themselves.
        "shown": True,
        "note": "These options are now shown to the user as clickable buttons. Do NOT narrate "
                "them in prose — no lead-in, no 'use the buttons below'. The turn ends here; wait "
                "for the user to click one or type a reply.",
    }
