"""Tests for the fetch_key_docs context tool, the vetted install_prereqs.sh prerequisite
installer, the UPSTREAM llm-d guide client-prereq installer install-deps.sh, and the
per-cluster install_metrics_server.sh installer (allowlist + runner resolution wiring — these
scripts stay allowlisted; the agent now invokes them via run_shell)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.tools.knowledge_access import fetch_key_docs, read_knowledge, search_knowledge
from app.tools.registry import dispatch

# ---- allowlist + runner resolution for the vetted install scripts ----------

@pytest.mark.parametrize("script_name,flag", [
    ("install_prereqs.sh", "--all"),
    ("install_metrics_server.sh", "--kubelet-insecure-tls"),
])
def test_vetted_installer_resolves_to_executable_project_script(tool_ctx, script_name, flag):
    # The `project-script` runner invoke type must resolve each vetted installer to the real,
    # executable file shipped with the agent project, with flags passed through verbatim.
    entry = tool_ctx.allowlist.executable(script_name)
    real, cwd = tool_ctx.runner.resolve([script_name, flag], entry)
    script = Path(real[0])
    assert script.name == script_name
    assert script.is_file() and os.access(script, os.X_OK)
    assert real[1:] == [flag]


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
    out = fetch_key_docs(tool_ctx, task="__none__")
    assert "quickstart" in out["available_tasks"]
    assert out["docs"] == []  # no doc has that task


def test_fetch_key_docs_quickstart(tool_ctx):
    if not tool_ctx.settings.bench_repo.is_dir():
        pytest.skip("bench repo not present")
    out = fetch_key_docs(tool_ctx, task="quickstart")
    assert out["task"] == "quickstart"
    assert all(d["task"] == "quickstart" for d in out["docs"])
    # The quickstart doc must resolve and carry real content.
    qs = next((d for d in out["docs"] if d["path"].endswith("docs/quickstart.md")), None)
    assert qs is not None and qs["found"] and "kind" in qs["content"].lower()


def test_fetch_key_docs_quickstart_includes_project_playbook(tool_ctx):
    # The kind runbook is no longer inlined into CORE — it now loads via fetch_key_docs(task=
    # "quickstart") as a `kind: knowledge` entry, ALONGSIDE the upstream quickstart docs. It reads
    # from knowledge/ (always present), so this is hermetic even without the bench repo.
    out = fetch_key_docs(tool_ctx, task="quickstart")
    paths = [d["path"] for d in out["docs"]]
    pb = next((d for d in out["docs"] if d["path"] == "quickstart_playbook.md"), None)
    assert pb is not None and pb["found"], "the project runbook must be served under task=quickstart"
    body = pb["content"].lower()
    assert "standup" in body and "run" in body, "runbook must carry the standup/run flow"
    # ...and the upstream quickstart docs are still listed together with it.
    assert "llm-d-benchmark/docs/quickstart.md" in paths
    assert "llm-d-benchmark/config/scenarios/cicd/kind.yaml" in paths


# ---- read_knowledge (hybrid: core inline + rest on-demand) ----------------

def test_read_knowledge_returns_content_for_valid_topic(tool_ctx):
    out = read_knowledge(tool_ctx, name="capacity")
    assert out["name"] == "capacity.md"
    assert out["topic"] == "capacity"
    # The on-demand guide must come back with its real, full content.
    expected = (tool_ctx.settings.knowledge_dir / "deploy/capacity.md").read_text()
    assert out["content"] == expected
    assert "capacity" in out["content"].lower()


def test_read_knowledge_accepts_full_basename(tool_ctx):
    out = read_knowledge(tool_ctx, name="analysis.md")
    assert out["name"] == "analysis.md"
    assert out["content"]


def test_read_knowledge_rejects_unknown_name(tool_ctx):
    out = read_knowledge(tool_ctx, name="does_not_exist")
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
    out = read_knowledge(tool_ctx, name=evil)
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
    out = search_knowledge(tool_ctx, query="will the model fit in gpu memory capacity")
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
    out = search_knowledge(tool_ctx, query="gateway programmed false traffic cannot reach pods")
    topics = [r["topic"] for r in out["results"] if r["kind"] == "knowledge"]
    assert "gateway_readiness" in topics


def test_search_knowledge_includes_repo_doc_pointers(tool_ctx):
    out = search_knowledge(tool_ctx, query="quickstart kind cpu only deploy")
    repo_hits = [r for r in out["results"] if r["kind"] == "repo_doc"]
    assert repo_hits, "expected at least one curated repo-doc pointer"
    ptr = repo_hits[0]
    assert ptr["path"].startswith(("llm-d/", "llm-d-benchmark/"))
    assert ptr["load_with"] == f"read_repo_doc('{ptr['path']}')"


def test_search_knowledge_can_exclude_repo_docs(tool_ctx):
    out = search_knowledge(
        tool_ctx, query="quickstart kind cpu only deploy", include_repo_docs=False
    )
    assert all(r["kind"] == "knowledge" for r in out["results"])


def test_search_knowledge_respects_limit_and_is_deterministic(tool_ctx):
    out1 = search_knowledge(tool_ctx, query="benchmark results report metrics", limit=3)
    assert len(out1["results"]) <= 3
    # Same query → byte-identical ranking (no model call, stable tie-break).
    out2 = search_knowledge(tool_ctx, query="benchmark results report metrics", limit=3)
    assert out1["results"] == out2["results"]


def test_search_knowledge_empty_and_stopword_only_query(tool_ctx):
    assert "error" in search_knowledge(tool_ctx, query="   ")
    # A query of only filler words has no searchable terms.
    assert "error" in search_knowledge(tool_ctx, query="how do i the a an")


def test_search_knowledge_no_match_returns_empty_results(tool_ctx):
    out = search_knowledge(tool_ctx, query="zzzznevernevermatchqqqq")
    assert out["match_count"] == 0
    assert out["results"] == []
    # Still hands back the valid topic list so the model can fall back to browsing.
    assert "capacity" in out["valid_topics"]


def test_search_knowledge_limit_one_keeps_top_hit(tool_ctx):
    # The reserved repo-doc quota must NEVER evict the single highest-scoring result. With
    # limit=1 and a query that matches both a strong knowledge guide AND a (lower-scoring) repo
    # pointer, the one result returned must be that top-scoring hit — not the reserved repo_doc.
    # Regression for the repo_quota=max(1, limit//3) crowd-out at limit=1.
    out = search_knowledge(tool_ctx, query="quickstart kind cpu only deploy", limit=1)
    assert len(out["results"]) == 1
    top = out["results"][0]
    # Without the quota cap this came back as the score-24 repo_doc, dropping the score-67 guide.
    full = search_knowledge(tool_ctx, query="quickstart kind cpu only deploy", limit=5)
    best_score = full["results"][0]["score"]
    assert top["score"] == best_score, "limit=1 dropped the highest-scoring result"
    assert top["kind"] == "knowledge"


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
    style = (tool_ctx.settings.knowledge_dir / "conversation/conversation_style.md").read_text()
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
        body = next(kdir.rglob(name)).read_text()
        chunk = body[300:480]
        assert chunk and chunk not in prompt, f"on-demand file {name} should NOT be inlined"
        assert name in prompt, f"on-demand file {name} missing from the index"


def test_multi_harness_full_body_absent_but_indexed(tool_ctx):
    # Explicit single-file assertion required by the task: the multi_harness body is gone
    # from the prompt, yet its name still appears (so the model can load it on demand).
    from app.agent.prompt import build_system_prompt

    prompt = build_system_prompt(tool_ctx)
    body = (tool_ctx.settings.knowledge_dir / "analysis/multi_harness.md").read_text()
    # The bulk of the file (everything past the first heading) is not present.
    assert body[200:] not in prompt
    assert "multi_harness" in prompt
