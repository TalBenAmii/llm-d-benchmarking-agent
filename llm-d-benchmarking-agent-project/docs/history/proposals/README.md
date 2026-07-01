# Feature proposals — design specs

Net-new, high-leverage capabilities designed against the real code (file-level,
thin-code/thick-agent compliant, reuse-heavy). Each was produced by an architect pass over
the relevant subsystem. Specs 1–4 have since been implemented and are kept as the design
record; spec 5 is an open decision doc, not yet built. See `FEATURES.md` for the live,
evidence-backed feature inventory.

| # | Spec | One-liner | Status |
|---|------|-----------|--------|
| 5 | [05-mcp-server.md](05-mcp-server.md) | Expose the agent's 38 tools to other people's agents over MCP, with the judgment shipped as resources/prompts; approval re-homed to the connecting client's own tool prompt. | **BUILT 2026-06-30** → `app/mcp/` (`python -m app.mcp`); spec → `app/mcp/DESIGN.md` |
| 4 | [04-reproducibility.md](04-reproducibility.md) | One-click provenance bundle + "Reproduce this run" (both repo SHAs, resolved config, self-contained HTML). | shipped: `export_run_bundle` + `reproduce_run` tools |
| 2 | [02-chaos-resilience.md](02-chaos-resilience.md) | Opt-in fault-injection (KubeClient decorator) + orchestrator-restart durability proof + resilience report. | shipped, then **REMOVED 2026-07-02** (hermetic-only drill retired; spec kept as historical record) |
| 1 | [01-autotuner.md](01-autotuner.md) | Closed-loop goal-seeking: agent adaptively searches the config space to hit an SLO at best goodput. | shipped: `autotune_search` tool + `knowledge/autotune_strategy.md` |
| 3 | [03-self-eval.md](03-self-eval.md) | LLM-judge agent-quality scorecard (opt-in) + autonomous exploratory bug-hunter (deterministic oracle). | shipped: self-eval harness `tests/eval/` (VALIDATION Layers 3 & 4) |

Built low-risk-first (#4 → #2 → #1 → #3), each on its own feature worktree, gate-checked
(hermetic suite green + ruff + mypy) and merged to `main`.

Shared invariants every spec respects: thin code / thick agent (judgment in `knowledge/`,
mechanism in Python); the four determinism gates; deny-by-default allowlist as DATA; the two
sibling repos are READ-ONLY; hermetic pytest stays fast and quota-free.
