# llm-d-bench — the MCP server

**Give Claude Code the ability to benchmark `llm-d` from plain English.** Point it at this
server and it can probe a cluster, propose a benchmark plan you approve, deploy an `llm-d`
stack, run the benchmark, and explain the results — inside the same security sandbox and
approval gates as the standalone app.

> **Supported now:** the `claude-agent-sdk` provider (no API key) wired into **Claude Code
> (the CLI)** — the only path the installer sets up and verifies. The server speaks standard
> MCP, so other providers (`anthropic`, `openai`) and clients (Claude Desktop, Cursor, VS Code,
> OpenAI Codex CLI) are planned for a future release.

It is the agent's toolset re-exposed over the Model Context Protocol: **35 tools**, **5
workflow prompts**, and the agent's entire **knowledge base** (60+ playbooks & heuristics) as
readable resources — so a generic agent behaves like a benchmarking expert, not a blank slate.

> Transport is **stdio / local single-user**: the server runs on *your* machine against *your*
> kubeconfig, trusted like any local tool you launch. There is no network/remote mode (see
> [Security & scope](#security--scope)).

## Install (one command)

The installer fetches the project, clones the read-only sibling repos, builds a virtualenv,
configures the `claude-agent-sdk` provider, and registers the server with Claude Code (or
prints the config to paste yourself):

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/TalBenAmii/llm-d-benchmarking-agent/main/llm-d-benchmarking-agent-project/scripts/install-mcp.sh)
```

Prefer to clone first? The same script runs from inside a checkout:

```bash
git clone https://github.com/TalBenAmii/llm-d-benchmarking-agent.git
cd llm-d-benchmarking-agent/llm-d-benchmarking-agent-project
./scripts/install-mcp.sh
```

It is idempotent (safe to re-run). The `claude-agent-sdk` provider needs **no API key** — it
authenticates through your `claude` CLI login, so the only prerequisite is that the `claude`
CLI is installed and logged in.

## What your agent gets

### Tools (35)

| Group | What your agent can do | Examples |
|---|---|---|
| Sense & ground *(read-only, auto-run)* | Inspect the environment, GPUs, catalog, docs, knowledge | `probe_environment`, `advise_accelerators`, `list_catalog`, `discover_stack`, `search_knowledge`, `read_knowledge`, `fetch_key_docs`, `read_repo_doc` |
| Plan before you spend | Map a use case to a validated plan; check it fits | `propose_session_plan`, `check_capacity`, `estimate_run_duration`, `write_and_validate_config`, `generate_doe_experiment` |
| Deploy & run *(approval-gated)* | Set up repos, run the CLI, orchestrate Jobs & sweeps | `ensure_repos`, `run_setup`, `execute_llmdbenchmark`, `orchestrate_benchmark_run`, `orchestrate_sweep`, `provision_hf_secret` |
| Make sense of results *(read-only)* | Parse reports, compare runs/harnesses, track trends | `locate_and_parse_report`, `analyze_results`, `compare_reports`, `compare_harness_runs`, `aggregate_runs`, `result_history` |
| Observe & manage | Readiness checks, live cluster metrics, run management | `check_endpoint_readiness`, `observe_run_metrics`, `manage_orchestrated_runs`, `cancel_run` |
| Trust & reproduce | Provenance bundles, reproduce a run | `export_run_bundle`, `reproduce_run` |

Numbers are only ever reported from a validated **Benchmark Report v0.2** — never scraped from
logs or invented.

### Workflow prompts (5)

Entry points that drop your agent into the right playbook:

| Prompt | Arguments | What it sets up |
|---|---|---|
| `benchmark_this_model` | `model?`, `goal?`, `slo?` | The full interview → preconditions → plan → run → explain workflow |
| `pick_deploy_path` | `model?`, `accelerator?` | Choosing a deploy path + accelerator guidance |
| `interpret_this_report` | `report_path?` | Parsing and explaining a benchmark report |
| `design_a_sweep` | `objective?` | Designing a design-of-experiments sweep |
| `goal_seek_to_slo` | `slo` | Iterative sweep rounds toward an SLO at best goodput |

### Resources & instructions

Every knowledge file is exposed as a `doc://knowledge/<name>` resource, so your agent can read
the same playbooks the standalone agent reasons over. The server also advertises a
role/workflow preamble in its MCP `instructions` ("probe first, ground in docs, propose a plan,
run only with approval") that capable clients fold into their system prompt.

## Manual config (Claude Code)

The installer does this for you; here's the block to wire it up by hand. The launch command is
the console entry point created by `pip install -e .` — use its **absolute** path in your venv:

```bash
claude mcp add llm-d-bench -s user -- /ABS/PATH/.venv/bin/llm-d-bench-mcp
# verify:  claude mcp list   (or /mcp inside a session)
```

A gated-model `HF_TOKEN` is optional — add it with `-e HF_TOKEN=hf_xxx`; your `.env` already
carries the LLM provider config and is loaded regardless of how the server is launched. If you
skip `pip install -e .`, launch via the module instead:

```bash
claude mcp add llm-d-bench -s user -e PYTHONPATH=/ABS/PATH -- /ABS/PATH/.venv/bin/python -m app.mcp
```

Smoke-test it without a client using the official inspector:

```bash
npx @modelcontextprotocol/inspector /ABS/PATH/.venv/bin/llm-d-bench-mcp
```

## Requirements & scope

- **Python ≥ 3.11** and `git` (the installer handles the venv via `uv`, or `python3 -m venv`).
- **LLM provider**: `claude-agent-sdk` — no API key, authenticated via your `claude` CLI login.
- **Client**: Claude Code (the CLI).
- **No cluster needed** for the advisory tools and knowledge resources. The
  deploy/run/orchestrate tools need a reachable Kubernetes cluster + `kubeconfig` (and
  `HF_TOKEN` for gated models).
- The read-only sibling repos (`llm-d`, `llm-d-benchmark`, `llm-d-skills`) must sit next to the
  project — the installer clones them automatically.

## Security & scope

- **stdio / local single-user only.** The server has no network listener and no per-caller
  auth; it acts with your own kubeconfig. This is acceptable *only* for local use —
  HTTP/remote/shared transport is deliberately deferred, and "who may connect, whose
  credentials, what blast radius" become blocking questions before any such mode.
- **Approval is re-homed to your client.** Every tool call is gated by your MCP client's own
  tool-permission prompt; the richer `SessionPlan` approval uses MCP elicitation where the
  client supports it (with a graceful fallback otherwise). Nothing mutating runs without your
  say-so — never a silent auto-approve.

Design of record and rationale: [`app/mcp/DESIGN.md`](../app/mcp/DESIGN.md). Project overview:
[the root README](../../README.md).
