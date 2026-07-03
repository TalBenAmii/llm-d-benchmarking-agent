"""read_knowledge truncation UX + section addressing.

A large knowledge guide overflows the loop's per-tool-result feed-back budget and is clamped to a
leading preview before the MODEL sees it — so the later sections silently vanish. These tests pin
the mechanism that fixes that: read_knowledge keeps the FULL content but, when it will overflow,
annotates the result with the dropped ## section names (short signal strings the loop's clamp
preserves), and a `section=` arg re-fetches any one section verbatim. Pure mechanism — no judgment
about WHICH section to read (that is the model's call).
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from app.agent.tool_result_budget import (
    DEFAULT_TOOL_RESULT_BUDGET,
    clamp_tool_result_content,
)
from app.tools import knowledge_access


def _kctx(kdir):
    """A minimal stand-in ctx: read_knowledge only touches settings.knowledge_dir + fetched_docs."""
    return SimpleNamespace(settings=SimpleNamespace(knowledge_dir=kdir), fetched_docs=set())


def _write_large_guide(kdir):
    kdir.mkdir(parents=True, exist_ok=True)
    # A leading section big enough to overflow the budget on its own, then two sections that fall
    # PAST the cut (they must be named as dropped, not silently swallowed).
    filler = "\n".join(f"intro line {i} with some words to take up room" for i in range(140))
    body = (
        "# Observability\n"
        "## Intro\n"
        f"{filler}\n"
        "## Grafana dashboards\n"
        "How to stand up Grafana for the live panel.\n"
        "## Distributed tracing\n"
        "Configure the tracing block (config-only).\n"
        "### Authoring it\n"
        "the jaeger/tempo endpoint goes here.\n"
        "## After tracing\n"
        "trailing section.\n"
    )
    (kdir / "observability.md").write_text(body)
    return body


def test_large_guide_keeps_full_content_and_names_dropped_sections(tmp_path):
    ctx = _kctx(tmp_path / "knowledge")
    body = _write_large_guide(tmp_path / "knowledge")
    out = knowledge_access.read_knowledge(ctx, name="observability")
    # The FULL guide text is preserved in the returned dict (callers/UI still get the whole thing).
    assert out["content"] == body
    # The sections past the cut are NAMED so the model knows what it is missing.
    dropped = out["dropped_sections"]
    assert "Grafana dashboards" in dropped
    assert "Distributed tracing" in dropped
    assert "After tracing" in dropped
    # ...and the leading (shown) sections are NOT listed as dropped.
    assert "Intro" not in dropped
    # The note tells the model how to re-fetch a dropped section.
    assert "section=" in out["note"]


def test_dropped_sections_survive_the_feed_back_clamp(tmp_path):
    """End-to-end: the model only ever sees the clamped copy, so the dropped-section names + note
    must SURVIVE the loop's clamp (they are short signal strings), even though the full content is
    clipped to a preview."""
    ctx = _kctx(tmp_path / "knowledge")
    _write_large_guide(tmp_path / "knowledge")
    out = knowledge_access.read_knowledge(ctx, name="observability")
    clamped = json.loads(clamp_tool_result_content(out, DEFAULT_TOOL_RESULT_BUDGET))
    # The clamp DID truncate (content is bulk payload, shown only as a clipped preview)...
    assert clamped.get("_truncated") is True
    # ...but the model still learns which sections were dropped and how to re-fetch them.
    assert "Grafana dashboards" in clamped["dropped_sections"]
    assert "Distributed tracing" in clamped["dropped_sections"]
    assert "section=" in clamped["note"]


def test_section_arg_returns_only_that_section(tmp_path):
    ctx = _kctx(tmp_path / "knowledge")
    _write_large_guide(tmp_path / "knowledge")
    out = knowledge_access.read_knowledge(ctx, name="observability", section="Distributed tracing")
    assert out["section"] == "Distributed tracing"
    content = out["content"]
    assert content.startswith("## Distributed tracing")
    # It carries its own ### subsection...
    assert "Authoring it" in content and "jaeger/tempo" in content
    # ...but stops at the next same-level (##) heading and never bleeds into other sections.
    assert "After tracing" not in content
    assert "Grafana dashboards" not in content
    # A targeted section fetch is small — never clamped.
    assert len(json.dumps(out)) <= DEFAULT_TOOL_RESULT_BUDGET


def test_section_arg_is_case_insensitive_and_ignores_leading_hash(tmp_path):
    ctx = _kctx(tmp_path / "knowledge")
    _write_large_guide(tmp_path / "knowledge")
    out = knowledge_access.read_knowledge(ctx, name="observability", section="## grafana DASHBOARDS")
    assert out["section"] == "Grafana dashboards"
    assert "stand up Grafana" in out["content"]


def test_unknown_section_returns_available_sections(tmp_path):
    ctx = _kctx(tmp_path / "knowledge")
    _write_large_guide(tmp_path / "knowledge")
    out = knowledge_access.read_knowledge(ctx, name="observability", section="nonexistent")
    assert "error" in out
    avail = out["available_sections"]
    assert "Grafana dashboards" in avail and "Distributed tracing" in avail


def test_small_guide_is_returned_whole_untruncated(tmp_path):
    """Regression: a guide that fits the budget is returned in full — no truncation envelope."""
    kdir = tmp_path / "knowledge"
    kdir.mkdir(parents=True, exist_ok=True)
    (kdir / "tiny.md").write_text("# Tiny\n## One\nshort body\n## Two\nother body\n")
    ctx = _kctx(kdir)
    out = knowledge_access.read_knowledge(ctx, name="tiny")
    assert out["name"] == "tiny.md"
    assert "truncated" not in out
    assert "dropped_sections" not in out
    assert out["content"] == "# Tiny\n## One\nshort body\n## Two\nother body\n"
