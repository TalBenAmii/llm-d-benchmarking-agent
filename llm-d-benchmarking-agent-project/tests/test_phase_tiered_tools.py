"""Model-driven advanced-tool exposure + the knowledge de-inlining that go with it (token-budget
work).

The heavy late-phase tool schemas (registry._ADVANCED_TOOLS) are hidden by default and revealed
only when the model calls enable_advanced_tools — which flips session.advanced_tools_enabled, and
the agent loop re-opens the provider turn so they are callable the SAME turn. The unlock is
model-driven (not a phase gate) so it works from any entry point: an already-running stack, a pile
of prior results, or a reproduce request, with no in-session deploy. Two fat CORE knowledge files
(key_docs.yaml, deploy_path_playbook.md) are also now on-demand, not inlined. All hermetic.
"""
from __future__ import annotations

from pathlib import Path

from app.agent.prompt import ADVANCED_TOOLS_NOTE, CORE_KNOWLEDGE, build_system_prompt
from app.agent.session import Session, SessionManager
from app.config import Settings, get_settings
from app.llm.provider import AssistantTurn, ToolCall
from app.security.allowlist import Allowlist
from app.security.runner import CommandRunner
from app.tools.context import ToolContext
from app.tools.registry import _ADVANCED_TOOLS, REGISTRY, tool_definitions

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST_PATH = PROJECT_ROOT / "security" / "allowlist.yaml"

# Representative tools that must ALWAYS be present, incl. the unlock tool itself.
_CORE_ALWAYS = {"probe_environment", "list_catalog", "propose_session_plan",
                "execute_llmdbenchmark", "enable_advanced_tools"}


def _ctx(tmp_path) -> ToolContext:
    s = get_settings()
    al = Allowlist.from_file(ALLOWLIST_PATH)
    return ToolContext(settings=s, allowlist=al, runner=CommandRunner(s.repo_paths), workspace=tmp_path / "ws")


# ---- registry: the gate is additive (default = full set); the unlock tool is never gated --------

def test_advanced_tool_names_are_all_real_and_unlock_tool_is_not_gated():
    unknown = _ADVANCED_TOOLS - set(REGISTRY)
    assert not unknown, f"_ADVANCED_TOOLS names not in the registry: {unknown}"
    assert "enable_advanced_tools" in REGISTRY, "the unlock tool must be registered"
    assert "enable_advanced_tools" not in _ADVANCED_TOOLS, "the unlock tool must never gate itself"


def test_tool_definitions_default_returns_full_set():
    """No-arg (every existing caller incl. the schema/registry tests) sees ALL tools — the gate is
    opt-OUT, never silently dropping tools from unrelated callers."""
    names = {d["name"] for d in tool_definitions()}
    assert names == set(REGISTRY)
    assert names >= _ADVANCED_TOOLS


def test_tool_definitions_without_advanced_drops_exactly_the_advanced_set():
    full = {d["name"] for d in tool_definitions(include_advanced=True)}
    early = {d["name"] for d in tool_definitions(include_advanced=False)}
    assert full - early == set(_ADVANCED_TOOLS)
    # The early list keeps the core path AND the unlock tool (so the model can reveal the rest).
    assert early >= _CORE_ALWAYS
    assert not (_ADVANCED_TOOLS & early), "no advanced tool may leak into the default list"


def test_withholding_advanced_actually_saves_meaningful_schema_bytes():
    import json
    full = sum(len(json.dumps(d)) for d in tool_definitions(include_advanced=True))
    early = sum(len(json.dumps(d)) for d in tool_definitions(include_advanced=False))
    assert full - early > 20_000, "withholding advanced tools should drop a large chunk of schema"


# ---- prompt note is byte-stable and EXACTLY in sync with the registry (both directions) ---------

def test_advanced_tools_note_in_prompt(tmp_path):
    assert ADVANCED_TOOLS_NOTE in build_system_prompt(_ctx(tmp_path))


def test_note_names_exactly_the_gated_set_plus_the_unlock_tool():
    """Every gated tool is named in the note AND no non-gated tool is wrongly presented as advanced
    — a bidirectional sync guard so the note and _ADVANCED_TOOLS cannot drift apart."""
    named = {name for name in REGISTRY if name in ADVANCED_TOOLS_NOTE}
    assert named == set(_ADVANCED_TOOLS) | {"enable_advanced_tools"}


# ---- knowledge de-inlining: key_docs + deploy_path_playbook are now on-demand ------------------

def test_fat_guides_de_inlined_from_core():
    assert "key_docs.yaml" not in CORE_KNOWLEDGE
    assert "deploy_path_playbook.md" not in CORE_KNOWLEDGE


def test_de_inlined_guides_not_inlined_but_reachable(tmp_path):
    from app.tools import knowledge_access

    ctx = _ctx(tmp_path)
    prompt = build_system_prompt(ctx)
    kdir = ctx.settings.knowledge_dir
    for name, topic in [("key_docs.yaml", "key_docs"), ("deploy_path_playbook.md", "deploy_path_playbook")]:
        body = (kdir / name).read_text()
        assert body[300:480] not in prompt, f"{name} should no longer be inlined"
        assert name in prompt, f"{name} must still appear in the on-demand index"
        out = knowledge_access.read_knowledge(ctx, name=topic)
        assert out.get("content"), f"read_knowledge({topic!r}) must still return the guide"


# ---- the flag persists across save/load --------------------------------------------------------

def test_advanced_tools_enabled_survives_persist_and_load(tmp_path):
    mgr = SessionManager(Settings(workspace_dir=tmp_path),
                         Allowlist.from_file(ALLOWLIST_PATH),
                         CommandRunner(get_settings().repo_paths))
    sess = mgr.create()
    sess.messages.append({"role": "user", "content": "hi"})
    sess.advanced_tools_enabled = True
    sess.persist()
    mgr._sessions.clear()
    loaded = mgr.load(sess.id)
    assert loaded is not None and loaded.advanced_tools_enabled is True


# ---- loop: model-driven unlock reveals the advanced tools in the SAME turn ----------------------

class _CapturingProvider:
    """Scripted by call index; records the tool names handed to each chat() so we can prove the
    provider turn was re-opened with the expanded set after enable_advanced_tools."""

    def __init__(self, turns):
        self._turns = turns
        self.i = 0
        self.tool_names_per_call: list[set[str]] = []

    async def chat(self, *, system, messages, tools, cache_key=None):
        self.tool_names_per_call.append({t["name"] for t in tools})
        turn = self._turns[self.i]
        self.i += 1
        return turn


async def test_loop_reveals_advanced_tools_same_turn_after_enable(tmp_path):
    from app.agent.loop import AgentLoop

    async def emit(t, p):
        pass

    async def request_approval(kind, payload):
        return True

    session = Session(id="modeldriven", ctx=_ctx(tmp_path))
    # Step 1: model calls enable_advanced_tools. Step 2 (after the re-open): a text-only finish.
    prov = _CapturingProvider([
        AssistantTurn(text="", tool_calls=[ToolCall("c1", "enable_advanced_tools", {})]),
        AssistantTurn(text="advanced tools ready", tool_calls=[]),
    ])
    await AgentLoop(prov).run_turn(session, "my stack is up, sweep it",
                                   emit=emit, request_approval=request_approval)

    assert session.advanced_tools_enabled is True
    assert len(prov.tool_names_per_call) == 2, "the turn should have re-opened for a 2nd step"
    # 1st step: advanced tools hidden but the unlock tool present.
    assert not (_ADVANCED_TOOLS & prov.tool_names_per_call[0])
    assert "enable_advanced_tools" in prov.tool_names_per_call[0]
    # 2nd step (same user turn, after re-open): the advanced tools are now exposed.
    assert prov.tool_names_per_call[1] >= _ADVANCED_TOOLS, "advanced tools must appear the same turn"
