# Documentation

The technical documentation for the llm-d Benchmarking Agent: a conversational agent,
Kubernetes-native benchmark orchestrator, and results analyzer for
[`llm-d-benchmark`](https://github.com/llm-d/llm-d-benchmark).

| Doc | For | Covers |
|---|---|---|
| [ARCHITECTURE.md](reference/ARCHITECTURE.md) | engineers / reviewers | System design: layers, components, the four determinism gates, trust boundaries. |
| [API.md](reference/API.md) | integrators / contributors | The HTTP/WebSocket API and the agent tool surface (inputs, classification, result shapes) + the `SessionPlan`. |
| [DEPLOYMENT.md](guides/DEPLOYMENT.md) | operators | Running locally and in-cluster (Helm), configuration, secrets, least-privilege RBAC, observability. |
| [CLUSTER_SERVICE_DEPLOY.md](guides/CLUSTER_SERVICE_DEPLOY.md) | maintainers / operators | Deploying the agent as an in-cluster service: image build/publish, `install_service.sh` (Helm), persistence, testing. |
| [USER_GUIDE.md](guides/USER_GUIDE.md) | end users | Using the agent end-to-end with no `llm-d-benchmark` expertise. |
| [GPU_CLUSTER_RUNBOOK.md](guides/GPU_CLUSTER_RUNBOOK.md) | end users / operators | Real single-GPU cluster (minikube + NVIDIA): setup, a tiny-model scenario, what's real vs simulated on one card. |
| [VALIDATION.md](reference/VALIDATION.md) | contributors | The flow-validation harness: proving the agent runs the right commands. |
| [MCP.md](reference/MCP.md) | Claude Code users | Pointer to the split-out `llm-d-bench` MCP server repo ([llm-d-bench-mcp](https://github.com/TalBenAmii/llm-d-bench-mcp)). |
| [SECURITY.md](reference/SECURITY.md) | operators / reviewers | Threat model: trust boundaries, command policy/approvals, secret scrubbing, exposure guidance. |
| [TROUBLESHOOTING.md](guides/TROUBLESHOOTING.md) | operators | Symptom → what to check; debug mode; structured logs; readiness/metrics endpoints. |
| [INTERACTIVE_TEST_GUIDE.md](guides/INTERACTIVE_TEST_GUIDE.md) | contributors / testers | Follow-along runbook to drive every feature by hand with a real LLM. |
| [BENCHMARK_FEATURE_COVERAGE.md](reference/BENCHMARK_FEATURE_COVERAGE.md) | contributors / reviewers | Benchmark-CLI feature-coverage catalog (✅/🟡/⬜): what's wired, per upstream feature. |
| [USEFUL_REPO_DOCS.md](reference/USEFUL_REPO_DOCS.md) | contributors | Curated index of which upstream `llm-d` / `llm-d-benchmark` docs matter and why. |
| [CONTEXT.md](reference/CONTEXT.md) | contributors / reviewers | Domain glossary: the project's shared vocabulary (spec, harness, workload, SessionPlan, goodput, …) with "avoid" synonyms. |
| [PROJECT_BRAIN_REFERENCE.md](reference/PROJECT_BRAIN_REFERENCE.md) | engineers / maintainers | Orientation hub for reference/historical material: status and pointers into the rest of the suite. |
| [UPSTREAM_REUSE_PATHS.md](reference/UPSTREAM_REUSE_PATHS.md) | contributors | Where to look in the READ-ONLY `llm-d-benchmark/`: the CLI entry point, specs, harnesses, and the Benchmark Report schema. |

Ops assets live under [`deploy/observability/`](../deploy/observability/): a Prometheus scrape
config, alert rules (`alerts.rules.yaml`), and a Grafana dashboard.

Project root: [`README.md`](../../README.md) (overview, at the repo root), [`CLAUDE.md`](../CLAUDE.md) (working
rules), and [`FEATURES.md`](reference/FEATURES.md) (live, evidence-backed feature
inventory). The agent's judgment lives in [`knowledge/`](../knowledge/); UI screenshots used by
docs/demos live in [`images/`](images/).

Design ("thin code, thick agent") → the [root README](../../README.md) +
[the four determinism gates](reference/ARCHITECTURE.md#the-four-determinism-gates).
