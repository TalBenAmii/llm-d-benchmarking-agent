"""Tests for the generic run_command tool, the fetch_key_docs context tool, the vetted
install_prereqs.sh prerequisite installer, the UPSTREAM llm-d guide client-prereq installer
install-deps.sh, and the per-cluster install_metrics_server.sh installer (allowlist + runner
wiring)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.tools import command, probe
from app.tools.context import ApprovalRejected, ToolError
from app.tools.registry import dispatch

# ---- run_command (the generic allowlisted-command tool) -------------------

async def test_run_command_denies_non_allowlisted(tool_ctx):
    with pytest.raises(ToolError):
        await command.run_command(tool_ctx, argv=["rm", "-rf", "/"])


async def test_run_command_denies_bad_cluster_name(tool_ctx):
    with pytest.raises(ToolError):
        await command.run_command(tool_ctx, argv=["kind", "create", "cluster", "--name", "Bad_Name"])


async def test_run_command_mutating_requires_approval(tool_ctx):
    # A valid mutating command must hit the approval gate; rejecting it raises (no exec).
    async def reject(kind, payload):
        assert kind == "command"
        return False

    tool_ctx.request_approval = reject
    with pytest.raises(ApprovalRejected):
        await command.run_command(tool_ctx, argv=["kind", "create", "cluster", "--name", "llmd-quickstart"])


async def test_run_command_install_prereqs_requires_approval(tool_ctx):
    # Installing prerequisites is mutating — it must route through the approval gate too.
    async def reject(kind, payload):
        return False

    tool_ctx.request_approval = reject
    with pytest.raises(ApprovalRejected):
        await command.run_command(tool_ctx, argv=["install_prereqs.sh", "--all"])


async def test_run_command_install_deps_requires_approval(tool_ctx):
    # The UPSTREAM llm-d guide client-prereq installer is mutating — approval-gated too.
    async def reject(kind, payload):
        return False

    tool_ctx.request_approval = reject
    with pytest.raises(ApprovalRejected):
        await command.run_command(tool_ctx, argv=["install-deps.sh"])


async def test_run_command_install_metrics_server_requires_approval(tool_ctx):
    # The per-cluster metrics-server installer is mutating — it must route through approval too.
    async def reject(kind, payload):
        return False

    tool_ctx.request_approval = reject
    with pytest.raises(ApprovalRejected):
        await command.run_command(tool_ctx, argv=["install_metrics_server.sh", "--kubelet-insecure-tls"])


async def test_run_command_schema_requires_argv(tool_ctx):
    result = await dispatch(tool_ctx, "run_command", {})
    assert result.get("error") == "invalid arguments"


def test_install_prereqs_resolves_to_executable_project_script(tool_ctx):
    # The `project-script` runner invoke type must resolve install_prereqs.sh to the real
    # file shipped with the agent project — present and executable.
    entry = tool_ctx.allowlist.executable("install_prereqs.sh")
    real, cwd = tool_ctx.runner.resolve(["install_prereqs.sh", "--all"], entry)
    script = Path(real[0])
    assert script.name == "install_prereqs.sh"
    assert script.is_file() and os.access(script, os.X_OK)
    assert real[1:] == ["--all"]


def test_install_metrics_server_resolves_to_executable_project_script(tool_ctx):
    # The metrics-server installer is also a vetted `project-script`: it must resolve to the
    # real, executable file shipped with the agent project, flags passed through verbatim.
    entry = tool_ctx.allowlist.executable("install_metrics_server.sh")
    real, cwd = tool_ctx.runner.resolve(
        ["install_metrics_server.sh", "--kubelet-insecure-tls"], entry
    )
    script = Path(real[0])
    assert script.name == "install_metrics_server.sh"
    assert script.is_file() and os.access(script, os.X_OK)
    assert real[1:] == ["--kubelet-insecure-tls"]


def test_install_deps_resolves_to_upstream_guide_script(tool_ctx):
    # The `repo-script` runner invoke type must resolve install-deps.sh to the REAL upstream
    # script under the read-only llm-d guide repo, with cwd pinned to that repo root.
    if not tool_ctx.settings.guide_repo.is_dir():
        pytest.skip("guide repo (llm-d) not present")
    entry = tool_ctx.allowlist.executable("install-deps.sh")
    real, cwd = tool_ctx.runner.resolve(["install-deps.sh", "--dev"], entry)
    script = Path(real[0])
    assert script.name == "install-deps.sh"
    assert script.parts[-3:] == ("helpers", "client-setup", "install-deps.sh")
    assert script.is_file()
    assert cwd == str(tool_ctx.settings.guide_repo)
    assert real[1:] == ["--dev"]


# ---- fetch_key_docs (hard-coded pointers, live content) -------------------

def test_fetch_key_docs_lists_available_tasks(tool_ctx):
    out = probe.fetch_key_docs(tool_ctx, task="__none__")
    assert "quickstart" in out["available_tasks"]
    assert out["docs"] == []  # no doc has that task


def test_fetch_key_docs_quickstart(tool_ctx):
    if not tool_ctx.settings.bench_repo.is_dir():
        pytest.skip("bench repo not present")
    out = probe.fetch_key_docs(tool_ctx, task="quickstart")
    assert out["task"] == "quickstart"
    assert all(d["task"] == "quickstart" for d in out["docs"])
    # The quickstart doc must resolve and carry real content.
    qs = next((d for d in out["docs"] if d["path"].endswith("docs/quickstart.md")), None)
    assert qs is not None and qs["found"] and "kind" in qs["content"].lower()


# ---- read_knowledge (hybrid: core inline + rest on-demand) ----------------

def test_read_knowledge_returns_content_for_valid_topic(tool_ctx):
    out = probe.read_knowledge(tool_ctx, name="capacity")
    assert out["name"] == "capacity.md"
    assert out["topic"] == "capacity"
    # The on-demand guide must come back with its real, full content.
    expected = (tool_ctx.settings.knowledge_dir / "capacity.md").read_text()
    assert out["content"] == expected
    assert "capacity" in out["content"].lower()


def test_read_knowledge_accepts_full_basename(tool_ctx):
    out = probe.read_knowledge(tool_ctx, name="analysis.md")
    assert out["name"] == "analysis.md"
    assert out["content"]


def test_read_knowledge_rejects_unknown_name(tool_ctx):
    out = probe.read_knowledge(tool_ctx, name="does_not_exist")
    assert "error" in out
    # The error must list valid topics so the model can self-correct.
    assert "capacity.md" in out["valid_topics"]


@pytest.mark.parametrize("evil", [
    "../config.py",
    "../../etc/passwd",
    "/etc/passwd",
    "knowledge/capacity.md",
    "..",
])
def test_read_knowledge_rejects_path_traversal(tool_ctx, evil):
    out = probe.read_knowledge(tool_ctx, name=evil)
    assert "error" in out and "content" not in out
    assert "valid_topics" in out


async def test_read_knowledge_in_tool_definitions_and_dispatch(tool_ctx):
    from app.tools.registry import tool_definitions

    names = {d["name"] for d in tool_definitions()}
    assert "read_knowledge" in names
    # End-to-end via dispatch: valid topic returns content.
    result = await dispatch(tool_ctx, "read_knowledge", {"name": "history"})
    assert result["name"] == "history.md" and result["content"]


# ---- search_knowledge (lexical search across knowledge/ + repo-doc index) ----

def test_search_knowledge_ranks_relevant_guide_first(tool_ctx):
    # A capacity/fit query should rank the capacity guide at the top via name + heading hits.
    out = probe.search_knowledge(tool_ctx, query="will the model fit in gpu memory capacity")
    assert out["match_count"] >= 1
    top = out["results"][0]
    assert top["kind"] == "knowledge"
    assert top["topic"] == "capacity"
    # Every result carries a ready-to-use load hint and a non-empty snippet.
    assert top["load_with"] == "read_knowledge('capacity')"
    assert top["snippet"]
    assert top["matched_terms"]


def test_search_knowledge_finds_doc_without_exact_basename(tool_ctx):
    # The user describes a symptom in their own words (not a basename); the gateway readiness
    # guide should still surface.
    out = probe.search_knowledge(tool_ctx, query="gateway programmed false traffic cannot reach pods")
    topics = [r["topic"] for r in out["results"] if r["kind"] == "knowledge"]
    assert "gateway_readiness" in topics


def test_search_knowledge_includes_repo_doc_pointers(tool_ctx):
    out = probe.search_knowledge(tool_ctx, query="quickstart kind cpu only deploy")
    repo_hits = [r for r in out["results"] if r["kind"] == "repo_doc"]
    assert repo_hits, "expected at least one curated repo-doc pointer"
    ptr = repo_hits[0]
    assert ptr["path"].startswith(("llm-d/", "llm-d-benchmark/"))
    assert ptr["load_with"] == f"read_repo_doc('{ptr['path']}')"


def test_search_knowledge_can_exclude_repo_docs(tool_ctx):
    out = probe.search_knowledge(
        tool_ctx, query="quickstart kind cpu only deploy", include_repo_docs=False
    )
    assert all(r["kind"] == "knowledge" for r in out["results"])


def test_search_knowledge_respects_limit_and_is_deterministic(tool_ctx):
    out1 = probe.search_knowledge(tool_ctx, query="benchmark results report metrics", limit=3)
    assert len(out1["results"]) <= 3
    # Same query → byte-identical ranking (no model call, stable tie-break).
    out2 = probe.search_knowledge(tool_ctx, query="benchmark results report metrics", limit=3)
    assert out1["results"] == out2["results"]


def test_search_knowledge_empty_and_stopword_only_query(tool_ctx):
    assert "error" in probe.search_knowledge(tool_ctx, query="   ")
    # A query of only filler words has no searchable terms.
    assert "error" in probe.search_knowledge(tool_ctx, query="how do i the a an")


def test_search_knowledge_no_match_returns_empty_results(tool_ctx):
    out = probe.search_knowledge(tool_ctx, query="zzzznevernevermatchqqqq")
    assert out["match_count"] == 0
    assert out["results"] == []
    # Still hands back the valid topic list so the model can fall back to browsing.
    assert "capacity" in out["valid_topics"]


async def test_search_knowledge_in_tool_definitions_and_dispatch(tool_ctx):
    from app.tools.registry import tool_definitions

    names = {d["name"] for d in tool_definitions()}
    assert "search_knowledge" in names
    result = await dispatch(tool_ctx, "search_knowledge", {"query": "lower harness cpu kind node"})
    assert "results" in result and result["query"]
    # harness_sizing.md should be among the hits for this troubleshooting phrasing.
    topics = [r.get("topic") for r in result["results"]]
    assert "harness_sizing" in topics


async def test_search_knowledge_dispatch_requires_query(tool_ctx):
    result = await dispatch(tool_ctx, "search_knowledge", {})
    assert result.get("error") == "invalid arguments"


def test_search_knowledge_when_to_use_is_in_knowledge(tool_ctx):
    # Judgment (WHEN to reach for it) lives in knowledge/, not in Python branches.
    style = (tool_ctx.settings.knowledge_dir / "conversation_style.md").read_text()
    assert "search_knowledge" in style


# ---- build_system_prompt: core inline + on-demand index ------------------

def test_system_prompt_inlines_core_and_indexes_on_demand(tool_ctx):
    from app.agent.prompt import CORE_KNOWLEDGE, build_system_prompt

    prompt = build_system_prompt(tool_ctx)
    kdir = tool_ctx.settings.knowledge_dir

    # (a) Each CORE file's actual body must be inlined verbatim.
    for name in CORE_KNOWLEDGE:
        body = (kdir / name).read_text()
        # A distinctive mid-file slice (skips the heading shared with the index line).
        chunk = body[120:300]
        assert chunk and chunk in prompt, f"core file {name} not inlined"

    # (b) On-demand files: their FULL body is NOT inlined, but their name IS in the index,
    # and the index tells the model to call read_knowledge.
    on_demand = ["multi_harness.md", "capacity.md", "analysis.md", "sweep_playbook.md",
                 "history.md", "observability.md", "packaging.md", "orchestrator.md"]
    assert 'read_knowledge("<topic>")' in prompt
    for name in on_demand:
        body = (kdir / name).read_text()
        chunk = body[300:480]
        assert chunk and chunk not in prompt, f"on-demand file {name} should NOT be inlined"
        assert name in prompt, f"on-demand file {name} missing from the index"


def test_autotune_strategy_is_on_demand_not_core(tool_ctx):
    # The autotuner's JUDGMENT lives in an ON-DEMAND knowledge file (goal-seeking is mid/late
    # session) — it must be loadable by read_knowledge but NOT inlined into the always-on prompt.
    from app.agent.prompt import build_system_prompt

    out = probe.read_knowledge(tool_ctx, name="autotune_strategy")
    assert out["name"] == "autotune_strategy.md" and out["content"]
    prompt = build_system_prompt(tool_ctx)
    body = (tool_ctx.settings.knowledge_dir / "autotune_strategy.md").read_text()
    assert body[300:480] not in prompt, "autotune_strategy must NOT be inlined (it's on-demand, not CORE)"
    assert "autotune_strategy" in prompt, "autotune_strategy must appear in the on-demand index"


async def test_autotune_search_dispatch_status_facts_only(tool_ctx):
    # End-to-end via dispatch: a status read on an empty search returns FACTS and NO verdict.
    out = await dispatch(tool_ctx, "autotune_search", {
        "action": "status", "search_id": "fresh",
        "slo": {"ttft_ms": 300, "percentile": "p95"},
        "objective": "output_token_rate", "direction": "max", "budget": 6})
    assert "converged" not in out
    assert out["trials_used"] == 0 and out["best_feasible"] is None


def test_multi_harness_full_body_absent_but_indexed(tool_ctx):
    # Explicit single-file assertion required by the task: the multi_harness body is gone
    # from the prompt, yet its name still appears (so the model can load it on demand).
    from app.agent.prompt import build_system_prompt

    prompt = build_system_prompt(tool_ctx)
    body = (tool_ctx.settings.knowledge_dir / "multi_harness.md").read_text()
    # The bulk of the file (everything past the first heading) is not present.
    assert body[200:] not in prompt
    assert "multi_harness" in prompt
