"""read_knowledge markdown section addressing.

A ``section=`` arg returns just one named markdown section verbatim, so the model can pull a
specific part of a large guide instead of the whole thing. These tests pin the addressing
mechanism: exact section extraction (with its own sub-headings, stopping at the next
same-or-shallower heading), case/`#`-insensitive matching, the available_sections error path,
and the outline walk's fence/indentation handling. Pure mechanism — no judgment about WHICH
section to read (that is the model's call).
"""
from __future__ import annotations

from types import SimpleNamespace

from app.tools.access import knowledge_access


def _kctx(kdir):
    """A minimal stand-in ctx: read_knowledge only touches settings.knowledge_dir."""
    return SimpleNamespace(settings=SimpleNamespace(knowledge_dir=kdir))


def _write_guide(kdir):
    kdir.mkdir(parents=True, exist_ok=True)
    body = (
        "# Observability\n"
        "## Intro\n"
        "lead-in text.\n"
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


def test_section_arg_returns_only_that_section(tmp_path):
    ctx = _kctx(tmp_path / "knowledge")
    _write_guide(tmp_path / "knowledge")
    out = knowledge_access.read_knowledge(ctx, name="observability", section="Distributed tracing")
    assert out["section"] == "Distributed tracing"
    content = out["content"]
    assert content.startswith("## Distributed tracing")
    # It carries its own ### subsection...
    assert "Authoring it" in content and "jaeger/tempo" in content
    # ...but stops at the next same-level (##) heading and never bleeds into other sections.
    assert "After tracing" not in content
    assert "Grafana dashboards" not in content


def test_section_arg_is_case_insensitive_and_ignores_leading_hash(tmp_path):
    ctx = _kctx(tmp_path / "knowledge")
    _write_guide(tmp_path / "knowledge")
    out = knowledge_access.read_knowledge(ctx, name="observability", section="## grafana DASHBOARDS")
    assert out["section"] == "Grafana dashboards"
    assert "stand up Grafana" in out["content"]


def test_unknown_section_returns_available_sections(tmp_path):
    ctx = _kctx(tmp_path / "knowledge")
    _write_guide(tmp_path / "knowledge")
    out = knowledge_access.read_knowledge(ctx, name="observability", section="nonexistent")
    assert "error" in out
    avail = out["available_sections"]
    assert "Grafana dashboards" in avail and "Distributed tracing" in avail


def test_fenced_heading_lookalikes_are_not_parsed_as_headings(tmp_path):
    """A '# comment' inside a ``` / ~~~ code fence is NOT a markdown heading; the outline walk must
    track fences and skip their contents (else a fenced comment corrupts available_sections and the
    section addressing that keys off it). Live defect: vllm_overrides.md / sim_integration.md
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


def test_whole_guide_is_returned_whole(tmp_path):
    """A no-section read returns the full guide verbatim — no truncation, no annotations."""
    kdir = tmp_path / "knowledge"
    kdir.mkdir(parents=True, exist_ok=True)
    (kdir / "tiny.md").write_text("# Tiny\n## One\nshort body\n## Two\nother body\n")
    ctx = _kctx(kdir)
    out = knowledge_access.read_knowledge(ctx, name="tiny")
    assert out["name"] == "tiny.md"
    assert "truncated" not in out
    assert out["content"] == "# Tiny\n## One\nshort body\n## Two\nother body\n"
