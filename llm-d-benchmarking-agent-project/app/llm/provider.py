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
class Usage:
    """Real token usage for ONE LLM call, normalized across providers so the UI math is
    provider-agnostic. NORMALIZATION CONTRACT: ``input_tokens`` is the freshly-processed
    (non-cached) input ONLY; ``cache_read_tokens`` is the cached portion; ``total_input``
    is everything sent to the model. Both providers MUST honor this."""

    input_tokens: int = 0        # NON-cached input processed this call
    output_tokens: int = 0       # generated tokens
    cache_read_tokens: int = 0   # input served from cache (cheap)
    cache_write_tokens: int = 0  # input written to cache this call (Anthropic only; 0 elsewhere)

    @property
    def total_input(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )


@dataclass
class AssistantTurn:
    text: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str | None = None
    usage: Usage = field(default_factory=Usage)


class LLMProvider(ABC):
    @abstractmethod
    async def chat(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        cache_key: str | None = None,
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
