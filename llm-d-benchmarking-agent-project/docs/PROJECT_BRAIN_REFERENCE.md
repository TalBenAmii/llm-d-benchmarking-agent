# Project brain — reference (read on demand)

This holds the **reference / historical** material that used to live in the always-on
`CLAUDE.md`. It was moved here to keep the per-turn context budget small: this file is read
only when you actually need it, not loaded every session. The active `CLAUDE.md` keeps the
non-negotiable rules, the layout, the reuse paths, and the worktree/test setup, and points
here for the rest.

## First milestone (MVP) — IMPLEMENTED & verified (2026-05-31)
Drive the `llm-d-benchmark` quickstart (local kind cluster, CPU-only sim) end-to-end:
probe → ensure repo → `install.sh --uv` → `standup --spec cicd/kind` → `smoketest` →
`run -l inference-perf -w sanity_random.yaml` → parse report → summarize → offer teardown.

**Status:** the full vertical is built and `pytest tests/` passes. A real LLM session needs
an API key in `.env` (only the fake-provider loop is exercised in tests). The agent owns
host bootstrap: it installs the prerequisites `install.sh` does NOT (the Docker daemon + the
kind binary) via the vetted `scripts/install_prereqs.sh`, creates/deletes the kind cluster
(`kind create/delete cluster`), and runs any allowlisted command through the generic
`run_command` tool — all approval-gated, and all widened purely via
`security/allowlist.yaml` (no per-command Python). The agent tools live in `app/tools/`; the
loop is `app/agent/loop.py`; the policy is `security/allowlist.yaml`.

## Beyond the MVP — current feature set
The project has grown well past the quickstart MVP (see `FEATURES.md` for the full,
evidence-backed feature inventory and `ROADMAP_V4.md` for remaining/deferred work). It now
also includes: a **Kubernetes-native benchmark orchestrator**
(`app/orchestrator/` — Job lifecycle, fault classification, retry/dead-letter, parallel
sweeps), a **results analyzer** (goodput, SLO filtering, Pareto/DoE), **multi-harness
comparison**, a **capacity pre-flight**, **cross-session result history + trends**,
**Prometheus/Grafana observability**, and a **hardened image + one-command Helm/Kustomize
deploy** (`Dockerfile`, `deploy/`) with least-privilege RBAC. The agent exposes **32 tools**
(see `app/tools/registry.py` for the authoritative list).
All of this obeys the same thin-code/thick-agent + determinism-gate rules in `CLAUDE.md`.

## Key documentation map (read these for context)
Paths are relative to `llm-d-benchmarking-agent-project/`.

**Start here / project-level**
- `README.md` — overview, safety model, how to run.
- `FEATURES.md` — **authoritative, evidence-backed inventory of every feature** + how to see/verify each. Read this first to understand what the agent actually does.
- `llm-d-benchmarking-agent-proposal.md` — the original project proposal / requirements (the "north star").
- `plan.md` — original design doc + MVP "Implementation status" record (design rationale, locked decisions, edge cases).
- `ROADMAP_V4.md` — forward-looking gap roadmap; the only remaining work is the 7 DEFERRED phases (everything else is merged).

**Technical docs (`docs/`)**
- `docs/README.md` — the docs index.
- `docs/ARCHITECTURE.md` — layers, components, the four determinism gates, trust boundaries.
- `docs/API.md` — HTTP/WebSocket API + the 32-tool agent surface + `SessionPlan`.
- `docs/DEPLOYMENT.md` — running locally and in-cluster (Helm/Kustomize), config, secrets, RBAC, observability.
- `docs/USER_GUIDE.md` — using the agent end-to-end with no `llm-d-benchmark` expertise.
- `docs/VALIDATION.md` — the flow-validation harness (does the agent run the *right* commands?).
- `docs/SECURITY.md` · `docs/TROUBLESHOOTING.md` · `docs/CONTRIBUTING.md` · `docs/CHANGELOG.md` — ops/trust, symptom→fix, how to add a tool/flow, release history.
- `docs/BENCHMARK_FEATURE_COVERAGE.md` — benchmark-CLI feature-coverage catalog (✅/🟡/⬜).
- `docs/USEFUL_REPO_DOCS.md` — curated index of which upstream `llm-d` / `llm-d-benchmark` docs matter and why.
- `docs/DEV_PLUGINS.md` — the Claude Code dev plugins enabled for this repo (project-scoped in `.claude/settings.json`) + which need a restart / external setup to work in this headless WSL host.
- `INTERACTIVE_TEST_GUIDE.md` — follow-along runbook to drive every feature by hand with a real LLM.

**Agent brain** — `knowledge/*.md|*.yaml` hold all *judgment* (loaded at runtime; not docs to edit casually). See `knowledge/CLAUDE.md` before editing them.

## Run locally
```bash
cp .env.example .env   # fill in ANTHROPIC_API_KEY (or OpenAI-compatible) — never commit .env
pip install -e .       # or: uv pip install -e .
uvicorn app.main:app --reload
# open http://127.0.0.1:8000
pytest tests/
```

## graphify (dev code-nav) — full detail
This project has a knowledge graph at `graphify-out/` with god nodes, community structure, and cross-file relationships. (A `PreToolUse` hook in `.claude/settings.json` already nudges you toward `graphify query` when you grep or read source, so the per-turn reminder is automatic — this is the full reference.)
- For codebase questions, first run `graphify query "<question>"` when `graphify-out/graph.json` exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than `GRAPH_REPORT.md` or raw grep output.
- If `graphify-out/wiki/index.md` exists, use it for broad navigation instead of raw source browsing.
- Read `graphify-out/GRAPH_REPORT.md` only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost). (A custom subdir-aware post-commit hook already does this in the background on project commits.)

## Config / model-drift audit log
Per the large-codebase best-practices guide ("review config after major model releases; retire
workarounds built for old model limitations"):
- **2026-06-07 — Opus 4.8 (Claude Code) / agent runtime = Sonnet 4.6.** Reviewed `CLAUDE.md` +
  the agent's system-prompt `ROLE`/`HARD_RULES` (`app/agent/prompt.py`): **no stale model-era
  workarounds found** — both encode project facts + domain procedure, not model coaxing. The
  always-on prompt prefix was already trimmed ~33.6k→~28.1k tok across 4 commits (the lowest-risk
  levers are exhausted; further CORE trimming needs the live-LLM eval, which is off-limits unless
  user-explicit). Re-review after the next major model release.
- **2026-06-13 — context-budget pass (Claude Code = Opus 4.8).** Moved the reference/history
  sections out of the always-on `CLAUDE.md` into this file; converted the
  `use-worktree-when-implementing` memory into a `PreToolUse(Edit|Write)` worktree gate and the
  `rtx5070-gpu-cluster` memory into a keyword-gated `UserPromptSubmit` injection
  (`.claude/hooks/gpu_context.sh`). Goal: shrink the per-turn always-on prefix without losing the
  knowledge — it now loads on demand / on relevance instead of every turn.
