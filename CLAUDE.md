# Monorepo root — pointer only

This directory is a **container**, not the project. It holds:

```
/home/tal/kind-quickstart-guide/
├── llm-d/                            # READ-ONLY upstream repo (deploy guides) — never edit
├── llm-d-benchmark/                  # READ-ONLY upstream repo (the `llmdbenchmark` CLI) — never edit
└── llm-d-benchmarking-agent-project/ # THE project — the ONLY folder we write code in
```

## Critical gotcha (applies everywhere)
**`llm-d/` and `llm-d-benchmark/` are READ-ONLY.** We read their docs/specs/schemas at
runtime and shell out to their CLI; we never modify them. (Enforced by a `permissions.deny`
rule in `.claude/settings.json` — Edit/Write to those paths is hard-blocked.) All new code
lives under `llm-d-benchmarking-agent-project/` only.

## Where the real instructions live
Work happens inside **`llm-d-benchmarking-agent-project/`** — see its `CLAUDE.md` for the
full project brain (architecture, non-negotiable rules, the worktree/test setup, the doc
map). Subsystems under it carry their own scoped `CLAUDE.md` that load additively when you
work in that directory. If you launched Claude from this monorepo root, `cd` into the
project (or just open files there) to pick up the scoped guidance.
