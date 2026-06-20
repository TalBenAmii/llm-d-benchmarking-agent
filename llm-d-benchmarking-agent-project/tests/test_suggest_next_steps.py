"""suggest_next_steps — the agent offers its "what next?" choices as clickable buttons.

The agent stops asking "want me to…?" in prose and instead CALLS suggest_next_steps with
{label, prompt} options; the UI draws them as the same floating pills as the welcome chips and
clicking one sends its prompt. These tests pin the mechanism: the tool is registered, validated
(1-4 well-formed items), returns the chip payload, and is on the card-replay path so the buttons
survive a resume/reload (and never spawn a spurious results_card).
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agent.loop import CARD_RESULT_TOOLS
from app.agent.results_card import build_results_card
from app.tools.registry import dispatch, tool_definitions
from app.tools.schemas import SuggestNextStepsInput

_OK = {"label": "Save as baseline", "prompt": "Save this run as my baseline so we can trend it"}
_OK2 = {"label": "Compare to last run", "prompt": "Compare this run against my previous one"}


def test_suggest_next_steps_in_tool_definitions():
    defs = {d["name"]: d for d in tool_definitions()}
    assert "suggest_next_steps" in defs
    spec = defs["suggest_next_steps"]
    assert spec["description"] and spec["input_schema"]["type"] == "object"
    # The description must steer the model OFF prose offers and ONTO buttons.
    assert "button" in spec["description"].lower()


async def test_dispatch_returns_the_chip_payload(tool_ctx):
    result = await dispatch(tool_ctx, "suggest_next_steps", {"suggestions": [_OK, _OK2]})
    assert result["count"] == 2
    assert result["suggestions"] == [_OK, _OK2]
    # A note tells the model the buttons are shown so it doesn't also recite them in prose.
    assert result.get("shown") is True and "prose" in result.get("note", "").lower()


async def test_dispatch_rejects_empty_list(tool_ctx):
    result = await dispatch(tool_ctx, "suggest_next_steps", {"suggestions": []})
    assert result.get("error") == "invalid arguments"


async def test_dispatch_rejects_too_many(tool_ctx):
    five = [{"label": f"L{i}", "prompt": f"do thing {i}"} for i in range(5)]
    result = await dispatch(tool_ctx, "suggest_next_steps", {"suggestions": five})
    assert result.get("error") == "invalid arguments"


async def test_dispatch_rejects_missing_fields(tool_ctx):
    # Missing prompt → schema rejects (each chip needs BOTH a label and a prompt).
    r1 = await dispatch(tool_ctx, "suggest_next_steps", {"suggestions": [{"label": "x"}]})
    assert r1.get("error") == "invalid arguments"
    # Missing label.
    r2 = await dispatch(tool_ctx, "suggest_next_steps", {"suggestions": [{"prompt": "x"}]})
    assert r2.get("error") == "invalid arguments"


def test_schema_bounds_label_and_requires_nonempty():
    # 1-4 items, label 1..48 chars, prompt non-empty.
    SuggestNextStepsInput.model_validate({"suggestions": [_OK]})  # min boundary OK
    SuggestNextStepsInput.model_validate({"suggestions": [_OK, _OK2, _OK, _OK2]})  # max boundary OK
    with pytest.raises(ValidationError):
        SuggestNextStepsInput.model_validate({"suggestions": [{"label": "x" * 49, "prompt": "p"}]})
    with pytest.raises(ValidationError):
        SuggestNextStepsInput.model_validate({"suggestions": [{"label": "", "prompt": "p"}]})
    with pytest.raises(ValidationError):
        SuggestNextStepsInput.model_validate({"suggestions": [{"label": "ok", "prompt": ""}]})


def test_on_card_replay_path_but_yields_no_results_card():
    # In CARD_RESULT_TOOLS → its result is persisted + replayed (the buttons survive reload)…
    assert "suggest_next_steps" in CARD_RESULT_TOOLS
    # …but it is NOT a metrics card, so build_results_card must not manufacture a results_card for it.
    assert build_results_card("suggest_next_steps", {"suggestions": [_OK], "count": 1}) is None


# ---- end-to-end through the agent loop (scripted fake provider) --------------

async def test_loop_emits_buttons_and_persists_them_for_replay(tmp_path):
    """When the model CALLS suggest_next_steps, the loop streams a tool_result carrying the chip
    payload (the live UI render) AND records it to session.card_results (so it replays on reload),
    while emitting NO results_card (it carries no metrics)."""
    from pathlib import Path

    from app.agent.loop import AgentLoop
    from app.agent.session import Session
    from app.config import get_settings
    from app.llm.provider import AssistantTurn, ToolCall
    from app.security.allowlist import Allowlist
    from app.security.runner import CommandRunner
    from app.tools.context import ToolContext

    project_root = Path(__file__).resolve().parents[1]

    class FakeProvider:
        def __init__(self, turns):
            self._turns, self.i = turns, 0

        async def chat(self, *, system, messages, tools, cache_key=None):
            turn = self._turns[self.i]
            self.i += 1
            return turn

    s = get_settings()
    al = Allowlist.from_file(project_root / "security" / "allowlist.yaml")
    ctx = ToolContext(settings=s, allowlist=al, runner=CommandRunner(s.repo_paths),
                      workspace=tmp_path / "ws")
    session = Session(id="t", ctx=ctx)

    turns = [
        AssistantTurn(text="Here's where you can go next:", tool_calls=[
            ToolCall("c1", "suggest_next_steps", {"suggestions": [_OK, _OK2]})]),
        AssistantTurn(text="", tool_calls=[]),  # nothing more to do → turn ends
    ]
    events: list[tuple[str, dict]] = []

    async def emit(t, p):
        events.append((t, p))

    async def request_approval(kind, payload):  # never called — this tool is not gated
        raise AssertionError("suggest_next_steps must NOT raise an approval gate")

    await AgentLoop(FakeProvider(turns)).run_turn(
        session, "summarize my run", emit=emit, request_approval=request_approval)

    # The chips were streamed to the UI on the tool_result (the live render path).
    tr = [p for (t, p) in events if t == "tool_result" and p["name"] == "suggest_next_steps"]
    assert tr and tr[0]["result"]["suggestions"] == [_OK, _OK2]
    # No results_card (suggest_next_steps carries no metrics).
    assert not any(t == "results_card" for (t, _p) in events)
    # Persisted on the card-replay path so the buttons survive a resume/reload.
    persisted = [c for c in session.card_results if c.get("name") == "suggest_next_steps"]
    assert persisted and persisted[0]["result"]["suggestions"] == [_OK, _OK2]


def _loop_harness(tmp_path):
    """Build the (FakeProvider-driving) pieces shared by the terminal-behavior tests."""
    from pathlib import Path

    from app.agent.session import Session
    from app.config import get_settings
    from app.security.allowlist import Allowlist
    from app.security.runner import CommandRunner
    from app.tools.context import ToolContext

    project_root = Path(__file__).resolve().parents[1]
    s = get_settings()
    al = Allowlist.from_file(project_root / "security" / "allowlist.yaml")
    ctx = ToolContext(settings=s, allowlist=al, runner=CommandRunner(s.repo_paths),
                      workspace=tmp_path / "ws")
    return Session(id="t", ctx=ctx), ctx


async def test_suggest_next_steps_is_terminal_no_trailing_closer(tmp_path):
    """suggest_next_steps ENDS the turn: the loop must NOT make another LLM call after it, so the
    model never gets a step to append a redundant "use the buttons below" closer. Regression for
    session bceaecb766eb, where the agent narrated the buttons twice (a lead-in AND a closer)."""
    from app.agent.loop import AgentLoop
    from app.llm.provider import AssistantTurn, ToolCall

    session, _ctx = _loop_harness(tmp_path)

    # Turn 2 is a closer the loop must NEVER reach — if it does, this text would be emitted.
    _CLOSER = "Use the buttons below to choose your next step."
    turns = [
        AssistantTurn(text="", tool_calls=[
            ToolCall("c1", "suggest_next_steps", {"suggestions": [_OK, _OK2]})]),
        AssistantTurn(text=_CLOSER, tool_calls=[]),
    ]

    class FakeProvider:
        def __init__(self, turns):
            self._turns, self.i = turns, 0

        async def chat(self, *, system, messages, tools, cache_key=None):
            turn = self._turns[self.i]
            self.i += 1
            return turn

    provider = FakeProvider(turns)
    events: list[tuple[str, dict]] = []

    async def emit(t, p):
        events.append((t, p))

    async def request_approval(kind, payload):
        raise AssertionError("suggest_next_steps must NOT raise an approval gate")

    await AgentLoop(provider).run_turn(
        session, "summarize my run", emit=emit, request_approval=request_approval)

    # The loop stopped right after the tool: exactly ONE LLM call, the second turn untouched.
    assert provider.i == 1
    # The closer was never produced — no assistant_text carries it.
    assert not any(t == "assistant_text" and _CLOSER in (p.get("text") or "")
                   for (t, p) in events)
    # The buttons were still shown.
    tr = [p for (t, p) in events if t == "tool_result" and p["name"] == "suggest_next_steps"]
    assert tr and tr[0]["result"]["suggestions"] == [_OK, _OK2]


async def test_waiting_steer_outranks_the_terminal_offer(tmp_path):
    """A mid-turn user STEER outranks the terminal-offer stop: if the user typed something while
    the turn ran, the loop keeps going to answer it instead of parking on the buttons."""
    from app.agent.loop import AgentLoop
    from app.llm.provider import AssistantTurn, ToolCall

    session, ctx = _loop_harness(tmp_path)

    turns = [
        AssistantTurn(text="", tool_calls=[
            ToolCall("c1", "suggest_next_steps", {"suggestions": [_OK, _OK2]})]),
        AssistantTurn(text="Sure — comparing now.", tool_calls=[]),
    ]

    class FakeProviderWithSteer:
        def __init__(self, turns, ctx):
            self._turns, self.i, self._ctx = turns, 0, ctx

        async def chat(self, *, system, messages, tools, cache_key=None):
            # Simulate the user typing mid-turn: the WS handler would drop it into steer_messages.
            if self.i == 0:
                self._ctx.steer_messages = ["actually, compare to my last run instead"]
            turn = self._turns[self.i]
            self.i += 1
            return turn

    provider = FakeProviderWithSteer(turns, ctx)
    events: list[tuple[str, dict]] = []

    async def emit(t, p):
        events.append((t, p))

    async def request_approval(kind, payload):
        raise AssertionError("suggest_next_steps must NOT raise an approval gate")

    await AgentLoop(provider).run_turn(
        session, "summarize my run", emit=emit, request_approval=request_approval)

    # The loop did NOT stop on the offer — it ran a second step to answer the steer.
    assert provider.i == 2
    assert any(t == "assistant_text" and "comparing now" in (p.get("text") or "")
               for (t, p) in events)
    assert not ctx.steer_messages  # the steer was drained
