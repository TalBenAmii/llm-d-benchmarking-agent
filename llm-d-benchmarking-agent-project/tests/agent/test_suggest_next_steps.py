"""suggest_next_steps — the agent offers its "what next?" choices as clickable buttons.

The agent stops asking "want me to…?" in prose and instead CALLS suggest_next_steps with
{label, prompt} options; the UI draws them as the same floating pills as the welcome chips and
clicking one sends its prompt. These tests pin the mechanism: the tool is registered, validated
(1-6 well-formed items), returns the chip payload, and is on the card-replay path so the buttons
survive a resume/reload (and never spawn a spurious results_card). The end-to-end half drives
the real engine over the FakeTransport.
"""
from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from app.agent.cards import build_results_card
from app.agent.engine import SdkNativeEngine, steer
from app.agent.session import Session
from app.tools.mcp_server import CARD_RESULT_TOOLS, TOOL_PREFIX
from app.tools.registry import REGISTRY, ToolSpec, dispatch, tool_definitions
from app.tools.schemas import SuggestNextStepsInput
from tests._helpers import _capture_ctx
from tests._sdk_fake import FakeTransport, assistant, result, text, tool_use

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
    seven = [{"label": f"L{i}", "prompt": f"do thing {i}"} for i in range(7)]
    result = await dispatch(tool_ctx, "suggest_next_steps", {"suggestions": seven})
    assert result.get("error") == "invalid arguments"


async def test_dispatch_rejects_missing_fields(tool_ctx):
    # Missing prompt → schema rejects (each chip needs BOTH a label and a prompt).
    r1 = await dispatch(tool_ctx, "suggest_next_steps", {"suggestions": [{"label": "x"}]})
    assert r1.get("error") == "invalid arguments"
    # Missing label.
    r2 = await dispatch(tool_ctx, "suggest_next_steps", {"suggestions": [{"prompt": "x"}]})
    assert r2.get("error") == "invalid arguments"


def test_schema_bounds_label_and_requires_nonempty():
    # 1-6 items, label 1..48 chars, prompt non-empty.
    SuggestNextStepsInput.model_validate({"suggestions": [_OK]})  # min boundary OK
    SuggestNextStepsInput.model_validate(
        {"suggestions": [_OK, _OK2, _OK, _OK2, _OK, _OK2]}  # max boundary OK (6)
    )
    with pytest.raises(ValidationError):  # 7 items → over the cap
        SuggestNextStepsInput.model_validate({"suggestions": [_OK] * 7})
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


# ---- end-to-end through the engine (scripted FakeTransport) ------------------

def _run_pieces(tmp_path):
    ctx, _runner = _capture_ctx(tmp_path)
    session = Session(id="chips", ctx=ctx, catalog_injected=True)
    events: list[tuple[str, dict]] = []

    async def emit(t, p):
        events.append((t, p))

    async def no_gate(kind, payload):  # never called — this tool is not gated
        raise AssertionError("suggest_next_steps must NOT raise an approval gate")

    return session, events, emit, no_gate


async def test_engine_emits_buttons_and_persists_them_for_replay(tmp_path):
    """When the model CALLS suggest_next_steps, the engine streams a tool_result carrying the
    chip payload (the live UI render) AND records it to session.card_results (so it replays on
    reload), while emitting NO results_card (it carries no metrics)."""
    session, events, emit, no_gate = _run_pieces(tmp_path)
    script = [[
        assistant(text("Here's where you can go next:"),
                  tool_use("c1", TOOL_PREFIX + "suggest_next_steps",
                           {"suggestions": [_OK, _OK2]})),
        result(),
    ]]
    engine = SdkNativeEngine(transport_factory=lambda: FakeTransport(script))
    await engine.run_turn(session, "summarize my run", emit=emit, request_approval=no_gate)

    # The chips were streamed to the UI on the tool_result (the live render path).
    tr = [p for (t, p) in events if t == "tool_result" and p["name"] == "suggest_next_steps"]
    assert tr and tr[0]["result"]["suggestions"] == [_OK, _OK2]
    # No results_card (suggest_next_steps carries no metrics).
    assert not any(t == "results_card" for (t, _p) in events)
    # Persisted on the card-replay path so the buttons survive a resume/reload.
    persisted = [c for c in session.card_results if c.get("name") == "suggest_next_steps"]
    assert persisted and persisted[0]["result"]["suggestions"] == [_OK, _OK2]


async def test_suggest_next_steps_is_terminal_no_trailing_closer(tmp_path):
    """suggest_next_steps ENDS the turn: any trailing model text is exactly the redundant "use
    the buttons below" closer the buttons replace, so the engine suppresses it (regression for
    session bceaecb766eb, where the agent narrated the buttons twice)."""
    session, events, emit, no_gate = _run_pieces(tmp_path)
    _CLOSER = "Use the buttons below to choose your next step."
    script = [[
        assistant(tool_use("c1", TOOL_PREFIX + "suggest_next_steps",
                           {"suggestions": [_OK, _OK2]})),
        assistant(text(_CLOSER)),
        result(),
    ]]
    engine = SdkNativeEngine(transport_factory=lambda: FakeTransport(script))
    await engine.run_turn(session, "summarize my run", emit=emit, request_approval=no_gate)

    # The closer was never surfaced — no assistant_text carries it.
    assert not any(t == "assistant_text" and _CLOSER in (p.get("text") or "")
                   for (t, p) in events)
    # The buttons were still shown.
    tr = [p for (t, p) in events if t == "tool_result" and p["name"] == "suggest_next_steps"]
    assert tr and tr[0]["result"]["suggestions"] == [_OK, _OK2]


class _EmptyInput(BaseModel):
    pass


async def test_waiting_steer_outranks_the_terminal_offer(tmp_path, monkeypatch):
    """A mid-turn user STEER outranks the terminal-offer stop: if the user typed something while
    the turn ran, the follow-up response answers it un-suppressed instead of the turn parking on
    the buttons."""
    session, events, emit, no_gate = _run_pieces(tmp_path)

    async def steer_probe(ctx):
        # Simulates the WS handler queueing a steer while this turn is mid-tool.
        assert steer(session.id, "actually, compare to my last run instead") is True
        return {"ok": True}

    monkeypatch.setitem(
        REGISTRY, "steer_probe",
        ToolSpec("steer_probe", "queues a steer", _EmptyInput, steer_probe))
    script = [
        [
            assistant(tool_use("s1", TOOL_PREFIX + "steer_probe", {})),
            assistant(tool_use("c1", TOOL_PREFIX + "suggest_next_steps",
                               {"suggestions": [_OK, _OK2]})),
            assistant(text("Use the buttons below.")),   # suppressed: chips are terminal
            result(),
        ],
        [assistant(text("Sure — comparing now.")), result()],
    ]
    engine = SdkNativeEngine(transport_factory=lambda: FakeTransport(script))
    await engine.run_turn(session, "summarize my run", emit=emit, request_approval=no_gate)

    # The steered follow-up was answered UN-suppressed, after the chips.
    assert any(t == "assistant_text" and "comparing now" in (p.get("text") or "")
               for (t, p) in events)
    # The closer inside the chips response stayed suppressed.
    assert not any(t == "assistant_text" and "buttons below" in (p.get("text") or "")
                   for (t, p) in events)
    # One app-level turn: a single done event.
    assert [t for t, _ in events].count("done") == 1
