"""Hermetic fake Transport for claude-agent-sdk.

FakeTransport plays a scripted conversation through the SDK's REAL protocol
machinery: ClaudeSDKClient + Query parse the messages, bridge can_use_tool to
the registered callback, and execute the real in-process MCP tool handlers on
the mcp_message control requests the fake emits — exactly like the CLI would.

A script is a list of turns; a turn is the list of wire messages the "CLI"
sends after receiving one user message (built with the assistant/text/
tool_use/result/stream_event helpers below). For every tool_use block in a
scripted assistant message the fake emits a can_use_tool control request,
awaits the SDK's decision, on allow emits an mcp_message tools/call (so the
SDK runs the real handler) and feeds the handler's result back as a
tool_result user message; on deny it feeds back an is_error tool_result.
"""

import json
import math
from collections.abc import AsyncIterator
from itertools import count
from typing import Any

import anyio

from claude_agent_sdk._internal.transport import Transport

FAKE_MODEL = "fake-model"
_RESPONSE_TIMEOUT = 10.0
_uuids = count(1)


def text(s: str) -> dict[str, Any]:
    return {"type": "text", "text": s}


def tool_use(id: str, name: str, input: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "id": id, "name": name, "input": input}


def assistant(*blocks: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "model": FAKE_MODEL, "content": list(blocks)},
        "parent_tool_use_id": None,
        "session_id": "default",
    }


def result(**overrides: Any) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "duration_ms": 1,
        "duration_api_ms": 1,
        "is_error": False,
        "num_turns": 1,
        "session_id": "default",
        "total_cost_usd": 0.0,
        "usage": {},
        "result": "",
    }
    msg.update(overrides)
    return msg


def stream_event(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "stream_event",
        "uuid": f"fake-uuid-{next(_uuids)}",
        "session_id": "default",
        "event": event,
    }


def _tool_result(tool_use_id: str, content: Any, is_error: bool) -> dict[str, Any]:
    block = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
        "is_error": is_error,
    }
    return {
        "type": "user",
        "message": {"role": "user", "content": [block]},
        "parent_tool_use_id": None,
        "session_id": "default",
    }


class FakeTransport(Transport):
    """Scripted stand-in for the CLI subprocess behind ClaudeSDKClient.

    Records every user message the SDK writes in ``user_messages`` and every
    can_use_tool decision payload the SDK returned in ``permission_responses``.
    """

    def __init__(self, script: list[list[dict[str, Any]]]):
        self._script = list(script)
        self._turn_index = 0
        self._ready = False
        self._input_ended = False
        self._request_ids = count(1)
        # (event, slot) per outstanding control request the fake sent the SDK.
        self._pending: dict[str, tuple[anyio.Event, list[dict[str, Any]]]] = {}
        # Items for read_messages: ("message", wire dict) | ("turn", scripted turn).
        self._items_send, self._items_recv = anyio.create_memory_object_stream[
            tuple[str, Any]
        ](max_buffer_size=math.inf)
        self.user_messages: list[dict[str, Any]] = []
        self.permission_responses: list[dict[str, Any]] = []

    async def connect(self) -> None:
        self._ready = True

    def is_ready(self) -> bool:
        return self._ready

    async def end_input(self) -> None:
        self._input_ended = True

    async def close(self) -> None:
        self._ready = False
        self._items_send.close()
        self._items_recv.close()

    async def write(self, data: str) -> None:
        assert self._ready, "write() on a transport that is not connected"
        message = json.loads(data)
        msg_type = message["type"]

        if msg_type == "user":
            assert not self._input_ended, "user message after end_input()"
            assert self._turn_index < len(self._script), (
                f"script exhausted: user message #{self._turn_index + 1} "
                f"but only {len(self._script)} scripted turn(s)"
            )
            self.user_messages.append(message)
            turn = self._script[self._turn_index]
            self._turn_index += 1
            self._items_send.send_nowait(("turn", turn))

        elif msg_type == "control_response":
            response = message["response"]
            request_id = response["request_id"]
            assert request_id in self._pending, (
                f"control_response for unknown/already-answered request_id "
                f"{request_id!r} (pending: {sorted(self._pending)})"
            )
            event, slot = self._pending.pop(request_id)
            slot.append(response)
            event.set()

        elif msg_type == "control_request":
            subtype = message["request"]["subtype"]
            assert subtype in {
                "initialize",
                "interrupt",
                "set_permission_mode",
                "set_model",
            }, f"unsupported outgoing control request subtype: {subtype!r}"
            reply = (
                {"commands": [], "output_style": "default"}
                if subtype == "initialize"
                else {}
            )
            self._items_send.send_nowait(
                (
                    "message",
                    {
                        "type": "control_response",
                        "response": {
                            "subtype": "success",
                            "request_id": message["request_id"],
                            "response": reply,
                        },
                    },
                )
            )

        else:
            raise AssertionError(f"unexpected message type written by SDK: {msg_type!r}")

    async def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        async for kind, payload in self._items_recv:
            if kind == "message":
                yield payload
                continue
            for msg in payload:
                yield msg
                if msg["type"] != "assistant":
                    continue
                for block in msg["message"]["content"]:
                    if block["type"] == "tool_use":
                        async for fed_back in self._execute_tool(block):
                            yield fed_back

    def _open_request(
        self, request: dict[str, Any]
    ) -> tuple[dict[str, Any], anyio.Event, list[dict[str, Any]]]:
        request_id = f"fake_req_{next(self._request_ids)}"
        event = anyio.Event()
        slot: list[dict[str, Any]] = []
        self._pending[request_id] = (event, slot)
        wire = {"type": "control_request", "request_id": request_id, "request": request}
        return wire, event, slot

    async def _await_response(
        self, event: anyio.Event, slot: list[dict[str, Any]], what: str
    ) -> dict[str, Any]:
        with anyio.fail_after(_RESPONSE_TIMEOUT):
            await event.wait()
        response = slot[0]
        assert response["subtype"] == "success", (
            f"SDK answered {what} with an error: {response.get('error')}"
        )
        return response["response"]

    async def _execute_tool(
        self, block: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        name = block["name"]
        assert name.startswith("mcp__"), (
            f"FakeTransport only executes SDK MCP tools (mcp__<server>__<tool>), "
            f"got {name!r}"
        )
        _, server_name, tool_name = name.split("__", 2)

        wire, event, slot = self._open_request(
            {
                "subtype": "can_use_tool",
                "tool_name": name,
                "input": block["input"],
                "permission_suggestions": None,
                "blocked_path": None,
                "tool_use_id": block["id"],
            }
        )
        yield wire
        decision = await self._await_response(event, slot, "can_use_tool")
        self.permission_responses.append(decision)

        if decision["behavior"] == "deny":
            yield _tool_result(block["id"], decision.get("message", ""), is_error=True)
            return
        assert decision["behavior"] == "allow", f"unknown behavior: {decision}"

        wire, event, slot = self._open_request(
            {
                "subtype": "mcp_message",
                "server_name": server_name,
                "message": {
                    "jsonrpc": "2.0",
                    "id": next(self._request_ids),
                    "method": "tools/call",
                    "params": {
                        "name": tool_name,
                        "arguments": decision["updatedInput"],
                    },
                },
            }
        )
        yield wire
        rpc = await self._await_response(event, slot, "mcp_message tools/call")
        mcp_response = rpc["mcp_response"]
        assert "result" in mcp_response, (
            f"in-process MCP server returned an error: {mcp_response.get('error')}"
        )
        tool_result = mcp_response["result"]
        yield _tool_result(
            block["id"],
            tool_result["content"],
            is_error=bool(tool_result.get("isError")),
        )
