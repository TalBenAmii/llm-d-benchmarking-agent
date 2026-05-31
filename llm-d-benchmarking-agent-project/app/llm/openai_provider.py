"""OpenAI-compatible provider — works with OpenAI or any OpenAI-compatible server
(including a self-hosted vLLM / llm-d endpoint)."""
from __future__ import annotations

import json
from typing import Any

from app.config import Settings
from app.llm.provider import AssistantTurn, LLMProvider, ProviderError, ToolCall


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

    async def chat(self, *, system, messages, tools) -> AssistantTurn:
        oai_tools = [
            {"type": "function", "function": {
                "name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
            for t in tools
        ]
        resp = await self._client.chat.completions.create(
            model=self._model,
            messages=_to_openai(system, messages),
            tools=oai_tools,
            tool_choice="auto",
        )
        msg = resp.choices[0].message
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
