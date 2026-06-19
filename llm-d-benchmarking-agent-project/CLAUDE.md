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
1. **The two repos are READ-ONLY** (`llm-d/`, `llm-d-benchmark/`). Read their docs/specs/schemas
   at runtime and shell out to their CLI; never edit. The agent may *clone* them if missing.
   Hard-enforced by a `permissions.deny` rule in `.claude/settings.json`.
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

## What's built (status + feature set)
The MVP — drive the `llm-d-benchmark` quickstart end-to-end on a local kind cluster
(probe → ensure repo → `install.sh --uv` → `standup --spec cicd/kind` → `smoketest` →
`run` → parse report → summarize → teardown) — is **implemented & `pytest`-green**, and the
project has grown well past it: a **Kubernetes-native orchestrator** (`app/orchestrator/`),
a **results analyzer** (goodput/SLO/Pareto/DoE), **multi-harness comparison**, a **capacity
pre-flight**, **cross-session trends**, **Prometheus/Grafana observability**, and a hardened
**Helm/Kustomize deploy**, exposing **32 tools** (`app/tools/registry.py` is authoritative).
The agent owns host bootstrap (Docker daemon + kind binary via `scripts/install_prereqs.sh`,
cluster create/delete), all approval-gated and widened purely via `security/allowlist.yaml`
(no per-command Python). All of it obeys the thin-code/thick-agent + determinism-gate rules
above. **Full feature inventory + how to verify each → `FEATURES.md` (read first); MVP/status
detail, remaining work, doc map → `docs/PROJECT_BRAIN_REFERENCE.md` + `ROADMAP_V4.md`.**

**Simulate Mode (`SIMULATE=1`).** A dry-run toggle: the agent walks the WHOLE workflow
(probe → plan → standup → smoketest → run → report) but executes nothing — every command is
a no-op returning synthetic success, per-command approvals are skipped (the upfront
SessionPlan approval is kept), and a synthetic report is produced. Watch a guide end-to-end
without touching a cluster. Default `0` (real execution).

## Docs & run — pointers
- **What the agent does + how to verify each feature** → `FEATURES.md` (read first).
- **Full doc map** (`docs/README.md` index → ARCHITECTURE, API, DEPLOYMENT, USER_GUIDE, VALIDATION,
  SECURITY, TROUBLESHOOTING, coverage catalogs, `INTERACTIVE_TEST_GUIDE.md`) and the **run-locally
  quickstart** → `docs/README.md` + `docs/PROJECT_BRAIN_REFERENCE.md`.
- **North star / design** → `llm-d-benchmarking-agent-proposal.md`, `plan.md`, `ROADMAP_V4.md`.
- **Agent brain** — `knowledge/*.md|*.yaml` hold all *judgment* (loaded at runtime; not docs to edit
  casually). See `knowledge/CLAUDE.md` before editing them.
- **Domain glossary** → `CONTEXT.md` (project root): the canonical names for domain concepts (spec,
  harness, workload, SessionPlan, goodput, dead-letter, …) + the wrong words to avoid. Use these terms
  exactly; update it the moment a term is coined/sharpened (the `domain-modeling` skill maintains it).
  For *architecture/refactor* vocabulary (module, interface, depth, seam, adapter, leverage, locality)
  + finding refactor candidates, use the `codebase-design` / `improve-codebase-architecture` skills.

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
A knowledge graph at `graphify-out/` backs code navigation: prefer `graphify query/explain/path`
over raw grep for codebase questions, and run `graphify update .` after code changes. A `PreToolUse`
hook already nudges this on grep/read, and a post-commit hook keeps the graph current — so this is
hands-off. Full usage detail → `docs/PROJECT_BRAIN_REFERENCE.md`.

## Capturing recurring conclusions (standing instruction to future-me)
When you derive a conclusion you'd otherwise re-investigate on a later task (env/test/build setup, repo
gotchas, locked design decisions), append a **1–2 line** summary to the relevant section above —
**consolidate, don't duplicate, keep it tight** (this file loads into context every session). A
`SessionEnd` hook in `.claude/settings.json` nudges you to do this at the end of a session. Reference
material (status, doc map, run-locally, graphify detail, the **config/model-drift audit log**) lives in
`docs/PROJECT_BRAIN_REFERENCE.md` — keep it there, not here, so this file stays small.
