"""Conformance tests: FakeTransport drives the SDK's real protocol machinery.

Proves a real ClaudeSDKClient(transport=FakeTransport(...)) completes the
initialize handshake, parses scripted turns into typed messages, bridges
can_use_tool to the registered callback, and executes REAL in-process MCP
tool handlers (create_sdk_mcp_server) on the fake's mcp_message requests.
"""

import inspect
import json

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
    tool,
)
from claude_agent_sdk._internal.transport import Transport

from tests._sdk_fake import FakeTransport, assistant, result, stream_event, text, tool_use


def _toy_server(calls: list[str]):
    @tool("bump", "Record a value", {"value": str})
    async def bump(args):
        calls.append(args["value"])
        return {"content": [{"type": "text", "text": f"recorded {args['value']}"}]}

    return create_sdk_mcp_server("toy", tools=[bump])


async def test_text_only_turn():
    fake = FakeTransport([[assistant(text("hello there")), result()]])
    async with ClaudeSDKClient(options=ClaudeAgentOptions(), transport=fake) as client:
        assert await client.get_server_info() is not None  # initialize completed
        await client.query("hi")
        messages = [m async for m in client.receive_response()]

    assert isinstance(messages[0], AssistantMessage)
    assert isinstance(messages[0].content[0], TextBlock)
    assert messages[0].content[0].text == "hello there"
    assert isinstance(messages[-1], ResultMessage)
    assert fake.user_messages[0]["message"]["content"] == "hi"


async def test_tool_use_allow_runs_real_handler():
    calls: list[str] = []
    seen: list[tuple[str, dict]] = []

    async def approve(name, tool_input, _context):
        seen.append((name, tool_input))
        return PermissionResultAllow()

    script = [
        [
            assistant(tool_use("tu_1", "mcp__toy__bump", {"value": "v1"})),
            assistant(text("tool done")),
            result(),
        ]
    ]
    fake = FakeTransport(script)
    options = ClaudeAgentOptions(
        mcp_servers={"toy": _toy_server(calls)}, can_use_tool=approve
    )
    async with ClaudeSDKClient(options=options, transport=fake) as client:
        await client.query("run the tool")
        messages = [m async for m in client.receive_response()]

    assert calls == ["v1"]  # the REAL handler ran, in-process
    assert seen == [("mcp__toy__bump", {"value": "v1"})]

    tool_uses = [
        b
        for m in messages
        if isinstance(m, AssistantMessage)
        for b in m.content
        if isinstance(b, ToolUseBlock)
    ]
    assert tool_uses[0].name == "mcp__toy__bump"

    tool_results = [
        b
        for m in messages
        if isinstance(m, UserMessage)
        for b in m.content
        if isinstance(b, ToolResultBlock)
    ]
    assert tool_results[0].tool_use_id == "tu_1"
    assert tool_results[0].content == [{"type": "text", "text": "recorded v1"}]
    assert tool_results[0].is_error is False

    # Query fills updatedInput with the original input on a plain allow —
    # the fake (like the real CLI) calls the handler with exactly this.
    assert fake.permission_responses == [
        {"behavior": "allow", "updatedInput": {"value": "v1"}}
    ]
    assert isinstance(messages[-1], ResultMessage)


async def test_tool_use_deny_skips_handler_and_continues():
    calls: list[str] = []

    async def deny(_name, _tool_input, _context):
        return PermissionResultDeny(message="not now")

    script = [
        [
            assistant(tool_use("tu_1", "mcp__toy__bump", {"value": "v1"})),
            assistant(text("moving on")),
            result(),
        ]
    ]
    fake = FakeTransport(script)
    options = ClaudeAgentOptions(
        mcp_servers={"toy": _toy_server(calls)}, can_use_tool=deny
    )
    async with ClaudeSDKClient(options=options, transport=fake) as client:
        await client.query("run the tool")
        messages = [m async for m in client.receive_response()]

    assert calls == []  # handler did NOT run
    assert fake.permission_responses == [{"behavior": "deny", "message": "not now"}]

    tool_results = [
        b
        for m in messages
        if isinstance(m, UserMessage)
        for b in m.content
        if isinstance(b, ToolResultBlock)
    ]
    assert tool_results[0].is_error is True
    assert tool_results[0].content == "not now"

    texts = [
        b.text
        for m in messages
        if isinstance(m, AssistantMessage)
        for b in m.content
        if isinstance(b, TextBlock)
    ]
    assert texts == ["moving on"]  # scripted flow continued past the deny
    assert isinstance(messages[-1], ResultMessage)


async def test_multi_turn_same_transport():
    fake = FakeTransport(
        [
            [assistant(text("first")), result()],
            [assistant(text("second")), result()],
        ]
    )
    async with ClaudeSDKClient(options=ClaudeAgentOptions(), transport=fake) as client:
        await client.query("one")
        first = [m async for m in client.receive_response()]
        await client.query("two")
        second = [m async for m in client.receive_response()]

    assert first[0].content[0].text == "first"
    assert second[0].content[0].text == "second"
    assert [m["message"]["content"] for m in fake.user_messages] == ["one", "two"]


async def test_scripted_stream_event_passthrough():
    event = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "he"}}
    fake = FakeTransport([[stream_event(event), assistant(text("hello")), result()]])
    async with ClaudeSDKClient(options=ClaudeAgentOptions(), transport=fake) as client:
        await client.query("hi")
        messages = [m async for m in client.receive_response()]

    assert isinstance(messages[0], StreamEvent)
    assert messages[0].event == event


async def test_script_exhaustion_fails_loudly():
    fake = FakeTransport([])
    async with ClaudeSDKClient(options=ClaudeAgentOptions(), transport=fake) as client:
        with pytest.raises(AssertionError, match="script exhausted"):
            await client.query("hi")


async def test_unknown_control_response_fails_loudly():
    fake = FakeTransport([])
    await fake.connect()
    bogus = {
        "type": "control_response",
        "response": {"subtype": "success", "request_id": "nope", "response": {}},
    }
    with pytest.raises(AssertionError, match="unknown"):
        await fake.write(json.dumps(bogus))


def test_protocol_canary():
    """Import-time assertions on the SDK surface FakeTransport relies on.

    An SDK upgrade that renames/reshapes any of this must fail HERE, loudly,
    not as a hang or a silently-skipped handler in the eval harness.
    """
    from claude_agent_sdk._internal.query import Query
    from claude_agent_sdk.types import (
        SDKControlMcpMessageRequest,
        SDKControlPermissionRequest,
        SDKControlRequest,
        SDKControlResponse,
    )

    assert set(Transport.__abstractmethods__) == {
        "connect",
        "write",
        "read_messages",
        "close",
        "is_ready",
        "end_input",
    }

    assert {"tool_name", "input", "tool_use_id"} <= set(
        SDKControlPermissionRequest.__annotations__
    )
    assert {"server_name", "message"} <= set(
        SDKControlMcpMessageRequest.__annotations__
    )
    assert set(SDKControlRequest.__annotations__) == {"type", "request_id", "request"}
    assert set(SDKControlResponse.__annotations__) == {"type", "response"}

    # Query must still route the exact control subtypes the fake emits, and
    # still fill updatedInput on allow (the fake calls the handler with it).
    handler_src = inspect.getsource(Query._handle_control_request)
    for subtype in ("can_use_tool", "hook_callback", "mcp_message"):
        assert f'subtype == "{subtype}"' in handler_src
    assert "updatedInput" in handler_src

    # The in-process MCP bridge must still execute tools/call via the real
    # registered handler.
    mcp_src = inspect.getsource(Query._handle_sdk_mcp_request)
    assert '"tools/call"' in mcp_src or "'tools/call'" in mcp_src
    assert "request_handlers" in mcp_src
