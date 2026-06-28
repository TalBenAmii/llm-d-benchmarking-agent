"""Pydantic input models for the shell-command / setup / report-locate tools."""
from __future__ import annotations

from pydantic import BaseModel, Field


class RunShellInput(BaseModel):
    command: str = Field(
        ...,
        description="An arbitrary shell command, run verbatim via `bash -lc` (so pipes, "
                    "redirects, globs, and env expansion work). Read-only commands auto-run; "
                    "commands that write/mutate anything (or that aren't recognized as read-only) "
                    "require the user's Approve before they execute.",
        min_length=1,
    )
    timeout: float | None = Field(default=None, description="Optional timeout in seconds")


class LocateReportInput(BaseModel):
    results_dir: str | None = Field(default=None, description="Explicit results directory, if known")
    session_id: str | None = None


class EnsureReposInput(BaseModel):
    repos: list[str] | None = Field(default=None, description="Repos to clone; omit to clone all known repos. All three are REQUIRED: 'llm-d-benchmark' + 'llm-d' (specs/guides) and 'llm-d-skills' (the canonical deploy/teardown/benchmark/compare/autoscale SKILL.md grounding the knowledge/ adapters defer to — (re)clone it if fetch_key_docs(task='*_skill') comes back empty)")
    ref: str | None = Field(default=None, description="Optional branch/tag (applied to the benchmark/guide repos only, never the independently-versioned llm-d-skills)")


class RunSetupInput(BaseModel):
    use_uv: bool = Field(default=True, description="Use uv to fetch Python 3.11 (recommended)")
    force: bool = Field(default=False, description="Re-run install.sh even if the venv exists")
