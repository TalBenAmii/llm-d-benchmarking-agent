# CLAUDE.md — critical instructions for this project

> This is the **project-scoped** CLAUDE.md (the canonical project brain). It lives in
> `llm-d-benchmarking-agent-project/` so Claude loads it whenever you work inside the
> project. The monorepo-root `CLAUDE.md` is now just a slim pointer + the READ-ONLY gotcha.
> Several subsystems also carry their own scoped `CLAUDE.md` (see "Layout" below) — those
> load additively when you work in that directory.

## What this is
A **local chat-based assistant agent** that helps non-experts run `llm-d-benchmark`.
A user describes a use case ("benchmark a chat app with 500 concurrent users"); the agent
interviews them, checks preconditions, deploys an llm-d stack if needed, runs a benchmark,
and explains the results — driving the `llmdbenchmark` CLI on their behalf.

## Workspace structure (three sibling folders)
```
<repo-root>/                         # this monorepo checkout (any path / clone location)
├── llm-d/                            # guide repo — READ-ONLY context (deploy guides)
├── llm-d-benchmark/                  # benchmark repo — READ-ONLY; provides the `llmdbenchmark`
│                                     #   CLI (console script `llmdbenchmark = llmdbenchmark.cli:cli`)
│                                     #   installed into its own .venv by ./install.sh
└── llm-d-benchmarking-agent-project/ # THIS project — the ONLY folder we write code in
```

## Non-negotiable rules
1. **The two repos are READ-ONLY.** Never edit, never write into `llm-d/` or `llm-d-benchmark/`.
   We read their docs/specs/schemas at runtime and shell out to their CLI. The agent may
   *clone* them if missing, but never modifies them. (This is now also enforced by a
   `permissions.deny` rule in `.claude/settings.json` — Edit/Write to those paths is hard-blocked.)
2. **All new code lives under `llm-d-benchmarking-agent-project/` only.**
3. **Thin code, thick agent.** Code = mechanism only (UI, agent loop, tools, security
   allowlist, validation). All *judgment* (which spec/harness/workload, what flags, how to
   read results) lives in the LLM + editable files under `knowledge/`. **Do not put
   decision logic in Python `if/elif` branches** — put it in `knowledge/` and let the LLM
   reason over it.
4. **Determinism via validation, not scripting.** Constrain the LLM at the boundaries:
   tool-call args validated against schemas; a `SessionPlan` approved before any mutation;
   generated configs validated via the CLI's own `--dry-run`/`plan`; results parsed from
   the repo's **Benchmark Report v0.2** schema, never scraped from logs.
5. **Security: deny-by-default allowlist + per-action approval.** Commands run as argv
   lists with `shell=False` (no shell string, ever). Read-only probes auto-run; every
   mutating command requires explicit UI approval. The allowlist is **data**
   (`security/allowlist.yaml`); `app/security/allowlist.py` is a pure validator with no
   embedded per-command knowledge.
6. **Secrets stay in the backend.** LLM API keys / HF tokens live only in backend env
   (`.env`, gitignored). The browser never sees them; subprocess env is scrubbed.
7. **Read repo truth at runtime; don't vendor copies.** The Benchmark Report schema, the
   spec/harness/workload catalog, and repo docs are read live. The only schemas we author
   are our own tool I/O and `SessionPlan`. If a repo path can't be resolved, fail loudly.

## Reuse (don't reinvent) — key paths in `llm-d-benchmark/`
- CLI entry: `pyproject.toml` → `llmdbenchmark = "llmdbenchmark.cli:cli"`
- Specs: `config/specification/**/*.yaml.j2` (e.g. `cicd/kind`, `guides/optimized-baseline`)
- Harnesses: `workload/harnesses/*` · Workloads: `workload/profiles/{harness}/*.yaml.in`
- Benchmark Report v0.2 schema: `llmdbenchmark/analysis/benchmark_report/br_v0_2_json_schema.json`
- Safe preview / config gen: CLI `plan`, `run --dry-run`, `run --generate-config`, `run --list-endpoints`
- Bootstrap: `install.sh` (`--uv` fetches python3.11, builds `.venv`)

## Layout of this project
- `app/` — FastAPI backend (mechanism): `main.py`, `config.py`, `llm/`, `agent/`, `tools/`,
  `security/`, `validation/`, `storage/`, `orchestrator/` (knowledge is loaded by
  `app/config.py` + `app/agent/prompt.py` from the root `knowledge/` dir — there is no
  `app/knowledge/` package)
- `security/allowlist.yaml` — the deny-by-default policy (data)
- `knowledge/` — the agent's editable brain (markdown/yaml; no Python)
- `ui/` — static chat UI (`index.html`, `app.js`, `styles.css`)
- `workspace/` — gitignored runtime scratch (per-session state, generated configs, logs)
- `tests/` — pytest

**Subsystem-scoped `CLAUDE.md` files** (load additively when you work in that dir — read the
local one BEFORE editing there; it carries the invariants that aren't obvious from the code):
`app/agent/` (prompt-cache byte-stability), `app/tools/` (how to add a tool),
`app/orchestrator/` (Job lifecycle / fault classification), `app/validation/` (the four
determinism gates), `app/security/` (the allowlist validator contract), `knowledge/`
(CORE vs on-demand), `tests/` (scoped runs + the worktree env gotchas).

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
All of this obeys the same thin-code/thick-agent + determinism-gate rules above.

**Simulate Mode (`SIMULATE=1`).** A dry-run toggle: the agent walks the WHOLE workflow
(probe → plan → standup → smoketest → run → report) but executes nothing — every command is
a no-op returning synthetic success, per-command approvals are skipped (the upfront
SessionPlan approval is kept), and a synthetic report is produced. Watch a guide end-to-end
without touching a cluster. Default `0` (real execution).

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

## Working in a worktree + running the suite (recurring setup — DON'T re-derive each task)
Established facts; reuse them instead of re-investigating every session (the `tests/CLAUDE.md`
has the tests-local quick reference):
- **Git root = this monorepo** (the repo checkout, at whatever path you cloned it to —
  referred to below as `<repo-root>`); `llm-d-benchmarking-agent-project/`
  is a subdir; `llm-d/` + `llm-d-benchmark/` are **nested untracked repos that are EMPTY in any
  worktree** → catalog/report tests break there unless pointed back at the primary copy via `REPOS_DIR`.
- **Branch worktrees off LOCAL `main` HEAD, not origin** (main is many commits ahead of origin and
  other sessions commit to it). Verify `merge-base == main HEAD` before merging; re-check SHA ancestry
  AFTER merging (concurrent-session hazard). Never `git add -A` at the monorepo root (grabs
  `.claude/worktrees/*` gitlinks) — add specific paths.
- **Run the suite from a worktree like this** (exercises *your* worktree code against the *populated*
  primary sibling repos — the primary `.venv` is an editable install pointing at the primary tree, so
  `PYTHONPATH` is required; `conftest.py` resolves the bench repo via `get_settings().bench_repo`,
  honoring `REPOS_DIR`):
  ```bash
  cd <worktree>/llm-d-benchmarking-agent-project
  PYTHONPATH=<worktree>/llm-d-benchmarking-agent-project \
  REPOS_DIR=<repo-root> \
  <repo-root>/llm-d-benchmarking-agent-project/.venv/bin/python -m pytest tests/
  ```
- **Healthy baseline ≈ 1598 passed / 20 skipped in ~15–20s.** Establish green BEFORE changing anything.
- Don't auto-run live-LLM eval (`LLM_EVAL_LIVE=1`, `test_flows_live.py`, `make validate-live`) — spends
  Max-plan quota; only on explicit user request. Plain `pytest` is safe.

## graphify (dev code-nav)
This project has a knowledge graph at `graphify-out/` with god nodes, community structure, and cross-file relationships.
- For codebase questions, first run `graphify query "<question>"` when `graphify-out/graph.json` exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than `GRAPH_REPORT.md` or raw grep output.
- If `graphify-out/wiki/index.md` exists, use it for broad navigation instead of raw source browsing.
- Read `graphify-out/GRAPH_REPORT.md` only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost). (A custom subdir-aware post-commit hook already does this in the background on project commits.)

## Capturing recurring conclusions (standing instruction to future-me)
When you derive a conclusion you'd otherwise re-investigate on a later task (env/test/build setup, repo
gotchas, locked design decisions), append a **1–2 line** summary to the relevant section above —
**consolidate, don't duplicate, keep it tight** (this file loads into context every session). A
`SessionEnd` hook in `.claude/settings.json` nudges you to do this at the end of a session.

## Config / model-drift audit log
Per the large-codebase best-practices guide ("review config after major model releases; retire
workarounds built for old model limitations"):
- **2026-06-07 — Opus 4.8 (Claude Code) / agent runtime = Sonnet 4.6.** Reviewed this CLAUDE.md +
  the agent's system-prompt `ROLE`/`HARD_RULES` (`app/agent/prompt.py`): **no stale model-era
  workarounds found** — both encode project facts + domain procedure, not model coaxing. The
  always-on prompt prefix was already trimmed ~33.6k→~28.1k tok across 4 commits (the lowest-risk
  levers are exhausted; further CORE trimming needs the live-LLM eval, which is off-limits unless
  user-explicit). Re-review after the next major model release.
