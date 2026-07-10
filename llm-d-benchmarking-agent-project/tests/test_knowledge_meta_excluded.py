"""Editor-facing meta docs that live in knowledge/ (e.g. knowledge/CLAUDE.md) must NOT leak
into the runtime agent: not inlined into the system prompt, not in the on-demand index, and
not returnable by read_knowledge. This guards the subdir-CLAUDE.md pattern for the agent's
own data dir (the knowledge loader globs knowledge/*.md)."""
from __future__ import annotations

from types import SimpleNamespace

from app.agent.prompt import _knowledge_sections
from app.tools.access.knowledge_access import (
    EXCLUDED_KNOWLEDGE_FILES,
    _knowledge_files,
    read_knowledge,
)


def _ctx(kdir):
    # _knowledge_files / _knowledge_sections / read_knowledge only touch settings.knowledge_dir.
    return SimpleNamespace(settings=SimpleNamespace(knowledge_dir=kdir))


def test_excluded_set_lists_the_meta_files():
    assert "CLAUDE.md" in EXCLUDED_KNOWLEDGE_FILES
    assert "README.md" in EXCLUDED_KNOWLEDGE_FILES


def test_meta_files_excluded_everywhere(tmp_path):
    (tmp_path / "real_topic.md").write_text("# Real topic\nbody\n")
    (tmp_path / "CLAUDE.md").write_text("# meta — editor guidance only\n")
    (tmp_path / "README.md").write_text("# readme\n")
    ctx = _ctx(tmp_path)

    # (a) not in the file enumeration
    assert {f.name for f in _knowledge_files(ctx)} == {"real_topic.md"}

    # (b) not inlined or indexed into the system prompt
    blob = "\n".join(_knowledge_sections(ctx))
    assert "CLAUDE.md" not in blob
    assert "editor guidance only" not in blob
    assert "real_topic" in blob  # the genuine guide is still indexed

    # (c) read_knowledge refuses to return them, but still serves real topics
    assert "error" in read_knowledge(ctx, name="CLAUDE")
    assert "error" in read_knowledge(ctx, name="README")
    assert read_knowledge(ctx, name="real_topic")["content"].startswith("# Real topic")


def test_real_knowledge_dir_excludes_claude_md(tool_ctx):
    # The shipped knowledge/CLAUDE.md (editor guidance) must be filtered out in the real dir.
    assert "CLAUDE.md" not in {f.name for f in _knowledge_files(tool_ctx)}
