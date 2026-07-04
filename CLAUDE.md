# Monorepo root — pointer only

This directory is a **container**, not the project. It holds:

```
<repo-root>/                         # this monorepo checkout (any path / clone location)
├── llm-d/                            # READ-ONLY upstream repo (deploy guides) — never edit
├── llm-d-benchmark/                  # READ-ONLY upstream repo (the `llmdbenchmark` CLI) — never edit
├── llm-d-skills/                     # READ-ONLY upstream repo (incubation skills library) — never edit
├── demo-output/                      # demo videos/screenshots from the capture pipeline (git-excluded)
├── fresh-env/                        # throwaway fresh-WSL-distro test env (reset.sh → kind-fresh)
├── README.md · DEVICE_BOOTSTRAP.md   # the project README (front page) · fresh-device/WSL bootstrap steps
└── llm-d-benchmarking-agent-project/ # THE project — the ONLY folder we write code in
```

## Critical gotcha (applies everywhere)
**`llm-d/` + `llm-d-benchmark/` + `llm-d-skills/` are READ-ONLY** (hard-enforced by a `permissions.deny`
rule in `.claude/settings.json`): read their docs/specs/schemas/skills and shell out to their CLI at
runtime; never edit. All new code lives under `llm-d-benchmarking-agent-project/` only.

## Where the real instructions live
Work happens inside **`llm-d-benchmarking-agent-project/`** — see its `CLAUDE.md` for the
full project brain (architecture, non-negotiable rules, the worktree/test setup, the doc
map). Subsystems under it carry their own scoped `CLAUDE.md` that load additively when you
work in that directory. If you launched Claude from this monorepo root, `cd` into the
project (or just open files there) to pick up the scoped guidance.
