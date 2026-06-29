"""load_tools — reveal a phase-group of tools on demand.

Mechanism only. To keep the default tool list (and the prompt-cached prefix) lean, most tools are
hidden behind named groups (``registry._TOOL_GROUPS``: setup / run / analyze / advanced); only the
``STARTER_KIT`` is shown by default. When the user's request needs a grouped tool, the model calls
this with the group name(s); the agent loop owns the ``Session``, so it folds the requested groups
into ``session.loaded_groups`` and re-opens the provider turn with the expanded set so the tools
are callable the SAME turn (see ``app/agent/loop.py``). This handler just validates + echoes which
groups are now loaded — the JUDGMENT ("which group do I need?") stays with the model, which is why a
fixed phase gate could not replace it (a user can enter directly at the sweep/analyze/reproduce
phase with no in-session deploy). This is the generalization of the former ``enable_advanced_tools``
(the advanced tier is now just one group).
"""
from __future__ import annotations

from typing import Any

from app.tools.context import ToolContext


async def load_tools(ctx: ToolContext, *, groups: list[str]) -> dict[str, Any]:
    """Confirm which tool group(s) are now available. ``groups`` is already validated against the
    known group names by ``LoadToolsInput``. ``ctx`` is unused but kept for the uniform handler
    signature; the actual ``session.loaded_groups`` update + provider-turn re-open are done by the
    loop, which reads the ``loaded`` list below."""
    # De-dupe while preserving order (a model may repeat a group).
    loaded = list(dict.fromkeys(groups))
    return {
        "loaded": loaded,
        "note": f"Loaded tool group(s): {', '.join(loaded)}. Their tools are now in your tool list "
                "(this same turn) — call the specific tool you need next.",
    }
