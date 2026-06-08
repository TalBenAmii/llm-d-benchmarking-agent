# Feature proposals — design specs (pre-implementation)

Four net-new, high-leverage capabilities designed against the real code (file-level,
thin-code/thick-agent compliant, reuse-heavy). Each was produced by an architect pass over
the relevant subsystem. Build order is low-risk-first.

| # | Spec | Effort | One-liner |
|---|---|---|---|
| 4 | [04-reproducibility.md](04-reproducibility.md) | M | One-click provenance bundle + "Reproduce this run" (both repo SHAs, resolved config, self-contained HTML). |
| 2 | [02-chaos-resilience.md](02-chaos-resilience.md) | M | Opt-in fault-injection (KubeClient decorator) + orchestrator-restart durability proof + resilience report. |
| 1 | [01-autotuner.md](01-autotuner.md) | M | Closed-loop goal-seeking: agent adaptively searches the config space to hit an SLO at best goodput. |
| 3 | [03-self-eval.md](03-self-eval.md) | L | LLM-judge agent-quality scorecard (opt-in) + autonomous exploratory bug-hunter (deterministic oracle). |

Recommended build order: **#4 → #2 → #1 → #3** (lowest risk / most reuse first; the
self-eval judge can then grade the new features too). Each ships on its own feature worktree,
gate-checked (hermetic suite green + ruff + mypy), then merged to `main`.

Shared invariants every spec respects: thin code / thick agent (judgment in `knowledge/`,
mechanism in Python); the four determinism gates; deny-by-default allowlist as DATA; the two
sibling repos are READ-ONLY; hermetic pytest stays fast and quota-free.
