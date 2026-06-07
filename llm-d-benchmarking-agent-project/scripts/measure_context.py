"""THROWAWAY measurement: assemble a realistic long quickstart-like session WITHOUT a live
LLM and print the assembled-`messages` size (chars + ~tokens) broken down by message
role/kind. Run BEFORE and AFTER the context-reduction changes to prove the reduction.

Usage:
  PYTHONPATH=<worktree> REPOS_DIR=/home/tal/kind-quickstart-guide \
    <primary venv>/bin/python scripts/measure_context.py
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.agent.loop import AgentLoop
from app.agent.prompt import build_system_prompt
from app.agent.session import Session
from app.config import Settings
from app.llm.provider import AssistantTurn, LLMProvider, ToolCall
from app.security.allowlist import Allowlist
from app.security.runner import CommandRunner
from app.tools.context import ToolContext

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST_PATH = PROJECT_ROOT / "security" / "allowlist.yaml"


def _big(label: str, n: int) -> str:
    """A large-but-realistic JSON-ish blob standing in for a fat tool result."""
    body = (label + " ") * (n // (len(label) + 1) + 1)
    return body[:n]


class ScriptedProvider(LLMProvider):
    def __init__(self, turns: list[AssistantTurn]):
        self._turns = list(turns)
        self.i = 0

    async def chat(self, *, system, messages, tools, cache_key=None) -> AssistantTurn:
        if self.i >= len(self._turns):
            return AssistantTurn(text="", tool_calls=[])
        t = self._turns[self.i]
        self.i += 1
        return t


def _kind(m: dict[str, Any]) -> str:
    role = m.get("role")
    if role == "tool_results":
        return "tool_results"
    if role == "assistant":
        return "assistant"
    if role == "user":
        if m.get("synthetic"):
            return "synthetic-user(env-snapshot)"
        content = str(m.get("content") or "")
        if content.startswith("[live catalog snapshot"):
            return "synthetic-user(catalog)"
        if content.startswith("["):
            return "synthetic-user(other-injected)"
        return "real-user"
    return role or "?"


def _msg_chars(m: dict[str, Any]) -> int:
    if m.get("role") == "tool_results":
        return sum(len(r.get("content") or "") for r in m.get("results", []))
    c = m.get("content")
    return len(c) if isinstance(c, str) else 0


def breakdown(messages: list[dict[str, Any]], system: str) -> dict[str, int]:
    out: dict[str, int] = {"system-prompt": len(system)}
    for m in messages:
        out[_kind(m)] = out.get(_kind(m), 0) + _msg_chars(m)
    return out


def _report(label: str, messages: list[dict[str, Any]], system: str) -> int:
    bd = breakdown(messages, system)
    total = sum(bd.values())
    print(f"\n=== {label} ===")
    print(f"  messages: {len(messages)}")
    for kind, chars in sorted(bd.items(), key=lambda kv: -kv[1]):
        print(f"  {kind:34s} {chars:>9,d} chars  ~{chars // 4:>7,d} tok")
    print(f"  {'TOTAL':34s} {total:>9,d} chars  ~{total // 4:>7,d} tok")
    return total


def _result(payload: dict[str, Any]) -> dict[str, Any]:
    return payload


async def main() -> None:
    settings = Settings(
        _env_file=None,
        repos_dir=Path("/home/tal/kind-quickstart-guide"),
        workspace_dir=PROJECT_ROOT / "workspace" / "_measure",
        llm_provider="anthropic",
        anthropic_api_key="not-used",
    )
    allowlist = Allowlist.from_file(ALLOWLIST_PATH)
    runner = CommandRunner(settings.repo_paths)
    ws = settings.resolved_workspace_dir / "sessions" / "measure"
    ctx = ToolContext(settings=settings, allowlist=allowlist, runner=runner, workspace=ws)
    session = Session(id="measure", ctx=ctx)
    # Seed a realistic env pre-probe snapshot (injected as a synthetic user message turn 1).
    session.env_snapshot = {"cluster_info": _big("node", 3000), "namespaces": _big("ns", 1500)}

    async def emit(t, p):  # noqa: ANN001
        pass

    async def approve(kind, payload):  # noqa: ANN001
        return True

    # A many-turn quickstart-like flow. We DON'T need real tool execution to measure the
    # transcript footprint — the loop appends each tool's result; we make the provider call
    # tools whose results are naturally large (doc fetches), and a few that repeat (re-fetch).
    def turn(text: str, *calls: ToolCall) -> AssistantTurn:
        return AssistantTurn(text=text, tool_calls=list(calls))

    # A LONG quickstart-like session (probe -> docs -> standup -> smoketest -> run -> analyze ...)
    # with many fat doc fetches + several EXACT re-fetches of the same doc, so the transcript
    # blows past the 48k compaction threshold the way a real benchmarking session does.
    flows = [
        ("benchmark a tiny chat model on my laptop",
         [turn("Reading the quickstart docs.",
               ToolCall("t1", "fetch_key_docs", {"task": "quickstart"})),
          turn("Got it.", )]),
        ("what flags does standup take again?",
         [turn("Let me re-check the quickstart docs.",  # EXACT re-fetch (lever-2 target)
               ToolCall("t2", "fetch_key_docs", {"task": "quickstart"})),
          turn("Here you go.", )]),
        ("read me the deploy playbook",
         [turn("Loading the deploy playbook.",
               ToolCall("t3", "read_knowledge", {"name": "deploy_path_playbook"})),
          turn("ok", )]),
        ("now the capacity guide",
         [turn("Loading capacity.",
               ToolCall("t4", "read_knowledge", {"name": "capacity"})),
          turn("ok", )]),
        ("re-read the deploy playbook",
         [turn("Loading it again.",  # EXACT re-read (lever-2 target)
               ToolCall("t5", "read_knowledge", {"name": "deploy_path_playbook"})),
          turn("done", )]),
        ("how do I read the results?",
         [turn("Loading the results guide.",
               ToolCall("t6", "read_knowledge", {"name": "results_interpretation"})),
          turn("ok", )]),
        ("remind me about epp headers",
         [turn("Loading epp headers.",
               ToolCall("t7", "read_knowledge", {"name": "epp_headers"})),
          turn("done", )]),
        ("and the capacity guide once more",
         [turn("Loading capacity again.",  # EXACT re-read (lever-2 target)
               ToolCall("t8", "read_knowledge", {"name": "capacity"})),
          turn("thanks", )]),
    ]

    for user_text, turns in flows:
        provider = ScriptedProvider(turns)
        await AgentLoop(provider).run_turn(session, user_text, emit=emit, request_approval=approve)

    system = build_system_prompt(ctx)
    total = _report("ASSEMBLED CONTEXT (multi-turn quickstart session)", session.messages, system)
    print(f"\nTotal approx tokens (system + transcript): ~{total // 4:,}")
    # Dump the per-message kinds so we can eyeball what compaction targets.
    print("\nMessage stream (kind / chars):")
    for i, m in enumerate(session.messages):
        print(f"  [{i:2d}] {_kind(m):34s} {_msg_chars(m):>8,d}")

    # --- Demonstrate compaction's reach on a LONG transcript -------------------------------
    # The realistic session above stays under the 48k compaction threshold (tool results are
    # clamped to 6k each), so we synthesize a long-session transcript with big OLD synthetic
    # messages + big OLD tool results to show what extended compaction reclaims.
    from app.agent.context_mgmt import compact_messages

    long_msgs: list[dict[str, Any]] = [
        {"role": "user", "synthetic": True,
         "content": "[environment pre-probe — read-only snapshot]\n" + _big("env", 8000)},
        {"role": "user",
         "content": "[live catalog snapshot — names]\n" + _big("catalog", 4000)},
    ]
    for i in range(8):
        long_msgs.append({"role": "user", "content": f"question {i}"})
        long_msgs.append({"role": "assistant", "content": "working",
                          "tool_calls": [{"id": f"c{i}", "name": "fetch_key_docs", "input": {}}]})
        long_msgs.append({"role": "tool_results",
                          "results": [{"tool_call_id": f"c{i}", "name": "fetch_key_docs",
                                       "content": _big("doc", 6000)}]})
    sys2 = system
    before = _report("LONG SESSION — BEFORE extended compaction", long_msgs, sys2)
    reclaimed = compact_messages(long_msgs)
    after = _report("LONG SESSION — AFTER extended compaction", long_msgs, sys2)
    print(f"\nCompaction reclaimed {reclaimed:,} chars (~{reclaimed // 4:,} tok). "
          f"Transcript total {before:,} -> {after:,} chars "
          f"(~{before // 4:,} -> ~{after // 4:,} tok).")
    # Prove the synthetic head messages were stubbed, recent kept.
    print("\nHead synthetic messages after compaction:")
    for i in range(2):
        print(f"  [{i}] {_kind(long_msgs[i]):34s} {_msg_chars(long_msgs[i]):>6,d}  "
              f"{str(long_msgs[i].get('content'))[:60]!r}")


if __name__ == "__main__":
    asyncio.run(main())
