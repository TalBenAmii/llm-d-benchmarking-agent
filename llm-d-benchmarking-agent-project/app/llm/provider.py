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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings

# Called with each streamed text delta as the model generates (when a provider streams). The
# agent loop passes one that emits an ``assistant_delta`` event so the UI fills the live bubble
# token-by-token; providers that don't stream simply never invoke it.
OnText = Callable[[str], Awaitable[None]]


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


def model_context_limit(model: str) -> int:
    """The model's context-window size (input-token budget), used to render the live
    context-window meter (current prompt size / limit) the same way Claude Code shows
    "context used". Family-matched, not exact per snapshot — order matters (``haiku`` is
    checked before the generic ``claude`` branch). Unknown models get a conservative 200k."""
    m = (model or "").lower()
    if "haiku" in m:
        return 200_000
    if "opus" in m or "sonnet" in m or "claude" in m:
        return 1_000_000
    if "gpt-4o" in m or "gpt-4.1" in m or "gpt-4-turbo" in m:
        return 128_000
    return 200_000


class LLMProvider(ABC):
    # Concrete providers set ``self._model`` in __init__ (anthropic_model / agent_sdk_model /
    # openai_model). These read-only views expose it uniformly so the agent loop can size the
    # context-window meter without knowing which provider is active. Default "" keeps the
    # properties safe even if a subclass never set a model.
    _model: str = ""

    @property
    def model(self) -> str:
        return self._model

    @property
    def context_limit(self) -> int:
        return model_context_limit(self._model)

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


class ProviderTurn:
    """A turn-scoped handle that spans all of ONE user turn's LLM steps.

    The agent loop makes several ``chat()`` calls per user turn — one per round of tool calls.
    A ``ProviderTurn`` (an async context manager) lets a provider amortize expensive per-turn
    setup across those steps: ``__aenter__`` opens whatever is costly once, ``chat()`` runs each
    step (optionally streaming text via ``on_text``), ``__aexit__`` tears it down. The default
    :class:`StatelessTurn` does NO amortization — it just forwards each step to the provider's
    one-shot ``chat()`` — so every provider works through this interface unchanged. The Claude
    Agent SDK provider overrides it (``open_turn``) to keep one warm CLI subprocess per turn.
    """

    async def __aenter__(self) -> ProviderTurn:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def chat(
        self, messages: list[dict[str, Any]], *, on_text: OnText | None = None
    ) -> AssistantTurn:
        raise NotImplementedError


class StatelessTurn(ProviderTurn):
    """Default turn handle: no per-turn amortization. Each step is an independent one-shot
    ``provider.chat()`` over the FULL message list — exactly the behavior that predates the turn
    abstraction. Used by every provider that does not override ``open_turn`` (and by the test
    fakes, whose ``chat()`` signature is unchanged). ``on_text`` is accepted for interface parity
    and ignored: the one-shot path does not stream partial text."""

    def __init__(
        self,
        provider: Any,
        *,
        system: str,
        tools: list[dict[str, Any]],
        cache_key: str | None = None,
    ):
        self._provider = provider
        self._system = system
        self._tools = tools
        self._cache_key = cache_key

    async def chat(
        self, messages: list[dict[str, Any]], *, on_text: OnText | None = None
    ) -> AssistantTurn:
        return await self._provider.chat(
            system=self._system, messages=messages, tools=self._tools, cache_key=self._cache_key
        )


def open_provider_turn(
    provider: Any,
    *,
    system: str,
    tools: list[dict[str, Any]],
    cache_key: str | None = None,
) -> ProviderTurn:
    """Return a turn handle for ``provider``: its own amortized turn if it implements
    ``open_turn`` (only the Claude Agent SDK provider does), else a :class:`StatelessTurn`
    wrapper. Duck-typed on ``open_turn`` so test fakes (which don't inherit ``LLMProvider``)
    transparently get the stateless path."""
    factory = getattr(provider, "open_turn", None)
    if factory is not None:
        return factory(system=system, tools=tools, cache_key=cache_key)
    return StatelessTurn(provider, system=system, tools=tools, cache_key=cache_key)


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
    if provider in ("claude-agent-sdk", "agent-sdk", "claude-max"):
        from app.llm.agent_sdk_provider import AgentSdkProvider
        return AgentSdkProvider(settings)
    raise ProviderError(f"unknown LLM_PROVIDER {settings.llm_provider!r}")
