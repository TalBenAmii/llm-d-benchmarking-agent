"""Backend configuration. Reads from environment / .env (never the browser).

Resolves the locations of the three REQUIRED read-only sibling repos (benchmark, guide, and the
llm-d-skills procedure library) and the project's own runtime directories. Secrets
(LLM keys, HF token) live here and are never sent to the UI or to child processes (the runner
scrubs them out).
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Repo directory names (siblings of this project under REPOS_DIR).
BENCH_REPO_NAME = "llm-d-benchmark"
GUIDE_REPO_NAME = "llm-d"
# The llm-d-incubation skills library — canonical operational procedures (deploy / teardown /
# benchmark / compare / autoscale) the agent grounds itself in, read LIVE like the other repos.
SKILLS_REPO_NAME = "llm-d-skills"


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
    # Claude Agent SDK provider (LLM_PROVIDER=claude-agent-sdk): runs inference on the user's
    # Claude subscription (e.g. a Max plan) via the logged-in ``claude`` CLI — no API key needed.
    # The SDK's own built-in tools stay disabled; the app's tools + agent loop are unchanged.
    # ``claude_cli_path`` is optional (the SDK auto-discovers the CLI on PATH when unset).
    agent_sdk_model: str = "claude-haiku-4-5"
    claude_cli_path: str | None = None
    # Reasoning quality + chain-of-thought capture for the Claude Agent SDK provider. These two
    # knobs make the provider match the Sonnet-4.6 behavior of Claude Code: ``effort`` is the
    # response-effort level ("low"|"medium"|"high"|"xhigh"|"max"; "high" is Claude Code's default
    # deep-reasoning level) and ``thinking`` selects the extended-thinking mode — "adaptive"
    # (Claude decides when/how much to think, exactly what Sonnet 4.6 in Claude Code uses), a
    # positive integer (a fixed per-turn thinking-token budget, forces thinking every turn), or
    # "off"/"disabled" to turn extended thinking off. When thinking is active the agent loop
    # captures the model's reasoning into the per-session trace (see app/observability/cot_trace.py).
    # Bound via AGENT_SDK_EFFORT/AGENT_SDK_THINKING or the provider-neutral LLM_EFFORT/LLM_THINKING
    # aliases — so an .env that set LLM_EFFORT actually takes effect instead of silently using the
    # default.
    agent_sdk_effort: str = Field(
        default="high", validation_alias=AliasChoices("agent_sdk_effort", "llm_effort"))
    agent_sdk_thinking: str = Field(
        default="adaptive", validation_alias=AliasChoices("agent_sdk_thinking", "llm_thinking"))
    # Optional cap on extended-thinking tokens per call (SDK ``max_thinking_tokens``). Thinking
    # tokens are billed as OUTPUT and ``adaptive`` thinking is otherwise unbounded, so a cap bounds
    # the worst-case output spend on a hard turn. None (default) => no cap (today's behavior).
    agent_sdk_max_thinking_tokens: int | None = Field(
        default=None,
        validation_alias=AliasChoices("agent_sdk_max_thinking_tokens", "llm_max_thinking_tokens"))
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o"
    # When True, send an OpenAI ``prompt_cache_key`` (the session id) to improve prompt-cache
    # hit-rate routing on OpenAI proper. Default OFF — some OpenAI-compatible servers (vLLM,
    # some gateways) reject unknown params; they still get *implicit* prefix caching for free.
    openai_send_prompt_cache_key: bool = False

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

    # Simulate (dry run): drive the whole workflow but execute nothing — every command
    # is a no-op returning synthetic success and per-command approvals are skipped.
    simulate: bool = False

    # Default Kubernetes namespace stamped on every newly-created session — the sidebar
    # "folder" a chat lands in until an approved SessionPlan assigns one. None (the default,
    # and what production runs with) => new chats start in the "no_namespace" folder and move
    # to their plan's namespace once approved. The test suite sets DEFAULT_SESSION_NAMESPACE=test
    # so test-created sessions cluster under a foldable "test" folder instead of bloating the
    # real chat list. Keep this None in production (loop.py only fills an *unset* namespace, so a
    # non-None default here would swallow the plan's namespace — see app/agent/loop.py).
    default_session_namespace: str | None = None
    # ---- API trust (Phase 12): optional auth + rate-limit + CORS ----------
    # ALL THREE default OFF/open, so local use is unchanged and existing flows/tests pass.
    # Turn them on only when exposing the API beyond localhost. Pure mechanism here; the
    # operator's judgment about *when* to enable lives in knowledge/api_trust.md.
    #
    # Optional Bearer-token auth. When enabled, every HTTP route and the /ws endpoint require
    # ``Authorization: Bearer <AUTH_TOKEN>`` (constant-time compared); missing/bad token -> 401.
    # AUTH_TOKEN is a secret (backend-only, like the LLM keys) — never sent to the browser.
    auth_enabled: bool = False
    auth_token: str = ""

    # In-memory token-bucket rate limiter on the HTTP message-intake surface. RPS is the
    # steady-state refill rate (tokens/second); BURST is the bucket capacity (max instantaneous
    # tokens). Empty bucket -> 429. Off by default; per-process, not distributed.
    rate_limit_enabled: bool = False
    rate_limit_rps: float = 5.0
    rate_limit_burst: int = 10

    # CORS allowed origins for the browser fetch surface. Empty (default) = today's behavior
    # (no CORS middleware installed, so the response carries no CORS headers). Set a
    # comma-separated origin list (e.g. ``https://app.example.com``) to allow those origins.
    cors_allow_origins: str = ""

    @property
    def cors_origins_list(self) -> list[str]:
        """Parse CORS_ALLOW_ORIGINS (comma-separated) into a clean origin list. Empty when
        unset — the signal to NOT install the CORS middleware at all (today's default)."""
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]

    # Public base URL for share links. Empty (default) -> the share API returns a relative
    # ``/share/<token>`` path and the browser prepends its own origin (already the public host
    # when the app is opened via a public URL/tunnel). Set this to a public origin
    # (e.g. ``https://abc.trycloudflare.com`` or a deployed domain) to mint ABSOLUTE links that
    # are shareable off-host — e.g. when you run the app at localhost but reach it via a tunnel.
    # NOTE: exposing the app publicly so friends can open share links also exposes the whole
    # agent unless you enable AUTH_TOKEN — share GETs bypass auth by design, so turning auth on
    # locks the agent while keeping share links open. The "when" judgment lives in
    # knowledge/api_trust.md; this is pure mechanism.
    share_base_url: str = ""

    @property
    def share_link_base(self) -> str:
        """The configured public base for share links, trailing slash stripped, or "" when
        unset (the signal to return a relative path that the browser resolves against origin)."""
        return self.share_base_url.strip().rstrip("/")

    # Structured logging (Phase 11). LOG_LEVEL is a stdlib level name (DEBUG/INFO/WARNING/...).
    # LOG_FORMAT is "json" (one JSON object per line — the default, for log aggregation) or
    # "text" (a compact human line, for local dev). Set via LOG_LEVEL / LOG_FORMAT in the env.
    log_level: str = "INFO"
    log_format: str = "json"

    # Max concurrent *heavy* (mutating) command executions across ALL sessions — bounds
    # how many benchmark runs proceed in parallel so they don't thrash the host. Read-only
    # probes are never capped. <= 0 means unlimited.
    max_concurrent_runs: int = 2

    # Container image for orchestrator-submitted benchmark Jobs (the in-cluster image that
    # carries the llmdbenchmark CLI + kubectl). Empty until built/published in the packaging
    # phase; the orchestrate tool then refuses rather than submitting an unrunnable Job.
    orchestrator_image: str = ""

    # ServiceAccount the orchestrator-submitted benchmark Jobs run under. When the agent runs
    # in-cluster (the packaging deploy), this is the least-privilege SA the Helm chart /
    # Kustomize base create; an empty value (local dev) leaves the pod on the namespace default
    # SA. Set via ORCHESTRATOR_SERVICE_ACCOUNT in the backend env / the deploy manifest.
    orchestrator_service_account: str = ""

    # Optional external metrics dashboard (Grafana/Prometheus) surfaced in the live resource
    # panel DURING a run. Empty (default) -> the panel shows only the agent's own kubectl-top
    # table + sparklines (today's behavior). Set GRAFANA_DASHBOARD_URL to the user's own llm-d
    # observability dashboard (the upstream `--monitoring` Grafana, e.g.
    # https://grafana.example/d/llm-d/llm-d-overview) to EMBED it alongside the run — the
    # during-run "live monitoring with llm-d's observability stack" the proposal asks for. Pure
    # mechanism: the URL is the operator's; the UI only renders it when it is an http(s) URL.
    grafana_dashboard_url: str = ""

    @property
    def metrics_dashboard_url(self) -> str:
        """The configured external metrics dashboard URL (whitespace-stripped), or "" when unset
        — the signal NOT to add a ``dashboard_url`` to the live ``resource_stats`` payload (the
        UI then shows only the agent's own kubectl-top view). The browser revalidates it is an
        http(s) URL before embedding, so a stray value never renders."""
        return self.grafana_dashboard_url.strip()

    # ---- Chaos / resilience drill (opt-in fault injection) ----------------
    # Gate for the dedicated `run_resilience_drill` tool. OFF in production: the chaos
    # fault-injection seam (app/orchestrator/chaos.py) is a KubeClient decorator that rewrites
    # cluster READ responses, so it must never be reachable on the normal orchestrate path. The
    # drill is DOUBLE-gated — this flag AND invoking the named tool — and the drill runs against
    # an in-process/fake client (it never deliberately breaks a real cluster). Set
    # CHAOS_ENABLED=true to allow a resilience drill.
    chaos_enabled: bool = False

    # ---- Workspace lifecycle (Phase 18): retention/GC caps + startup self-check ----------
    # Bound the unbounded growth of per-session/run scratch and the history store. These are
    # DATA (the caps); the GC walk + counter in app/storage/retention.py is the MECHANISM —
    # no decision logic in Python. Each cap is applied INDEPENDENTLY to each managed area
    # (sessions/, runs/, history/), removing the OLDEST items first (by mtime) until the area
    # is within the cap. An ACTIVE/running session is NEVER pruned regardless of caps.
    #
    # 0 (or unset/None) means UNLIMITED for that dimension — so the DEFAULTS BELOW DO NOT
    # SURPRISE EXISTING USERS: max-age and max-bytes default to unlimited (no time-based or
    # size-based deletion out of the box). Only a generous per-area item count is enforced by
    # default, purely to stop truly unbounded file-count growth on a long-lived server.
    # Tighten any of these via the env vars below when you want active reclamation.
    retention_max_age_days: float = 0.0     # delete items older than N days (0 = unlimited)
    retention_max_items: int = 500          # keep at most N items per area    (0 = unlimited)
    retention_max_bytes: int = 0            # keep area under N bytes total     (0 = unlimited)

    # Run the retention GC pass once at startup (in the FastAPI lifespan). Defaults ON, but it
    # is a no-op under the default caps above except the generous item-count ceiling — so a
    # fresh install reclaims nothing surprising. Set RETENTION_GC_ON_STARTUP=false to disable.
    retention_gc_on_startup: bool = True

    # Run the startup configuration self-check (workspace writable, provider config coherent,
    # repos resolvable) and fold its result into readiness. Defaults ON; it only OBSERVES (it
    # never mutates), so leaving it on is safe. Set STARTUP_SELF_CHECK=false to skip it.
    startup_self_check: bool = True

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
    def skills_repo(self) -> Path:
        return self.resolved_repos_dir / SKILLS_REPO_NAME

    @property
    def repo_paths(self) -> dict[str, Path]:
        """The three REQUIRED read-only repos (benchmark, guide, skills). This set is the
        readiness gate (the startup self-check fails if any member is missing), the
        provenance-capture set, the ``read_repo_doc`` / ``fetch_key_docs`` allow-set, the
        ``ensure_repos`` clone targets, and the command runner's ``repo:<name>`` resolution —
        every member must resolve on disk. The skills library now grounds the agent's deploy /
        teardown / benchmark / compare / autoscale procedures as the canonical default (the
        ``knowledge/`` adapters defer to it), so it is required like the other two."""
        return {
            BENCH_REPO_NAME: self.bench_repo,
            GUIDE_REPO_NAME: self.guide_repo,
            SKILLS_REPO_NAME: self.skills_repo,
        }

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
    def agent_version(self) -> str:
        """This agent's installed package version, for provenance capture (reproducibility
        bundles). Read from the installed distribution metadata (pyproject ``version``); when
        the package isn't installed as a distribution (e.g. a bare source checkout / a worktree
        run via PYTHONPATH), fall back to a stable, honest sentinel rather than crashing. NEVER
        fabricates a real-looking version."""
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("llm-d-benchmarking-agent")
        except PackageNotFoundError:
            return "0.0.0+unknown"

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
