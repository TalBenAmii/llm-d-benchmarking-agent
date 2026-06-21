# CLAUDE.md — critical instructions for this project

> **Project-scoped brain** — loads whenever you work inside `llm-d-benchmarking-agent-project/`.
> The monorepo-root `CLAUDE.md` is a slim pointer; subsystem dirs carry their own scoped
> `CLAUDE.md` that load additively (see "Layout").

## What this is
A **local chat-based assistant agent** that helps non-experts run `llm-d-benchmark`.
A user describes a use case ("benchmark a chat app with 500 concurrent users"); the agent
interviews them, checks preconditions, deploys an llm-d stack if needed, runs a benchmark,
and explains the results — driving the `llmdbenchmark` CLI on their behalf.

## Workspace (three sibling folders under `<repo-root>/`)
`llm-d/` + `llm-d-benchmark/` = READ-ONLY upstream repos (deploy guides + the `llmdbenchmark`
CLI `llmdbenchmark = llmdbenchmark.cli:cli`, installed into its own `.venv` by `./install.sh`);
`llm-d-benchmarking-agent-project/` = THIS project, the only folder we write code in.

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
The kind-quickstart MVP (probe → ensure repo → `install.sh --uv` → `standup --spec cicd/kind`
→ `smoketest` → `run` → parse report → summarize → teardown) is **implemented & `pytest`-green**.
Well past it now: a **K8s-native orchestrator** (`app/orchestrator/`), results analyzer
(goodput/SLO/Pareto/DoE), multi-harness comparison, capacity pre-flight, cross-session trends,
Prometheus/Grafana observability, hardened Helm/Kustomize deploy — exposing **37 tools**
(`app/tools/registry.py` is authoritative). Host bootstrap (Docker daemon + kind via
`scripts/install_prereqs.sh`, cluster create/delete) is agent-owned, approval-gated, and widened
purely via `security/allowlist.yaml` (no per-command Python) — all obeying the thin-code/thick-agent
+ determinism rules above. **Full inventory + how to verify each → `FEATURES.md` (read first);
status/remaining/doc map → `docs/PROJECT_BRAIN_REFERENCE.md` + `ROADMAP_V4.md`.**

**Simulate Mode (`SIMULATE=1`).** Dry-run toggle: the agent walks the WHOLE workflow
(probe → plan → standup → smoketest → run → report) but executes nothing — every command a
no-op returning synthetic success, per-command approvals skipped (the upfront SessionPlan
approval is kept), a synthetic report produced. Default `0` (real execution).

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

## Working in a worktree + running the suite (established facts — reuse, don't re-derive)
`tests/CLAUDE.md` has the tests-local quick reference. Key facts:
- **Git root = this monorepo** (`<repo-root>`); `llm-d-benchmarking-agent-project/` is a subdir.
  `llm-d/` + `llm-d-benchmark/` are **nested untracked repos, EMPTY in any worktree** → point
  catalog/report tests back at the primary copy via `REPOS_DIR=<repo-root>`.
- **Branch worktrees off LOCAL `main` HEAD, not origin** (main is many commits ahead of origin and
  other sessions commit to it). Verify `merge-base == main HEAD` before merging; re-check SHA ancestry
  AFTER merging (concurrent-session hazard). Never `git add -A` at the monorepo root (grabs
  `.claude/worktrees/*` gitlinks) — add specific paths.
- **You don't run the suite or lint as a gate — the git hook does, at merge-to-main.** A local
  `.git/hooks/{pre-commit,pre-merge-commit}` runs `ruff check .` **and** `pytest tests/` on every
  commit/merge to `main` and blocks it if either is red (main-only; feature/worktree branches are
  **not** gated). So there is **no need to establish a "green baseline" when you branch out** —
  green is verified when the feature lands on `main`. Finish loop: commit on the branch → `--no-ff`
  merge into main (the hook verifies green) → `git worktree remove`. Don't run the suite yourself
  or record "ran it, green" notes. Bypass once with `--no-verify`. Hooks live in `.git/hooks` (not
  version-controlled) — recreate on a fresh clone, and keep `core.hooksPath` **empty** (a stale
  value silently disables every hook; it once pointed at a deleted dir → no hooks ran at all).
- **To run the suite manually** (debugging a specific failure, *not* as a gate): point it at the
  *populated* primary sibling repos — worktree siblings are EMPTY; the primary `.venv` is an
  editable install → `PYTHONPATH` required; `conftest.py` resolves the bench repo via
  `get_settings().bench_repo`, honoring `REPOS_DIR`. Healthy ≈ 1598 passed / 20 skipped in ~15–20s:
  ```bash
  cd <worktree>/llm-d-benchmarking-agent-project
  PYTHONPATH=<worktree>/llm-d-benchmarking-agent-project \
  REPOS_DIR=<repo-root> \
  <repo-root>/llm-d-benchmarking-agent-project/.venv/bin/python -m pytest tests/
  ```
- Don't auto-run live-LLM eval (`LLM_EVAL_LIVE=1`, `test_flows_live.py`, `make validate-live`) — spends
  Max-plan quota; only on explicit user request. The hook runs plain `pytest` (never live eval).

## Capturing recurring conclusions (standing instruction to future-me)
When you derive a conclusion you'd otherwise re-investigate on a later task (env/test/build setup, repo
gotchas, locked design decisions), append a **1–2 line** summary to the relevant section above —
**consolidate, don't duplicate, keep it tight** (this file loads into context every session). Reference
material (status, doc map, run-locally, the **config/model-drift audit log**) lives in
`docs/PROJECT_BRAIN_REFERENCE.md` — keep it there, not here, so this file stays small.
