"""Token-bloat fixes (TODO #11/#4): cache-prefix stability, one-shot live-catalog injection,
and old-tool-result compaction — plus a guard on the usage-total accounting.

All hermetic (no network, no live cluster, no repo dependency).
"""
from __future__ import annotations

from pathlib import Path

from app.agent import events
from app.agent.context_mgmt import (
    _COMPACT_THRESHOLD_CHARS,
    _ELIDE_OVER_CHARS,
    _ELIDED_PREFIX,
    _RECENT_MESSAGES_KEPT,
    compact_messages,
)
from app.agent.loop import AgentLoop
from app.agent.prompt import build_system_prompt, catalog_brief_message
from app.agent.session import Session, SessionManager, derive_title
from app.config import Settings, get_settings
from app.llm.provider import AssistantTurn, Usage
from app.security.allowlist import Allowlist
from app.security.runner import CommandRunner
from app.tools.context import ToolContext

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST_PATH = PROJECT_ROOT / "security" / "allowlist.yaml"


def _ctx(tmp_path) -> ToolContext:
    s = get_settings()
    al = Allowlist.from_file(ALLOWLIST_PATH)
    runner = CommandRunner(s.repo_paths)
    return ToolContext(settings=s, allowlist=al, runner=runner, workspace=tmp_path / "ws")


def _session(tmp_path) -> Session:
    return Session(id="ctxmgmt", ctx=_ctx(tmp_path))


# ---- (1) cache-prefix stability: the live catalog is OUT of the cached system prefix -------

def test_system_prompt_has_no_live_catalog_body(tool_ctx):
    """The dynamic catalog must NOT be inlined into the system prefix (it would break the
    cache prefix). Only the byte-stable pointer is present."""
    prompt = build_system_prompt(tool_ctx)
    # The pointer is present; the actual rendered specs/harnesses listing is not.
    assert "list_catalog" in prompt
    assert "[live catalog snapshot" in prompt or "Live catalog" in prompt
    # The catalog-brief body (the "harnesses: ..." line _catalog_brief renders) is NOT inlined.
    brief = catalog_brief_message(tool_ctx)
    # The brief's authoritative body line must not appear verbatim in the system prefix.
    body_lines = [ln for ln in brief.splitlines() if ln.startswith("specs:") or ln.startswith("harnesses:")]
    for ln in body_lines:
        assert ln not in prompt, f"catalog body line leaked into the cached system prefix: {ln!r}"


def test_system_prompt_is_byte_stable_across_calls(tool_ctx):
    """The whole point of the fix: the system prefix is identical on repeated builds (no
    per-turn dynamic content), so the provider reliably cache-hits it."""
    assert build_system_prompt(tool_ctx) == build_system_prompt(tool_ctx)


# ---- (2) the live catalog is injected ONCE as a synthetic conversation message -------------

async def test_loop_injects_catalog_message_once(tmp_path):
    session = _session(tmp_path)

    async def emit(t, p):
        pass

    async def request_approval(kind, payload):
        return True

    # A text-only turn ends after one LLM call.
    prov1 = _ScriptedProvider([AssistantTurn(text="ok", tool_calls=[])])
    await AgentLoop(prov1).run_turn(session, "hello", emit=emit, request_approval=request_approval)

    catalog_msgs = [m for m in session.messages
                    if m.get("role") == "user" and "[live catalog snapshot" in str(m.get("content", ""))]
    assert len(catalog_msgs) == 1, "catalog must be injected exactly once"
    assert session.catalog_injected is True

    # Second turn: no second catalog message.
    prov2 = _ScriptedProvider([AssistantTurn(text="again", tool_calls=[])])
    await AgentLoop(prov2).run_turn(session, "more", emit=emit, request_approval=request_approval)
    catalog_msgs = [m for m in session.messages
                    if m.get("role") == "user" and "[live catalog snapshot" in str(m.get("content", ""))]
    assert len(catalog_msgs) == 1, "catalog must NOT be re-injected on later turns"


def test_catalog_injected_flag_survives_persist_and_load(tmp_path):
    al = Allowlist.from_file(ALLOWLIST_PATH)
    runner = CommandRunner(get_settings().repo_paths)
    mgr = SessionManager(Settings(workspace_dir=tmp_path), al, runner)
    sess = mgr.create()
    sess.messages.append({"role": "user", "content": "hi"})
    sess.catalog_injected = True
    sess.persist()
    mgr._sessions.clear()
    loaded = mgr.load(sess.id)
    assert loaded is not None
    assert loaded.catalog_injected is True


def test_title_skips_synthetic_injected_messages():
    """The chat title comes from the first REAL user message, not the injected catalog/pre-probe
    snapshot messages (which both start with a bracket tag)."""
    msgs = [
        {"role": "user", "content": "[environment pre-probe — snapshot]\n{...}"},
        {"role": "user", "content": "[live catalog snapshot — names]\nspecs: cicd/kind"},
        {"role": "user", "content": "benchmark a tiny chat model"},
    ]
    assert derive_title(msgs) == "benchmark a tiny chat model"


def test_old_state_json_without_catalog_flag_defaults_false(tmp_path):
    import json
    al = Allowlist.from_file(ALLOWLIST_PATH)
    runner = CommandRunner(get_settings().repo_paths)
    mgr = SessionManager(Settings(workspace_dir=tmp_path), al, runner)
    sess = mgr.create()
    sess.messages.append({"role": "user", "content": "hi"})
    sess.persist()
    state = mgr._root / sess.id / "state.json"
    data = json.loads(state.read_text())
    data.pop("catalog_injected", None)
    state.write_text(json.dumps(data))
    mgr._sessions.clear()
    loaded = mgr.load(sess.id)
    assert loaded is not None
    assert loaded.catalog_injected is False


# ---- (3) compaction correctness ------------------------------------------------------------

def _big(n: int) -> str:
    return "x" * n


def _transcript_with_old_big_result() -> list[dict]:
    """A transcript whose total content is over the compaction threshold, with one LARGE old
    tool result well behind the recent window."""
    msgs: list[dict] = []
    # An old assistant tool_call + its big result, far behind the window.
    msgs.append({"role": "assistant", "content": "probing", "tool_calls": [{"id": "old1", "name": "probe_environment", "input": {}}]})
    msgs.append({"role": "tool_results", "results": [{"tool_call_id": "old1", "name": "probe_environment", "content": _big(_ELIDE_OVER_CHARS + 5_000)}]})
    # Padding turns to push the total over the threshold AND the big result behind the window.
    for _ in range(_RECENT_MESSAGES_KEPT + 2):
        msgs.append({"role": "user", "content": _big(_COMPACT_THRESHOLD_CHARS // 4)})
    return msgs


def test_compaction_no_op_below_threshold():
    msgs = [
        {"role": "user", "content": "small"},
        {"role": "tool_results", "results": [{"tool_call_id": "a", "name": "probe", "content": _big(_ELIDE_OVER_CHARS + 100)}]},
    ]
    before = [dict(m) for m in msgs]
    reclaimed = compact_messages(msgs)
    assert reclaimed == 0
    assert msgs[1]["results"][0]["content"] == before[1]["results"][0]["content"]


def test_compaction_elides_old_large_result_and_keeps_pairing():
    msgs = _transcript_with_old_big_result()
    n_msgs_before = len(msgs)
    n_results_before = len(msgs[1]["results"])
    old_content = msgs[1]["results"][0]["content"]

    reclaimed = compact_messages(msgs)

    assert reclaimed > 0
    # PAIRING preserved: same number of messages, same number of results, same tool_call_id.
    assert len(msgs) == n_msgs_before
    assert len(msgs[1]["results"]) == n_results_before
    assert msgs[1]["results"][0]["tool_call_id"] == "old1"
    # The old big result is now a short stub mentioning the tool + the original size.
    new_content = msgs[1]["results"][0]["content"]
    assert new_content.startswith(_ELIDED_PREFIX)
    assert "probe_environment" in new_content
    assert len(new_content) < len(old_content)
    # The matching assistant tool_call is untouched.
    assert msgs[0]["tool_calls"][0]["id"] == "old1"


def test_compaction_preserves_recent_window():
    """A LARGE tool result inside the recent window is NOT elided (the agent may still need it
    mid-task)."""
    msgs: list[dict] = [{"role": "user", "content": _big(_COMPACT_THRESHOLD_CHARS + 1_000)}]
    # A recent big tool result, within the trailing window.
    msgs.append({"role": "assistant", "content": "x", "tool_calls": [{"id": "r1", "name": "run", "input": {}}]})
    msgs.append({"role": "tool_results", "results": [{"tool_call_id": "r1", "name": "run", "content": _big(_ELIDE_OVER_CHARS + 2_000)}]})
    recent_content = msgs[-1]["results"][0]["content"]
    compact_messages(msgs)
    assert msgs[-1]["results"][0]["content"] == recent_content, "recent results must be kept verbatim"


def test_compaction_is_idempotent():
    msgs = _transcript_with_old_big_result()
    first = compact_messages(msgs)
    assert first > 0
    second = compact_messages(msgs)
    assert second == 0, "re-compacting an already-compacted transcript must be a no-op"


# ---- (4) usage-total accounting guard (recon flagged loop.py ~95) --------------------------

async def test_loop_usage_total_equals_all_billed_tokens(tmp_path):
    """turn['total'] and session['total'] must each equal EVERY billed token for the turn:
    input + output + cache_read + cache_write (no double-count, no omission)."""
    session = _session(tmp_path)
    captured: list[tuple[str, dict]] = []

    async def emit(t, p):
        captured.append((t, p))

    async def request_approval(kind, payload):
        return True

    u = Usage(input_tokens=200, output_tokens=33, cache_read_tokens=5_000, cache_write_tokens=120)
    await AgentLoop(_ScriptedProvider([AssistantTurn(text="done", tool_calls=[], usage=u)])).run_turn(
        session, "go", emit=emit, request_approval=request_approval)

    ue = [p for (t, p) in captured if t == events.USAGE][-1]
    expected = u.input_tokens + u.output_tokens + u.cache_read_tokens + u.cache_write_tokens
    assert ue["turn"]["total"] == expected
    assert ue["session"]["total"] == expected
    # And the components reported separately add up to the same total.
    t = ue["turn"]
    assert t["input"] + t["output"] + t["cache_read"] + t["cache_write"] == t["total"]


# ---- scripted provider ---------------------------------------------------------------------

class _ScriptedProvider:
    def __init__(self, turns):
        self._turns = turns
        self.i = 0

    async def chat(self, *, system, messages, tools, cache_key=None):
        turn = self._turns[self.i]
        self.i += 1
        return turn
