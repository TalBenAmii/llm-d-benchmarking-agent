"""enable_advanced_tools — reveal the advanced/late-phase tool set on demand.

Mechanism only. To keep the default tool list (and the prompt-cached prefix) lean, the heavy
late-phase tool schemas (``registry._ADVANCED_TOOLS``: config sweeps, autotuning, DOE, resilience
drills, run export/reproduce, cross-run/-harness comparison, scenario authoring) are NOT exposed
by default. When the user's request needs one, the model calls this tool; the agent loop owns the
``Session``, so it flips ``session.advanced_tools_enabled`` and re-opens the provider turn with the
expanded set so the advanced tools are callable the SAME turn (see ``app/agent/loop.py``). This
handler just returns the confirmation the model reads — the JUDGMENT ("do I need them?") stays with
the model, which is why a fixed phase gate could not replace it (a user can enter directly at the
sweep/analyze/reproduce phase with no in-session deploy).
"""
from __future__ import annotations

from typing import Any

from app.tools.context import ToolContext


async def enable_advanced_tools(ctx: ToolContext) -> dict[str, Any]:
    """Confirm that the advanced tool set is now available. ``ctx`` is unused but kept for the
    uniform handler signature; the actual flag flip + provider-turn re-open are done by the loop."""
    return {
        "enabled": True,
        "note": "Advanced tools are now in your tool list (this same turn): config sweeps, "
                "autotuning, design-of-experiments, resilience drills, run export/reproduce, "
                "cross-run aggregation + cross-harness comparison, and scenario authoring. Call "
                "the specific tool you need next.",
    }
