"""Pydantic input models for the repo-doc / knowledge access tools."""
from __future__ import annotations

from pydantic import BaseModel, Field


class ReadRepoDocInput(BaseModel):
    path: str = Field(..., description="Repo-relative path, e.g. 'llm-d-benchmark/docs/quickstart.md'")
    max_bytes: int = Field(default=40_000, ge=1, le=200_000)


class FetchKeyDocsInput(BaseModel):
    task: str | None = Field(
        default=None,
        description="Filter to one task's docs (e.g. 'quickstart', 'optimized_baseline'). "
                    "Omit to fetch every pinned doc.",
    )
    max_bytes_each: int = Field(default=20_000, ge=1, le=80_000)


class ReadKnowledgeInput(BaseModel):
    name: str = Field(
        ...,
        description="The knowledge topic to load, by its basename (with or without "
                    "extension), e.g. 'capacity', 'analysis', 'multi_harness'. Must be one "
                    "of the on-demand topics listed in the system prompt's knowledge index. "
                    "No paths, no '..', no absolute paths.",
    )
    section: str | None = Field(
        default=None,
        description="Optional: return ONLY this one markdown section of the guide (its ## / "
                    "### heading text, e.g. 'Distributed tracing') instead of the whole file. "
                    "Use it to re-fetch a section a prior whole-guide read reported under "
                    "'dropped_sections' (a large guide is truncated to a leading preview to fit "
                    "the result budget). Case-insensitive; a leading '#' is ignored.",
    )


class SearchKnowledgeInput(BaseModel):
    query: str = Field(
        ...,
        description="Free-text keywords/topic describing the problem or question, e.g. "
                    "'pods stuck pending unschedulable', 'gateway PROGRAMMED false', "
                    "'kv cache hit rate metric', 'how to lower harness cpu'. The search is "
                    "lexical (weighted keyword overlap) over every knowledge guide AND the "
                    "curated upstream repo-doc index — no exact basename needed.",
        min_length=1,
    )
    limit: int = Field(
        default=5, ge=1, le=20,
        description="Max number of ranked results to return (default 5).",
    )
    include_repo_docs: bool = Field(
        default=True,
        description="Also search the curated upstream repo-doc index "
                    "(knowledge/useful_repo_docs.md) and return repo-doc POINTERS you can open "
                    "with read_repo_doc. Set False to search only the agent's own knowledge/ "
                    "guides.",
    )
