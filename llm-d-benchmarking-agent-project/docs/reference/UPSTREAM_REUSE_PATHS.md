# Upstream reuse paths (`llm-d-benchmark/`): read on demand

Where to look in the READ-ONLY `llm-d-benchmark/` repo when generating configs, picking
specs, or parsing results. Read these at runtime; never vendor copies. If a path can't be
resolved, fail loudly (non-negotiable rule 7). Split out of `PROJECT_BRAIN_REFERENCE.md`.

- **CLI entry:** `pyproject.toml` → `llmdbenchmark = "llmdbenchmark.cli:cli"`
- **Specs:** `config/specification/**/*.yaml.j2` (e.g. `cicd/kind`, `guides/optimized-baseline`)
- **Harnesses:** `workload/harnesses/*` · **Workloads:** `workload/profiles/{harness}/*.yaml.in`
- **Benchmark Report v0.2 schema:** `llmdbenchmark/analysis/benchmark_report/br_v0_2_json_schema.json`
  (results are parsed from this schema, never scraped from logs: a determinism gate)
- **Safe preview / config gen:** CLI `plan`, `run --dry-run`, `run --generate-config`,
  `run --list-endpoints`
- **Bootstrap:** `install.sh` (`--uv` fetches python3.11, builds `.venv`)

## `llm-d-skills/`: the 3rd REQUIRED read-only repo (incubation skills library)
Canonical deploy / teardown / benchmark / compare / autoscale procedures, read live (never vendored;
clone via `ensure_repos` / the policy-allowed `git clone .../llm-d-incubation/llm-d-skills`). REQUIRED
alongside `llm-d` + `llm-d-benchmark`: in `Settings.repo_paths` (gates `/readyz`, captured in
provenance/reproducibility; a missing repo 503s the startup self-check, per rule 7). Independently
versioned, so `ensure_repos`' `ref` is never applied to it.
- **Skills:** `skills/<name>/SKILL.md`: `deploy-llm-d`, `teardown-llm-d`, `run-llm-d-benchmark`,
  `compare-llm-d-configurations`, `configure-wva-autoscaling-llm-d` (plus each skill's `references/` /
  `docs/` / `resources/`).
- **Wired in via:** `knowledge/key_docs.yaml` → `fetch_key_docs(task='*_skill')`; the `knowledge/`
  adapters (`deploy_path_playbook`, `sweep_playbook`, `teardown`, `autoscaling`, `author_spec_workload`)
  carry only the delta of running each through OUR tooling. The kind/CPU-sim `quickstart` runbook
  (`knowledge/quickstart_playbook.md`) is served the same way via a `kind: knowledge` entry.
- **Enforced** by the skill-grounding gate (`app/tools/run/skill_gate.py`) — mechanism + verify →
  `docs/reference/FEATURES.md` §8.
