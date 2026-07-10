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
    estimate_context_size,
)
from app.agent.loop import AgentLoop
from app.agent.prompt import build_system_prompt, catalog_brief_message
from app.agent.session import Session, SessionManager, derive_title
from app.config import Settings, get_settings
from app.llm.provider import AssistantTurn, ToolCall, Usage
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


# ---- (3b) extended compaction: OLD large SYNTHETIC injected user messages ------------------

def _long_transcript_with_synthetic_head() -> list[dict]:
    """A long transcript (over the threshold) led by the two machine-injected synthetic user
    messages (env pre-probe snapshot + live-catalog snapshot), then enough padding + a recent
    real message that the synthetic head sits well behind the recency window."""
    msgs: list[dict] = [
        {"role": "user", "synthetic": True,
         "content": "[environment pre-probe — read-only snapshot]\n" + _big(8_000)},
        {"role": "user",
         "content": "[live catalog snapshot — names]\n" + _big(4_000)},
    ]
    # Padding turns (real user + assistant) to push past the threshold AND the recency window.
    for _ in range(_RECENT_MESSAGES_KEPT + 2):
        msgs.append({"role": "user", "content": _big(_COMPACT_THRESHOLD_CHARS // 6)})
        msgs.append({"role": "assistant", "content": "ok"})
    return msgs


def test_compaction_elides_old_synthetic_env_and_catalog():
    """The env pre-probe snapshot (flagged synthetic) AND the live-catalog snapshot (recognised
    by its bracket-tag) are both elided to a short stub once OLD — they were never elided before
    and rode along in every replay forever."""
    msgs = _long_transcript_with_synthetic_head()
    env_before = msgs[0]["content"]
    cat_before = msgs[1]["content"]

    reclaimed = compact_messages(msgs)
    assert reclaimed > 0

    env_after = msgs[0]["content"]
    cat_after = msgs[1]["content"]
    # Both are now short stubs that name how to re-derive the context.
    assert env_after.startswith(_ELIDED_PREFIX) and "probe_environment" in env_after
    assert cat_after.startswith(_ELIDED_PREFIX) and "list_catalog" in cat_after
    assert len(env_after) < len(env_before) and len(cat_after) < len(cat_before)
    # The synthetic flag is PRESERVED so title/history rendering still skips the message.
    assert msgs[0].get("synthetic") is True
    # The catalog stub still starts with '[' so main.py's history-render skip still applies.
    assert cat_after.startswith("[")


def test_compaction_never_touches_real_user_or_assistant_messages():
    """CRITICAL SAFETY: a real (typed) user message and ALL assistant messages are NEVER elided,
    even when large and old — only machine-injected synthetic user messages are eligible."""
    msgs: list[dict] = [
        # An OLD, LARGE, REAL user message (no synthetic flag, not a known injected tag) and an
        # OLD large assistant message — both must survive verbatim.
        {"role": "user", "content": _big(_ELIDE_OVER_CHARS + 5_000)},
        {"role": "assistant", "content": _big(_ELIDE_OVER_CHARS + 5_000)},
    ]
    real_user_before = msgs[0]["content"]
    assistant_before = msgs[1]["content"]
    # Pad past the threshold + window with synthetic + filler so compaction definitely runs.
    msgs.insert(0, {"role": "user", "synthetic": True,
                    "content": "[environment pre-probe — snapshot]\n" + _big(_COMPACT_THRESHOLD_CHARS)})
    for _ in range(_RECENT_MESSAGES_KEPT + 2):
        msgs.append({"role": "user", "content": "recent"})

    compact_messages(msgs)

    # The real user + assistant messages are untouched (the synthetic head was elided instead).
    assert msgs[1]["content"] == real_user_before, "a REAL user message must never be elided"
    assert msgs[2]["content"] == assistant_before, "an assistant message must never be elided"
    assert msgs[0]["content"].startswith(_ELIDED_PREFIX), "the synthetic head should be elided"


def test_compaction_keeps_recent_synthetic_message():
    """A synthetic message INSIDE the recency window is kept verbatim (the agent may still be
    acting on the just-injected snapshot)."""
    msgs: list[dict] = [{"role": "user", "content": _big(_COMPACT_THRESHOLD_CHARS + 1_000)}]
    msgs.append({"role": "user", "synthetic": True,
                 "content": "[environment pre-probe — snapshot]\n" + _big(_ELIDE_OVER_CHARS + 2_000)})
    recent = msgs[-1]["content"]
    compact_messages(msgs)
    assert msgs[-1]["content"] == recent, "a recent synthetic message must be kept verbatim"


def test_compaction_synthetic_elision_is_idempotent():
    msgs = _long_transcript_with_synthetic_head()
    first = compact_messages(msgs)
    assert first > 0
    second = compact_messages(msgs)
    assert second == 0, "re-compacting elided synthetic messages must be a no-op"


def test_compaction_stays_under_a_bound_after_many_turns():
    """After many turns of big OLD synthetic + tool-result blobs, compaction keeps the elidable
    OLD content bounded — every old synthetic message + old large tool result becomes a stub.
    (REAL user/assistant messages are intentionally NOT counted here; they are never elided and
    are kept SMALL in this fixture so the bound isolates the elidable blobs.)"""
    # Build the head from big SYNTHETIC blobs + small real padding (so the only large OLD content
    # is elidable: the synthetic head and the spliced tool results).
    msgs: list[dict] = [
        {"role": "user", "synthetic": True,
         "content": "[environment pre-probe — read-only snapshot]\n" + _big(_ELIDE_OVER_CHARS + 8_000)},
        {"role": "user",
         "content": "[live catalog snapshot — names]\n" + _big(_ELIDE_OVER_CHARS + 4_000)},
    ]
    for i in range(6):
        msgs.append({"role": "assistant", "content": "x",
                     "tool_calls": [{"id": f"o{i}", "name": "fetch_key_docs", "input": {}}]})
        msgs.append({"role": "tool_results",
                     "results": [{"tool_call_id": f"o{i}", "name": "fetch_key_docs",
                                  "content": _big(_ELIDE_OVER_CHARS + 6_000)}]})
    # Small real padding to push past the threshold + the recency window (kept SMALL so it isn't
    # part of the bound under test — only elidable blobs are).
    for _ in range(_RECENT_MESSAGES_KEPT + 2):
        msgs.append({"role": "user", "content": "recent"})

    total_before = sum(
        (sum(len(r.get("content") or "") for r in m.get("results", []))
         if m.get("role") == "tool_results"
         else len(m.get("content") or "")) for m in msgs
    )
    assert total_before > _COMPACT_THRESHOLD_CHARS  # compaction will run
    compact_messages(msgs)

    # Sum the OLD (pre-window) ELIDABLE content: synthetic user messages + tool results. After
    # compaction each is a small stub, so their total collapses far under one un-elided blob.
    keep_from = max(0, len(msgs) - _RECENT_MESSAGES_KEPT)
    elidable_old = 0
    for m in msgs[:keep_from]:
        if m.get("role") == "tool_results":
            elidable_old += sum(len(r.get("content") or "") for r in m.get("results", []))
        elif m.get("synthetic") or str(m.get("content") or "").startswith(_ELIDED_PREFIX):
            elidable_old += len(m.get("content") or "")
    assert elidable_old < 4_000, f"elidable old region not bounded after compaction: {elidable_old}"


# ---- (3c) context-size estimate (debugging token usage) ------------------------------------

def test_estimate_context_size_breakdown_and_totals():
    system = "S" * 4_000
    messages = [
        {"role": "user", "content": "u" * 400},
        {"role": "assistant", "content": "a" * 200},
        {"role": "tool_results", "results": [{"tool_call_id": "x", "name": "t", "content": "r" * 1_200}]},
    ]
    est = estimate_context_size(system, messages)
    # char/4 estimate, exact arithmetic.
    assert est["system_chars"] == 4_000 and est["system_tokens_est"] == 1_000
    history = 400 + 200 + 1_200
    assert est["history_chars"] == history
    assert est["total_chars"] == 4_000 + history
    assert est["total_tokens_est"] == (4_000 + history) // 4
    # The last tool result is surfaced as the per-turn spike.
    assert est["last_tool_result_chars"] == 1_200


async def test_loop_usage_event_carries_context_estimate(tmp_path):
    """The per-call USAGE event includes a context_est breakdown (system/history/last-tool-result)
    so the UI can show context growth — a non-zero, internally-consistent estimate."""
    session = _session(tmp_path)
    captured: list[tuple[str, dict]] = []

    async def emit(t, p):
        captured.append((t, p))

    async def request_approval(kind, payload):
        return True

    await AgentLoop(_ScriptedProvider([AssistantTurn(text="hi", tool_calls=[])])).run_turn(
        session, "hello", emit=emit, request_approval=request_approval)

    ue = [p for (t, p) in captured if t == events.USAGE][-1]
    ctx_est = ue["context_est"]
    assert ctx_est["system_chars"] > 0, "the cached system prompt should dominate a tiny turn"
    assert ctx_est["total_chars"] == ctx_est["system_chars"] + ctx_est["history_chars"]
    assert ctx_est["total_tokens_est"] == ctx_est["total_chars"] // 4


# ---- (3d) compaction re-fires MID-TURN, not only once at turn start ------------------------

async def test_compaction_runs_mid_turn_when_a_long_turn_crosses_the_threshold(tmp_path, monkeypatch):
    """A single ``run_turn`` replays the WHOLE transcript on EVERY step, and one long multi-step
    turn can append many large tool results — so the replayed context can blow far past the
    compaction threshold WITHIN the turn. Compaction must therefore be re-evaluated before each
    step, not only once at turn start: otherwise the very mechanism meant to bound the transcript
    is structurally unable to fire while the turn that overflows it is still running, and the
    growing history is re-sent in full to the provider every step (eventually a context-overflow
    error).

    Reproduction: the model calls a tool every step for many steps; each tool result is
    budget-sized. The transcript starts tiny (below the threshold, so the start-of-turn compaction
    is a no-op) and crosses the threshold only several steps in. The OLD large tool results must
    be elided to stubs by end-of-turn — proving compaction ran AFTER the transcript grew, i.e.
    mid-turn.

    Fails before the fix (compaction is called once, before the loop, so the early results are
    never elided and the transcript ends far over the threshold); passes after (compaction is
    re-checked each step).
    """
    session = _session(tmp_path)

    # Drive a tool call per step for enough steps that the replayed transcript crosses the
    # threshold partway through, then a final text-only step ends the turn.
    n_steps = 20
    turns = [AssistantTurn(text="step", tool_calls=[ToolCall(f"c{i}", "list_catalog", {})])
             for i in range(n_steps)]
    turns.append(AssistantTurn(text="done", tool_calls=[]))

    # Each tool result is large (budget-sized) so a handful of steps push the transcript over the
    # threshold. dispatch is patched so the test is fully hermetic (no real tool / repo).
    big = "B" * 6_000

    async def fake_dispatch(ctx, name, raw_input):
        return {"data": big}

    monkeypatch.setattr("app.agent.loop.dispatch", fake_dispatch)

    async def emit(t, p):
        pass

    async def request_approval(kind, payload):
        return True

    await AgentLoop(_ScriptedProvider(turns)).run_turn(
        session, "go", emit=emit, request_approval=request_approval)

    # OLD large tool results (those behind the recency window at end-of-turn) must have been
    # elided to stubs — which can only happen if compaction ran AFTER the transcript grew past
    # the threshold, i.e. mid-turn. Count elided vs un-elided OLD tool results.
    keep_from = max(0, len(session.messages) - _RECENT_MESSAGES_KEPT)
    old_results = [r for m in session.messages[:keep_from] if m.get("role") == "tool_results"
                   for r in m["results"]]
    assert old_results, "the long turn should have produced tool results behind the recency window"
    elided = [r for r in old_results if r["content"].startswith(_ELIDED_PREFIX)]
    assert elided, (
        "old large tool results were never compacted: compaction only ran once at turn start, "
        "before the transcript grew — it must be re-evaluated mid-turn"
    )


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


# ---- (5) per-session doc de-duplication (knowledge_access) ---------------------------------

def test_read_knowledge_dedups_exact_repeat_within_session(tmp_path):
    """The FIRST read of a guide returns full content; an EXACT repeat in the SAME session returns
    a short 'already_provided' back-reference (not the full text again)."""
    from app.tools.access import knowledge_access

    ctx = _ctx(tmp_path)
    first = knowledge_access.read_knowledge(ctx, name="analysis")
    assert "content" in first and len(first["content"]) > 0
    second = knowledge_access.read_knowledge(ctx, name="analysis")
    assert second.get("already_provided") is True
    assert "content" not in second
    assert "analysis" in second.get("topic", "") or "analysis" in str(second.get("note", ""))


def test_read_knowledge_dedup_is_per_session(tmp_path):
    """De-dup is scoped to one session: a DIFFERENT ctx (a new/resumed session) gets full content
    on its first read."""
    from app.tools.access import knowledge_access

    ctx_a = _ctx(tmp_path / "a")
    ctx_b = _ctx(tmp_path / "b")
    knowledge_access.read_knowledge(ctx_a, name="analysis")  # prime A
    # A different session must still get the full text on its first read.
    out_b = knowledge_access.read_knowledge(ctx_b, name="analysis")
    assert "content" in out_b and len(out_b["content"]) > 0


def test_different_docs_are_not_deduped(tmp_path):
    """Only EXACT repeats are short-circuited — two different guides each return full content."""
    from app.tools.access import knowledge_access

    ctx = _ctx(tmp_path)
    a = knowledge_access.read_knowledge(ctx, name="analysis")
    b = knowledge_access.read_knowledge(ctx, name="preconditions")
    assert "content" in a and "content" in b


def test_fetch_key_docs_dedups_repeat_doc_body(tmp_path):
    """fetch_key_docs returns each doc's full body on the first fetch and omits the body (keeping
    the metadata + an already_provided marker) on an EXACT repeat in the same session."""
    from app.tools.access import knowledge_access

    ctx = _ctx(tmp_path)
    first = knowledge_access.fetch_key_docs(ctx, task="quickstart")
    # Re-fetch the same task: any doc whose body was already sent should now be marked, not re-sent.
    second = knowledge_access.fetch_key_docs(ctx, task="quickstart")
    found_first = [d for d in first.get("docs", []) if d.get("found") and "content" in d]
    if found_first:  # only meaningful when the quickstart doc actually resolved on disk
        # At least one doc that had content on the first fetch is now an already_provided marker.
        provided = [d for d in second.get("docs", []) if d.get("already_provided")]
        assert provided, "a repeated key-doc fetch should mark already-provided docs"
        assert all("content" not in d for d in provided)


# ---- scripted provider ---------------------------------------------------------------------

class _ScriptedProvider:
    def __init__(self, turns):
        self._turns = turns
        self.i = 0

    async def chat(self, *, system, messages, tools, cache_key=None):
        turn = self._turns[self.i]
        self.i += 1
        return turn
