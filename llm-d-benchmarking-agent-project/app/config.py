"""Backend configuration. Reads from environment / .env (never the browser).

Resolves the locations of the two read-only sibling repos and the project's own
runtime directories. Secrets (LLM keys, HF token) live here and are never sent to
the UI or to child processes (the runner scrubs them out).
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Repo directory names (siblings of this project under REPOS_DIR).
BENCH_REPO_NAME = "llm-d-benchmark"
GUIDE_REPO_NAME = "llm-d"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LLM provider
    llm_provider: str = "anthropic"
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-opus-4-8"
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o"

    # Paths (defaults computed from PROJECT_ROOT when unset)
    repos_dir: Path | None = None
    workspace_dir: Path | None = None

    # Optional secret, only for real (non-sim) gated models
    hf_token: str | None = None

    @field_validator("repos_dir", "workspace_dir", mode="before")
    @classmethod
    def _blank_is_none(cls, v: object) -> object:
        # An empty env var (e.g. ``REPOS_DIR=``) means "unset" (use the default),
        # not ``Path('.')`` which would resolve repos to the current directory.
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        return v

    # Server
    host: str = "127.0.0.1"
    port: int = 8000

    # Max concurrent *heavy* (mutating) command executions across ALL sessions — bounds
    # how many benchmark runs proceed in parallel so they don't thrash the host. Read-only
    # probes are never capped. <= 0 means unlimited.
    max_concurrent_runs: int = 2

    # Container image for orchestrator-submitted benchmark Jobs (the in-cluster image that
    # carries the llmdbenchmark CLI + kubectl). Empty until built/published in the packaging
    # phase; the orchestrate tool then refuses rather than submitting an unrunnable Job.
    orchestrator_image: str = ""

    # ---- derived locations ------------------------------------------------
    @property
    def resolved_repos_dir(self) -> Path:
        return (self.repos_dir or PROJECT_ROOT.parent).resolve()

    @property
    def resolved_workspace_dir(self) -> Path:
        return (self.workspace_dir or (PROJECT_ROOT / "workspace")).resolve()

    @property
    def bench_repo(self) -> Path:
        return self.resolved_repos_dir / BENCH_REPO_NAME

    @property
    def guide_repo(self) -> Path:
        return self.resolved_repos_dir / GUIDE_REPO_NAME

    @property
    def repo_paths(self) -> dict[str, Path]:
        """Mapping consumed by the command runner's ``repo:<name>`` references."""
        return {BENCH_REPO_NAME: self.bench_repo, GUIDE_REPO_NAME: self.guide_repo}

    @property
    def allowlist_path(self) -> Path:
        return PROJECT_ROOT / "security" / "allowlist.yaml"

    @property
    def knowledge_dir(self) -> Path:
        return PROJECT_ROOT / "knowledge"

    @property
    def ui_dir(self) -> Path:
        return PROJECT_ROOT / "ui"

    @property
    def benchmark_report_schema_path(self) -> Path:
        """The repo's authoritative Benchmark Report v0.2 JSON Schema (read at runtime)."""
        return (
            self.bench_repo
            / "llmdbenchmark"
            / "analysis"
            / "benchmark_report"
            / "br_v0_2_json_schema.json"
        )

    @property
    def extra_subprocess_env(self) -> dict[str, str]:
        """Non-secret-by-policy env passed to child processes. HF token included only
        if explicitly configured (needed for gated real-model deploys, not the sim)."""
        env: dict[str, str] = {}
        if self.hf_token:
            env["HF_TOKEN"] = self.hf_token
        return env


@lru_cache
def get_settings() -> Settings:
    return Settings()
