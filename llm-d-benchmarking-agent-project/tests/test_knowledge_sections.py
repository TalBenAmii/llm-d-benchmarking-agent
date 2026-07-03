"""read_knowledge truncation UX + section addressing.

A large knowledge guide overflows the loop's per-tool-result feed-back budget and is clamped to a
leading preview before the MODEL sees it — so the later sections silently vanish. These tests pin
the mechanism that fixes that: read_knowledge keeps the FULL content but, when it will overflow,
annotates the result with the dropped section names (short signal strings the loop's clamp
preserves), and a `section=` arg re-fetches any one section verbatim. Pure mechanism — no judgment
about WHICH section to read (that is the model's call).
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from app.agent.context_mgmt import (
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


def test_fenced_heading_lookalikes_are_not_parsed_as_headings(tmp_path):
    """A '# comment' inside a ``` / ~~~ code fence is NOT a markdown heading; the outline walk must
    track fences and skip their contents (else a fenced comment corrupts available_sections and the
    dropped/section addressing that keys off it). Live defect: vllm_overrides.md / sim_integration.md
    have shell comments inside fences that used to parse as an H1."""
    kdir = tmp_path / "knowledge"
    kdir.mkdir(parents=True, exist_ok=True)
    (kdir / "fenced.md").write_text(
        "# Real Heading\n"
        "## Usage\n"
        "```bash\n"
        "# this is a shell comment, not a heading\n"
        "## nor is this\n"
        "run the thing\n"
        "```\n"
        "## After Fence\n"
        "~~~\n"
        "# tilde-fenced comment, also not a heading\n"
        "~~~\n"
        "tail.\n"
    )
    ctx = _kctx(kdir)
    out = knowledge_access.read_knowledge(ctx, name="fenced", section="nonexistent")
    avail = out["available_sections"]
    # The real headings are seen...
    assert avail == ["Real Heading", "Usage", "After Fence"]
    # ...and none of the fenced comment lines leaked in as headings.
    assert not any("comment" in h or "nor is this" in h for h in avail)
    # A section fetch stops at the next REAL heading, not a fenced lookalike inside it.
    usage = knowledge_access.read_knowledge(ctx, name="fenced", section="Usage")
    assert "run the thing" in usage["content"]
    assert "After Fence" not in usage["content"]


def test_indented_atx_heading_is_found(tmp_path):
    """CommonMark allows up to 3 leading spaces before an ATX heading; an indented '### Sub' (as in
    capacity.md's provisioning block) must still be addressable. Regression: it used to be invisible."""
    kdir = tmp_path / "knowledge"
    kdir.mkdir(parents=True, exist_ok=True)
    (kdir / "indented.md").write_text(
        "# Top\n"
        "## Parent\n"
        "  ### Indented Sub\n"
        "    body under the indented sub\n"
        "## Sibling\n"
    )
    ctx = _kctx(kdir)
    out = knowledge_access.read_knowledge(ctx, name="indented", section="Indented Sub")
    assert out["section"] == "Indented Sub"
    assert "body under the indented sub" in out["content"]
    assert "Sibling" not in out["content"]


def test_oversized_section_gets_dropped_subsection_annotation(tmp_path):
    """A section is usually small, but a large one can itself overflow the feed-back budget. When it
    does, the section-fetch path gets the SAME clamp-surviving annotation as the whole-guide path,
    naming its ### sub-sections past the cut and pointing at a sub-section re-fetch."""
    kdir = tmp_path / "knowledge"
    kdir.mkdir(parents=True, exist_ok=True)
    # One '## Big' section whose own body overflows the budget, with two ### subs PAST the cut.
    filler = "\n".join(f"big section line {i} carrying enough words to overflow" for i in range(160))
    (kdir / "huge.md").write_text(
        "# Huge Guide\n"
        "## Big\n"
        f"{filler}\n"
        "### First Sub\n"
        "first sub body.\n"
        "### Second Sub\n"
        "second sub body.\n"
        "## Other\n"
        "unrelated.\n"
    )
    ctx = _kctx(kdir)
    out = knowledge_access.read_knowledge(ctx, name="huge", section="Big")
    # The full section body is still returned (callers/UI get the whole thing)...
    assert out["content"].startswith("## Big")
    assert len(json.dumps(out)) > DEFAULT_TOOL_RESULT_BUDGET
    # ...but the model-facing result names the sub-sections that fall past the clamp's cut...
    dropped = out["dropped_sections"]
    assert "First Sub" in dropped and "Second Sub" in dropped
    # ...and the note nudges a sub-section re-fetch (not a promise the section came whole).
    assert "sub-section" in out["note"]
    assert "section=" in out["note"]
    # The annotation survives the loop's clamp (short signal strings).
    clamped = json.loads(clamp_tool_result_content(out, DEFAULT_TOOL_RESULT_BUDGET))
    assert clamped.get("_truncated") is True
    assert "First Sub" in clamped["dropped_sections"]


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
