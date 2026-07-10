# CLAUDE.md — llm-d-benchmarking-agent (project map + non-negotiables)

> Project-scoped brain (loads every session you work here); the monorepo-root `CLAUDE.md` is a slim
> pointer. **This file is the map** — the folder tree plus the rules that must always apply. Folders
> marked 📁 carry their own `CLAUDE.md` with file-level detail: open that one when you work there.
> Everything else is a pointer to a reference doc, fetched on demand.

## What this is
A **local chat-based assistant agent** that helps non-experts run `llm-d-benchmark`: it interviews the
user, checks preconditions, deploys an llm-d stack if needed, runs a benchmark, and explains the results
— driving the `llmdbenchmark` CLI on their behalf.

## Non-negotiable rules (always apply)
1. **`llm-d/` + `llm-d-benchmark/` + `llm-d-skills/` are READ-ONLY** upstream repos — read their
   docs/specs/schemas/skills and shell out to their CLI; never edit (clone if missing). Hard-enforced
   by `permissions.deny`. (How the skills library is wired into the agent → `docs/reference/UPSTREAM_REUSE_PATHS.md`.)
2. **All new code lives under `llm-d-benchmarking-agent-project/` only** — except the split-out
   **`llm-d-bench-mcp/`** repo (the standalone MCP server; an OWNED sibling of this project, its own
   git repo — see the monorepo-root `CLAUDE.md` map).
3. **Thin code, thick agent** — code is mechanism; all judgment lives in the LLM + `knowledge/`. No
   decision logic in Python `if/elif` branches.
4. **Determinism at the boundaries** — schema-validated tool args; a `SessionPlan` approved before any
   mutation; configs validated via the CLI's `--dry-run`/`plan`; results parsed from the Benchmark
   Report v0.2 schema, never scraped from logs.
5. **Security: the mutating→approval gate is the guardrail.** Mutations need explicit approval; read-only
   probes auto-run; subprocess env is scrubbed. The deny-by-default allowlist (data in
   `security/allowlist.yaml`; `app/security` = pure validator) governs the dedicated command tools;
   `run_shell` is gated by the read-only/mutating classifier instead. Detail → `app/security/CLAUDE.md` +
   the `coding-guidelines` skill.
6. **Secrets stay in the backend** (`.env`, gitignored; subprocess env scrubbed; browser never sees keys).
7. **Read repo truth at runtime; don't vendor copies** — fail loudly if a repo path can't be resolved.

> Full rationale + the conventions the finish-time review enforces → the **`coding-guidelines`** skill.

## Project structure (the map — open a folder's 📁 `CLAUDE.md` when working there)
```
llm-d-benchmarking-agent-project/
├─ app/                   FastAPI backend — mechanism only (no judgment)
│  ├─ main.py·config.py·paths.py·dig.py   app entry · settings+knowledge load · path resolve · safe dict/JSON accessors
│  ├─ agent/         📁   the agent loop + system prompt (prompt-cache byte-stability)
│  ├─ tools/         📁   the tool registry (registry.py authoritative); handlers in setup/·run/·analyze/·access/ subpackages; schemas/ = tool I/O + SessionPlan JSON schemas
│  ├─ validation/    📁   the four determinism gates
│  ├─ security/      📁   allowlist validator (pure; the policy itself is data in /security)
│  ├─ orchestrator/  📁   Kubernetes-native benchmark Job lifecycle + fault classification
│  ├─ capacity/      📁   capacity pre-flight — feasibility check at the plan gate (pure, no-I/O half)
│  ├─ readiness/     📁   endpoint/stack readiness — pure analyzer + thin probe layer
│  ├─ packaging/     📁   deploy-artifact contract + shareable HTML report/chat export
│  ├─ observability/ 📁   dependency-free metrics + Prometheus exposition + structured logging
│  ├─ llm/           📁   provider-agnostic LLM integration (anthropic / openai-compat / claude-agent-sdk)
│  ├─ storage/       📁   persistence: history · provenance · share · retention/GC
│  └─ web.py             pure, decorator-free HTTP/SSE helpers extracted from main (path-traversal 404s, CORS guard, share redaction)
├─ knowledge/       📁    the agent's editable brain (md/yaml) — ALL judgment lives here (62 files in
│                         10 topic subfolders; resolved by basename via a recursive glob — see its CLAUDE.md)
├─ security/             allowlist.yaml — the deny-by-default policy (DATA, not code)
├─ deploy/               Helm chart + observability manifests
├─ scripts/              root entry points (run.sh · install_local.sh · _env.sh shared lib) + install/ (service+host bootstrap: install_service.sh · install_prereqs.sh · install_metrics_server.sh · install-git-hooks.sh · setup-claude-plan.sh · kind_egress_heal.sh) + bridges/ (allowlisted repo wrappers: aggregate_runs.py · capacity_check.py · provision_hf_secret.py) + eval/ (flow eval: validate_flows.py · run_eval_isolated.sh)
├─ tests/           📁    pytest suite (+ eval/ flows/ integration/) — env & run cheat sheet lives here
├─ testing/              non-product harnesses (local-cluster mock GPU; build-excluded)
├─ ui/                   static chat UI (index.html, app.js, styles.css)
├─ docs/                 documentation (README index + guides/ how-to · reference/ API·ARCHITECTURE·FEATURES·CONTEXT · project/ TODO·CONFIG_AUDIT_LOG + images/ UI stills)
└─ workspace/            gitignored runtime scratch (per-session state, generated configs, logs)
```
Knowledge is loaded by `app/config.py` + `app/agent/prompt.py` from the root `knowledge/` dir — there is
no `app/knowledge/` package.

## Reference — fetch on demand (NOT inlined here)
- **What's built / status / feature set** → `docs/reference/FEATURES.md` (read first; how to verify each) + `docs/reference/PROJECT_BRAIN_REFERENCE.md`; gaps → the DEFERRED phases in `docs/reference/FEATURES.md`
- **Coding conventions** (+ what the finish-time review checks) → **`coding-guidelines`** skill
- **Finish loop** (commit → review → `--no-ff` merge to main; the `main`-only git hook gates ruff+pytest) → **`finish-implementation`** skill
- **Test env + run commands + gotchas** → `tests/CLAUDE.md`
- **Upstream reuse paths** (specs, harnesses, report schema, CLI safe-preview) → `docs/reference/UPSTREAM_REUSE_PATHS.md`
- **Domain glossary** (spec/harness/workload/SessionPlan/goodput/dead-letter…) → `docs/reference/CONTEXT.md`
- **Full doc map** → `docs/README.md`; **run-locally quickstart** → root `README.md` / `docs/guides/DEPLOYMENT.md`
- **SIMULATE=1** — dry-run toggle (walk the whole workflow; read-only commands run for real, mutations no-op) → `docs/reference/CONTEXT.md` §Simulate Mode + `knowledge/reference/sim_integration.md`. Default `0`.

## Capturing recurring conclusions (standing instruction to future-me)
When you derive a conclusion you'd otherwise re-investigate later (env/build gotchas, locked decisions),
put it where it belongs — tightly: a **folder-level fact** → that folder's `CLAUDE.md`; a **cross-cutting
rule** → the right skill / global `~/.claude/CLAUDE.md`; **status / reference** →
`docs/reference/PROJECT_BRAIN_REFERENCE.md`; a **dated config/model-drift audit entry** → `docs/project/CONFIG_AUDIT_LOG.md`.
Keep THIS file a map (structure + non-negotiables + pointers only).
Consolidate, don't duplicate.
