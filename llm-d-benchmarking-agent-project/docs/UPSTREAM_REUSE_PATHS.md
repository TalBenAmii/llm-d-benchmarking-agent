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
