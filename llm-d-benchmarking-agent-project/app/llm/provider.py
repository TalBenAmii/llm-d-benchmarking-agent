"""Provider-agnostic LLM interface with tool-calling.

A neutral message format is converted to each backend's native shape. The agent loop
only ever sees :class:`AssistantTurn` (text + tool calls), regardless of provider.

Neutral message items (a list of dicts):
  {"role": "user", "content": str}
  {"role": "assistant", "content": str, "tool_calls": [{"id","name","input"}]}
  {"role": "tool_results", "results": [{"tool_call_id","name","content"}]}
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class AssistantTurn:
    text: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str | None = None


class LLMProvider(ABC):
    @abstractmethod
    async def chat(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> AssistantTurn:
        ...


class ProviderError(RuntimeError):
    pass


def get_provider(settings: Settings) -> LLMProvider:
    provider = (settings.llm_provider or "anthropic").lower()
    if provider == "anthropic":
        from app.llm.anthropic_provider import AnthropicProvider
        return AnthropicProvider(settings)
    if provider in ("openai", "openai-compatible", "vllm"):
        from app.llm.openai_provider import OpenAIProvider
        return OpenAIProvider(settings)
    raise ProviderError(f"unknown LLM_PROVIDER {settings.llm_provider!r}")
