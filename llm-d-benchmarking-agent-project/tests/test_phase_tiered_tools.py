"""Model-driven phase-group tool loading + the knowledge de-inlining that go with it (token-budget
work).

Most tool schemas are hidden behind named GROUPS (registry._TOOL_GROUPS: setup/run/analyze/
advanced); only the STARTER_KIT is shown by default. The model calls load_tools(['<group>']) when a
request needs a grouped tool — the agent loop folds the group(s) into session.loaded_groups and
re-opens the provider turn so the group's tools are callable the SAME turn. The unlock is
model-driven (not a phase gate) so it works from any entry point: an already-running stack, a pile
of prior results, or a reproduce request, with no in-session deploy. Two fat CORE knowledge files
(key_docs.yaml, deploy_path_playbook.md) are also now on-demand, not inlined. All hermetic.
"""
from __future__ import annotations

from pathlib import Path
from typing import get_args

from app.agent.prompt import CORE_KNOWLEDGE, GROUP_CATALOG_NOTE, build_system_prompt
from app.agent.session import Session, SessionManager
from app.config import Settings, get_settings
from app.llm.provider import AssistantTurn, ToolCall
from app.security.allowlist import Allowlist
from app.security.runner import CommandRunner
from app.tools.registry import _GROUPED_TOOLS, _TOOL_GROUPS, REGISTRY, STARTER_KIT, tool_definitions
from app.tools.schemas import LoadToolsInput

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST_PATH = PROJECT_ROOT / "security" / "allowlist.yaml"

# Representative tools that must ALWAYS be present (starter kit), incl. the loader tool itself.
_CORE_ALWAYS = {"probe_environment", "list_catalog", "propose_session_plan", "load_tools"}


# ---- registry: groups partition the non-starter tools; the loader tool is never grouped ---------

def test_group_tool_names_are_all_real_and_loader_is_starter_kit():
    unknown = _GROUPED_TOOLS - set(REGISTRY)
    assert not unknown, f"_TOOL_GROUPS names not in the registry: {unknown}"
    assert "load_tools" in REGISTRY, "the loader tool must be registered"
    assert "load_tools" in STARTER_KIT, "the loader tool must be always-resident"
    assert "load_tools" not in _GROUPED_TOOLS, "the loader tool must never gate itself"


def test_starter_kit_and_groups_partition_the_registry():
    # Every tool is either starter-kit OR in exactly one group — no overlap, no gaps.
    assert set(REGISTRY) == (STARTER_KIT | _GROUPED_TOOLS)
    assert not (STARTER_KIT & _GROUPED_TOOLS)
    members = [m for g in _TOOL_GROUPS.values() for m in g]
    assert len(members) == len(set(members)), "a tool appears in more than one group"


def test_tool_definitions_default_returns_full_set():
    """No-arg (every existing caller incl. the schema/registry tests) sees ALL tools — loading is
    opt-IN, never silently dropping tools from unrelated callers."""
    names = {d["name"] for d in tool_definitions()}
    assert names == set(REGISTRY)
    assert names >= _GROUPED_TOOLS


def test_tool_definitions_with_no_groups_loaded_is_exactly_the_starter_kit():
    names = {d["name"] for d in tool_definitions(loaded=frozenset())}
    assert names == set(STARTER_KIT)
    assert names >= _CORE_ALWAYS
    assert not (_GROUPED_TOOLS & names), "no grouped tool may leak into the default list"


def test_loading_a_group_adds_exactly_that_group():
    starter = {d["name"] for d in tool_definitions(loaded=frozenset())}
    for group, members in _TOOL_GROUPS.items():
        names = {d["name"] for d in tool_definitions(loaded=frozenset({group}))}
        assert names - starter == set(members), f"loading {group!r} should add exactly its members"


def test_withholding_groups_actually_saves_meaningful_schema_bytes():
    import json
    full = sum(len(json.dumps(d)) for d in tool_definitions())
    starter = sum(len(json.dumps(d)) for d in tool_definitions(loaded=frozenset()))
    assert full - starter > 20_000, "withholding grouped tools should drop a large chunk of schema"


# ---- prompt note is byte-stable and EXACTLY in sync with the groups (both directions) -----------

def test_group_catalog_note_in_prompt(tool_ctx):
    assert GROUP_CATALOG_NOTE in build_system_prompt(tool_ctx)


def test_note_names_every_grouped_tool_and_no_starter_tool_as_grouped():
    """Every grouped tool is named in the note AND no starter-kit tool is wrongly listed as
    grouped — a bidirectional sync guard so the note and _TOOL_GROUPS cannot drift apart."""
    named = {name for name in REGISTRY if name in GROUP_CATALOG_NOTE}
    # load_tools is named in the note's prose (how to load) but is starter-kit, so allow it.
    assert named - {"load_tools"} == _GROUPED_TOOLS
    # Every group NAME is also named in the note.
    for group in _TOOL_GROUPS:
        assert group in GROUP_CATALOG_NOTE


def test_note_lists_each_tool_under_its_own_group_heading():
    """Per-GROUP membership in the note must match _TOOL_GROUPS — not just the flat union — so a
    tool listed under the WRONG heading (telling the model to load the wrong group) is caught."""
    import re

    headings = list(_TOOL_GROUPS)
    pos = {}
    for g in headings:
        m = re.search(rf"(?m)^- {re.escape(g)} \(", GROUP_CATALOG_NOTE)
        assert m, f"group {g!r} has no '- {g} (...)' bullet in the note"
        pos[g] = m.start()
    ordered = sorted(headings, key=lambda g: pos[g])
    for i, g in enumerate(ordered):
        end = pos[ordered[i + 1]] if i + 1 < len(ordered) else len(GROUP_CATALOG_NOTE)
        # Tool names in this heading's segment (identifier tokens; blurb words can't collide with
        # the compound tool names).
        seg_tokens = set(re.findall(r"[a-z_]+", GROUP_CATALOG_NOTE[pos[g]:end]))
        assert _TOOL_GROUPS[g] <= seg_tokens, (
            f"{g} heading is missing tools {_TOOL_GROUPS[g] - seg_tokens}")
        for other in headings:
            if other != g:
                stray = _TOOL_GROUPS[other] & seg_tokens
                assert not stray, f"{g} heading wrongly lists {other}'s tools {stray}"


def test_loadtools_literal_groups_in_sync_with_registry():
    """LoadToolsInput's Literal group names (schema-level validation) must match _TOOL_GROUPS keys
    exactly, so the model is validated against the real groups."""
    ann = LoadToolsInput.model_fields["groups"].annotation  # list[Literal[...]]
    inner = get_args(ann)[0]  # Literal['setup','run','analyze','advanced']
    assert set(get_args(inner)) == set(_TOOL_GROUPS)


# ---- knowledge de-inlining: key_docs + deploy_path_playbook are now on-demand ------------------

def test_fat_guides_de_inlined_from_core():
    assert "key_docs.yaml" not in CORE_KNOWLEDGE
    assert "deploy_path_playbook.md" not in CORE_KNOWLEDGE
    # The kind runbook is now on-demand too (served by fetch_key_docs(task="quickstart") + gated).
    assert "quickstart_playbook.md" not in CORE_KNOWLEDGE


def test_de_inlined_guides_not_inlined_but_reachable(tool_ctx):
    from app.tools.access import knowledge_access

    ctx = tool_ctx
    prompt = build_system_prompt(ctx)
    kdir = ctx.settings.knowledge_dir
    for name, topic in [("key_docs.yaml", "key_docs"),
                        ("deploy_path_playbook.md", "deploy_path_playbook"),
                        ("quickstart_playbook.md", "quickstart_playbook")]:
        body = next(kdir.rglob(name)).read_text()
        assert body[300:480] not in prompt, f"{name} should no longer be inlined"
        assert name in prompt, f"{name} must still appear in the on-demand index"
        out = knowledge_access.read_knowledge(ctx, name=topic)
        assert out.get("content"), f"read_knowledge({topic!r}) must still return the guide"


# ---- the loaded-groups set persists across save/load, and the old flag migrates -----------------

def test_loaded_groups_survive_persist_and_load(tmp_path):
    mgr = SessionManager(Settings(workspace_dir=tmp_path),
                         Allowlist.from_file(ALLOWLIST_PATH),
                         CommandRunner(get_settings().repo_paths))
    sess = mgr.create()
    sess.messages.append({"role": "user", "content": "hi"})
    sess.loaded_groups.update({"run", "analyze"})
    sess.persist()
    mgr._sessions.clear()
    loaded = mgr.load(sess.id)
    assert loaded is not None and loaded.loaded_groups == {"run", "analyze"}


def test_pre_feature_advanced_flag_migrates_to_advanced_group(tmp_path):
    """A state.json saved before this feature carried a boolean advanced_tools_enabled; on load it
    must migrate to the 'advanced' group so a resumed advanced workflow keeps its tools."""
    import json
    mgr = SessionManager(Settings(workspace_dir=tmp_path),
                         Allowlist.from_file(ALLOWLIST_PATH),
                         CommandRunner(get_settings().repo_paths))
    sess = mgr.create()
    sess.persist()
    # Hand-write the legacy shape into the snapshot, then reload.
    state = sess.ctx.workspace / "state.json"
    data = json.loads(state.read_text())
    data.pop("loaded_groups", None)
    data["advanced_tools_enabled"] = True
    state.write_text(json.dumps(data))
    mgr._sessions.clear()
    loaded = mgr.load(sess.id)
    assert loaded is not None and loaded.loaded_groups == {"advanced"}


# ---- the load_tools handler de-dupes + echoes the loaded groups --------------------------------

async def test_load_tools_handler_dedupes_and_echoes_groups():
    from app.tools import tool_loader

    # ctx is unused by the handler; a repeated group is de-duped, order preserved.
    out = await tool_loader.load_tools(None, groups=["setup", "run", "setup"])
    assert out["loaded"] == ["setup", "run"]
    assert "setup" in out["note"] and "run" in out["note"]


# ---- loop: model-driven load reveals the group's tools in the SAME turn -------------------------

class _CapturingProvider:
    """Scripted by call index; records the tool names handed to each chat() so we can prove the
    provider turn was re-opened with the expanded set after load_tools."""

    def __init__(self, turns):
        self._turns = turns
        self.i = 0
        self.tool_names_per_call: list[set[str]] = []

    async def chat(self, *, system, messages, tools, cache_key=None):
        self.tool_names_per_call.append({t["name"] for t in tools})
        turn = self._turns[self.i]
        self.i += 1
        return turn


async def test_loop_reveals_group_same_turn_after_load_tools(tool_ctx):
    from app.agent.loop import AgentLoop

    async def emit(t, p):
        pass

    async def request_approval(kind, payload):
        return True

    session = Session(id="modeldriven", ctx=tool_ctx)
    # Step 1: model calls load_tools(['advanced']). Step 2 (after the re-open): a text-only finish.
    prov = _CapturingProvider([
        AssistantTurn(text="", tool_calls=[ToolCall("c1", "load_tools", {"groups": ["advanced"]})]),
        AssistantTurn(text="advanced tools ready", tool_calls=[]),
    ])
    await AgentLoop(prov).run_turn(session, "my stack is up, sweep it",
                                   emit=emit, request_approval=request_approval)

    assert session.loaded_groups == {"advanced"}
    assert len(prov.tool_names_per_call) == 2, "the turn should have re-opened for a 2nd step"
    # 1st step: the advanced group hidden but the loader tool present.
    assert not (_TOOL_GROUPS["advanced"] & prov.tool_names_per_call[0])
    assert "load_tools" in prov.tool_names_per_call[0]
    # 2nd step (same user turn, after re-open): the advanced group's tools are now exposed.
    assert prov.tool_names_per_call[1] >= _TOOL_GROUPS["advanced"], "group must appear the same turn"
