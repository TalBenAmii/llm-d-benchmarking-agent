"""OpenAI-compatible provider — works with OpenAI or any OpenAI-compatible server
(including a self-hosted vLLM / llm-d endpoint)."""
from __future__ import annotations

import json
from typing import Any

from app.config import Settings
from app.llm.provider import AssistantTurn, LLMProvider, ProviderError, ToolCall, Usage


class OpenAIProvider(LLMProvider):
    def __init__(self, settings: Settings):
        if not settings.openai_api_key:
            raise ProviderError("OPENAI_API_KEY is not set")
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover
            raise ProviderError("the 'openai' package is not installed") from exc
        self._client = AsyncOpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
        self._model = settings.openai_model
        self._send_cache_key = settings.openai_send_prompt_cache_key

    async def chat(self, *, system, messages, tools, cache_key=None) -> AssistantTurn:
        oai_tools = [
            {"type": "function", "function": {
                "name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
            for t in tools
        ]
        extra: dict[str, Any] = {}
        # Only send prompt_cache_key when explicitly enabled (OpenAI proper): some
        # OpenAI-compatible servers reject unknown params, so default OFF keeps them working
        # (they still get implicit prefix caching for free).
        if self._send_cache_key and cache_key:
            extra["prompt_cache_key"] = cache_key
        resp = await self._client.chat.completions.create(
            model=self._model,
            messages=_to_openai(system, messages),
            tools=oai_tools,
            tool_choice="auto",
            **extra,
        )
        # Some OpenAI-compatible servers (vLLM / llm-d under content-filter or error
        # conditions) can return a 200 with an EMPTY choices array. Guard it with a clear
        # ProviderError instead of letting `choices[0]` leak an opaque IndexError — mirrors
        # the "never crash on a degenerate response" contract of _usage_from below.
        choice = (resp.choices or [None])[0]
        if choice is None:
            raise ProviderError("the model server returned no choices (empty response)")
        msg = choice.message
        tool_calls: list[ToolCall] = []
        for tc in (msg.tool_calls or []):
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, input=args))
        return AssistantTurn(
            text=msg.content or None,
            tool_calls=tool_calls,
            stop_reason=resp.choices[0].finish_reason,
            usage=_usage_from(getattr(resp, "usage", None)),
        )


def _usage_from(u: Any) -> Usage:
    """Normalize an OpenAI usage object to the cross-provider contract. OpenAI reports cached
    tokens as a SUBSET of prompt_tokens, so subtract to get the freshly-processed input. Some
    OpenAI-compatible servers return no usage at all — never crash, return zeros."""
    if u is None:
        return Usage()
    prompt = getattr(u, "prompt_tokens", 0) or 0
    cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
    completion = getattr(u, "completion_tokens", 0) or 0
    return Usage(
        input_tokens=max(prompt - cached, 0),
        output_tokens=completion,
        cache_read_tokens=cached,
        cache_write_tokens=0,
    )


def _to_openai(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for m in messages:
        role = m["role"]
        if role == "user":
            out.append({"role": "user", "content": m["content"]})
        elif role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": m.get("content") or None}
            if m.get("tool_calls"):
                entry["tool_calls"] = [
                    {"id": tc["id"], "type": "function",
                     "function": {"name": tc["name"], "arguments": json.dumps(tc["input"])}}
                    for tc in m["tool_calls"]
                ]
            out.append(entry)
        elif role == "tool_results":
            for r in m["results"]:
                out.append({"role": "tool", "tool_call_id": r["tool_call_id"], "content": r["content"]})
    return out
