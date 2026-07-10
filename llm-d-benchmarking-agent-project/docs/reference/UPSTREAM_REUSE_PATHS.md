# Upstream reuse paths (`llm-d-benchmark/`) — read on demand

Where to look in the **READ-ONLY** `llm-d-benchmark/` repo when generating configs, picking
specs, or parsing results. Read these at runtime; **never vendor copies** — if a path can't be
resolved, fail loudly (non-negotiable rule 7). Split out of `PROJECT_BRAIN_REFERENCE.md`.

- **CLI entry:** `pyproject.toml` → `llmdbenchmark = "llmdbenchmark.cli:cli"`
- **Specs:** `config/specification/**/*.yaml.j2` (e.g. `cicd/kind`, `guides/optimized-baseline`)
- **Harnesses:** `workload/harnesses/*` · **Workloads:** `workload/profiles/{harness}/*.yaml.in`
- **Benchmark Report v0.2 schema:** `llmdbenchmark/analysis/benchmark_report/br_v0_2_json_schema.json`
  (results are parsed from this schema, never scraped from logs — determinism gate)
- **Safe preview / config gen:** CLI `plan`, `run --dry-run`, `run --generate-config`,
  `run --list-endpoints`
- **Bootstrap:** `install.sh` (`--uv` fetches python3.11, builds `.venv`)

## `llm-d-skills/` — the 3rd REQUIRED read-only repo (incubation skills library)
Canonical, upstream-maintained operational procedures, read live (never vendored; clone via
`ensure_repos` / the allowlisted `git clone .../llm-d-incubation/llm-d-skills`). It is the **canonical
default** source for the deploy / teardown / benchmark / compare / autoscale procedures, so it is now a
**REQUIRED** repo alongside `llm-d` + `llm-d-benchmark`: it's in `Settings.repo_paths` (gates `/readyz`
— a missing skills repo 503s the startup self-check, per rule 7 — and is captured in provenance /
reproducibility). It's still independently versioned, so `ensure_repos`' `ref` is never applied to it.
- **Skills:** `skills/<name>/SKILL.md` — `deploy-llm-d`, `teardown-llm-d`, `run-llm-d-benchmark`,
  `compare-llm-d-configurations`, `configure-wva-autoscaling-llm-d` (+ each skill's `references/` /
  `docs/` / `resources/`).
- **Wired in via:** `knowledge/key_docs.yaml` → `fetch_key_docs(task='*_skill')`. The `knowledge/`
  adapters (`deploy_path_playbook`, `sweep_playbook`, `teardown`, `autoscaling`, `author_spec_workload`)
  **defer to the skill for the procedure and carry only the delta** — how each runs through OUR tooling
  (the SessionPlan gate + `llmdbenchmark` CLI + BR-v0.2 parsing + our tool names stay authoritative);
  the skill-step recaps were removed (dedup), so read the skill rather than expecting it restated here.
- **Enforced, not just encouraged:** a mutating `llmdbenchmark` op is refused until its grounding doc
  was fetched this session — the **skill-grounding gate** (`app/tools/skill_gate.py`), wired at the
  command chokepoint + the plan gate. Spec-aware: the kind/CPU-sim path grounds in the `quickstart`
  runbook (`knowledge/quickstart_playbook.md`, now served on demand via a new `kind: knowledge`
  `key_docs.yaml` entry — a `knowledge/` file delivered through `fetch_key_docs` exactly like these
  guides), every other op in its `*_skill`; WVA autoscaling is description-driven (no command
  chokepoint, so no gate). Mechanism detail + how-to-verify → `docs/reference/FEATURES.md` §8.
