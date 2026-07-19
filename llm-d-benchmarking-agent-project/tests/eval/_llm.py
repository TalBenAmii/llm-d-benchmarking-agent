"""One-shot LLM text call for the opt-in evals (judge / bug-hunt selector / skill eval).

The evals need a bare "system + user text → reply text" call with NO tools — the old provider
abstraction's ``chat()``. After the SDK-native cutover that surface is the Claude Agent SDK
itself: a single-turn ``ClaudeSDKClient`` session over the logged-in CLI. SPENDS QUOTA — only
ever called from the LLM_EVAL_LIVE/BUGHUNT-gated tests; the lazy import keeps hermetic
collection from touching the SDK/CLI at all.
"""
from __future__ import annotations

from app.agent.engine import _CLI_ENV
from app.config import get_settings


async def llm_text(system: str, user_text: str) -> str:
    """ONE model call: send ``user_text`` under ``system``, return the reply's text blocks.

    Same safety posture as the engine: keys blanked for the CLI child (subscription auth only),
    no settings sources, no tools, one turn."""
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, TextBlock

    settings = get_settings()
    options = ClaudeAgentOptions(
        system_prompt=system,
        setting_sources=[],
        max_turns=1,
        tools=[],            # NO tools — the SDK default would leave CLI built-ins live
        allowed_tools=[],    # and nothing may sidestep that (host access from an eval)
        model=settings.agent_sdk_model or None,
        env=dict(_CLI_ENV),
        cli_path=settings.claude_cli_path or None,
    )
    parts: list[str] = []
    async with ClaudeSDKClient(options=options) as client:
        await client.query(user_text)
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                parts += [b.text for b in message.content if isinstance(b, TextBlock)]
    return "\n".join(parts)
