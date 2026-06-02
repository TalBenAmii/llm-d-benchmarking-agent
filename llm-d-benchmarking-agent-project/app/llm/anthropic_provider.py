"""Anthropic (Claude) provider — native tool calling."""
from __future__ import annotations

from typing import Any

from app.config import Settings
from app.llm.provider import AssistantTurn, LLMProvider, ProviderError, ToolCall, Usage

_MAX_TOKENS = 4096

# Ephemeral cache breakpoint reused at each of our (≤4) cache points: tools, system, and the
# rolling conversation tail. Provider-agnostic caching is the point — what the model SEES is
# unchanged; the breakpoints only hint the prefix is reusable.
_EPHEMERAL = {"type": "ephemeral"}


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

    async def chat(self, *, system, messages, tools, cache_key=None) -> AssistantTurn:
        # cache_key is accepted for the provider-agnostic interface but ignored here —
        # Anthropic caches automatically by prefix (we mark the breakpoints below).
        anth_tools = [
            {"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]}
            for t in tools
        ]
        # (1) Cache the whole tools block: a breakpoint on the LAST tool covers everything before it.
        if anth_tools:
            anth_tools[-1] = {**anth_tools[-1], "cache_control": _EPHEMERAL}
        anth_messages = _to_anthropic(messages)
        # (3) ROLLING conversation breakpoint: mark the last content block of the last message.
        _mark_last_cacheable(anth_messages)
        resp = await self._client.messages.create(
            model=self._model,
            max_tokens=_MAX_TOKENS,
            # (2) Cache the large static system prefix. (Literal cache_control here so the SDK's
            # typed system param infers the ephemeral TypedDict; _EPHEMERAL elsewhere is fine.)
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=anth_messages,
            tools=anth_tools,
        )
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name, input=dict(block.input)))
        # Real usage from the provider — defensive, since fields may be missing/None on some
        # responses. Anthropic already EXCLUDES cached reads/writes from input_tokens.
        u = getattr(resp, "usage", None)
        usage = Usage(
            input_tokens=getattr(u, "input_tokens", 0) or 0,
            output_tokens=getattr(u, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
        return AssistantTurn(
            text="".join(text_parts) or None,
            tool_calls=tool_calls,
            stop_reason=resp.stop_reason,
            usage=usage,
        )


def _mark_last_cacheable(out: list[dict[str, Any]]) -> None:
    """Attach an ephemeral cache_control to the LAST content block of the LAST message — the
    rolling conversation breakpoint. User messages have a PLAIN STRING content; convert that
    to block form so the breakpoint can be attached. No-op on an empty conversation."""
    if not out:
        return
    last = out[-1]
    content = last.get("content")
    if isinstance(content, str):
        last["content"] = [{"type": "text", "text": content, "cache_control": _EPHEMERAL}]
    elif isinstance(content, list) and content:
        content[-1] = {**content[-1], "cache_control": _EPHEMERAL}


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
