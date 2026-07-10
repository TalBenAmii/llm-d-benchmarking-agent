# Documentation

The technical documentation suite for the **llm-d Benchmarking Agent** — a conversational
agent + Kubernetes-native benchmark orchestrator + results analyzer for
[`llm-d-benchmark`](https://github.com/llm-d/llm-d-benchmark).

| Doc | For | Covers |
|---|---|---|
| [ARCHITECTURE.md](reference/ARCHITECTURE.md) | engineers / reviewers | System design: layers, components, the four determinism gates, request flow, trust boundaries, concurrency & resilience. |
| [API.md](reference/API.md) | integrators / contributors | The HTTP/WebSocket API and the agent tool surface (inputs, classification, result shapes) + the `SessionPlan`. |
| [DEPLOYMENT.md](guides/DEPLOYMENT.md) | operators | Running locally and in-cluster (Helm), configuration, secrets, least-privilege RBAC, observability. |
| [CLUSTER_SERVICE_DEPLOY.md](guides/CLUSTER_SERVICE_DEPLOY.md) | maintainers / operators | Deploying the agent **itself as an in-cluster service**: the self-contained full-bake image (bundles the `llmdbenchmark` CLI + sibling repos + client toolchain), `scripts/install_service.sh` (Helm), workspace persistence, and the kind test adapter. |
| [USER_GUIDE.md](guides/USER_GUIDE.md) | end users | Using the agent end-to-end with no `llm-d-benchmark` expertise. |
| [GPU_CLUSTER_RUNBOOK.md](guides/GPU_CLUSTER_RUNBOOK.md) | end users / operators | Going beyond the CPU `cicd/kind` quickstart: stand up a real **single-GPU** cluster (minikube + NVIDIA, WSL2/RTX 4060 worked example), author a tiny-model scenario that fits 8 GB, and a feature-by-feature checklist of what's real vs simulated on one card. |
| [VALIDATION.md](reference/VALIDATION.md) | contributors | The flow-validation harness — proving the agent runs the *right* commands. |
| [MCP.md](reference/MCP.md) | Claude Code users | Pointer: the `llm-d-bench` MCP server split into its own repo ([llm-d-bench-mcp](https://github.com/TalBenAmii/llm-d-bench-mcp)); the stub carries the install one-liner. |
| [SECURITY.md](reference/SECURITY.md) | operators / reviewers | Threat model: trust boundaries, the allowlist/approval model, secret scrubbing, network-exposure guidance, what requires isolation. |
| [TROUBLESHOOTING.md](guides/TROUBLESHOOTING.md) | operators | Symptom → what to check; debug mode; the structured logs + `corr_id`; the readiness/metrics endpoints. |
| [CONTRIBUTING.md](project/CONTRIBUTING.md) | contributors | How to add a tool/flow/phase; the two laws (thin-code, allowlist-as-data); the hermetic-test rule. |
| [CHANGELOG.md](project/CHANGELOG.md) | everyone | Keep-a-Changelog history (v1 phases 0-10, v2 operability phases 11-18, v3 proposal-completion phases 19-26 + token-tracking). |
| [INTERACTIVE_TEST_GUIDE.md](guides/INTERACTIVE_TEST_GUIDE.md) | contributors / testers | Follow-along runbook to drive every feature by hand with a real LLM. |
| [BENCHMARK_FEATURE_COVERAGE.md](reference/BENCHMARK_FEATURE_COVERAGE.md) | contributors / reviewers | Benchmark-CLI feature-coverage catalog (✅/🟡/⬜) — what's wired vs deferred. |
| [USEFUL_REPO_DOCS.md](reference/USEFUL_REPO_DOCS.md) | contributors | Curated index of which upstream `llm-d` / `llm-d-benchmark` docs matter and why. |
| [CONTEXT.md](reference/CONTEXT.md) | contributors / reviewers | Ubiquitous-language / domain glossary — the project's shared vocabulary (spec, harness, workload, SessionPlan, goodput, …) with "avoid" synonyms. |
| [CONFIG_AUDIT_LOG.md](project/CONFIG_AUDIT_LOG.md) | maintainers | Dated config/model-drift audit entries — the running log of configuration and model-version reconciliations. |
| [PROJECT_BRAIN_REFERENCE.md](reference/PROJECT_BRAIN_REFERENCE.md) | engineers / maintainers | Orientation hub for reference/historical material — status and pointers into the rest of the suite. |
| [UPSTREAM_REUSE_PATHS.md](reference/UPSTREAM_REUSE_PATHS.md) | contributors | Where to look in the READ-ONLY `llm-d-benchmark/` — the CLI entry point, specs, harnesses, and the Benchmark Report schema. |

Ops assets live under [`deploy/observability/`](../deploy/observability/): a Prometheus scrape
config, alert rules (`alerts.rules.yaml`), and a Grafana dashboard.

Project root: [`README.md`](../../README.md) (overview — at the repo root), [`CLAUDE.md`](../CLAUDE.md) (working
rules), [`FEATURES.md`](reference/FEATURES.md) (live, evidence-backed feature inventory + the
remaining/deferred phases), and [`plan.md`](history/plan.md) (design + status). The agent's
*judgment* lives in [`knowledge/`](../knowledge/).

Design history is archived under [`history/`](history/) — the original proposal + plan, plus
[`history/proposals/`](history/proposals/) (the five shipped feature proposals, kept as
design-of-record). UI screenshots used by docs/demos live in [`images/`](images/); the informal
working backlog is [`TODO.md`](project/TODO.md).

## Design in one line

**Thin code, thick agent.** Python is mechanism only (UI, agent loop, tools, security
allowlist, schema validation). All judgment lives in the LLM plus editable files under
`knowledge/`. Reliability comes from schema-validated handoffs at every boundary
([the four determinism gates](reference/ARCHITECTURE.md#the-four-determinism-gates)), not hard-coded
scripts.

## Upstream-PR readiness

This suite is the documentation deliverable on the path toward contributing the agent
upstream as a module in `llm-d-benchmark` (proposal §5.3 / §10). What's in place for that:

- **Architecture, API reference, deployment guide, and user guide** (this directory) — the
  four technical-documentation deliverables named in the proposal.
- **Read-only-repo discipline:** the agent never modifies `llm-d` / `llm-d-benchmark`; it
  reads their catalog, docs, and the Benchmark Report v0.2 schema *live* and shells out to
  the real `llmdbenchmark` CLI. That keeps the agent a clean, additive module.
- **Apache-2.0-compatible, additive surface:** the agent is self-contained under its own
  project folder with a deny-by-default security model and no vendored copies of repo
  internals — a drop-in front-end rather than a fork.
- **CI + hermetic tests:** the flow-validation harness ([VALIDATION.md](reference/VALIDATION.md)) and
  the full pytest suite run without an API key, Docker, kind, or a live cluster, so a
  reviewer can verify behavior deterministically.
- **One-command deploy:** a hardened image + Helm chart with
  least-privilege RBAC ([DEPLOYMENT.md](guides/DEPLOYMENT.md)).

Open items before a formal upstream PR are tracked in [`FEATURES.md`](reference/FEATURES.md)'s DEFERRED
phases and [`plan.md`](history/plan.md) ("Deferred / next").
