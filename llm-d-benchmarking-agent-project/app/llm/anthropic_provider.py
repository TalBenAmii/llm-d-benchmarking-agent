"""Anthropic (Claude) provider — native tool calling."""
from __future__ import annotations

from typing import Any

from app.config import Settings
from app.llm.provider import AssistantTurn, LLMProvider, ProviderError, ToolCall

_MAX_TOKENS = 4096


class AnthropicProvider(LLMProvider):
    def __init__(self, settings: Settings):
        if not settings.anthropic_api_key:
            raise ProviderError("ANTHROPIC_API_KEY is not set")
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:  # pragma: no cover
            raise ProviderError("the 'anthropic' package is not installed") from exc
        self._client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        self._model = settings.anthropic_model

    async def chat(self, *, system, messages, tools) -> AssistantTurn:
        anth_tools = [
            {"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]}
            for t in tools
        ]
        resp = await self._client.messages.create(
            model=self._model,
            max_tokens=_MAX_TOKENS,
            system=system,
            messages=_to_anthropic(messages),
            tools=anth_tools,
        )
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name, input=dict(block.input)))
        return AssistantTurn(
            text="".join(text_parts) or None,
            tool_calls=tool_calls,
            stop_reason=resp.stop_reason,
        )


def _to_anthropic(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m["role"]
        if role == "user":
            out.append({"role": "user", "content": m["content"]})
        elif role == "assistant":
            content: list[dict[str, Any]] = []
            if m.get("content"):
                content.append({"type": "text", "text": m["content"]})
            for tc in m.get("tool_calls", []):
                content.append({"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["input"]})
            out.append({"role": "assistant", "content": content or [{"type": "text", "text": ""}]})
        elif role == "tool_results":
            content = [
                {"type": "tool_result", "tool_use_id": r["tool_call_id"], "content": r["content"]}
                for r in m["results"]
            ]
            out.append({"role": "user", "content": content})
    return out
