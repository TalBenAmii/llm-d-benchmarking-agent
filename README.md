# llm-d-bench — an MCP server for benchmarking llm-d

**Give Claude Code the ability to benchmark `llm-d` from plain English.** Point it at this
server and it can probe a cluster, propose a benchmark plan you approve, deploy an `llm-d` stack,
run the benchmark, and explain the results — driving the real `llmdbenchmark` CLI on your behalf,
inside the same security sandbox and approval gates as the standalone app.

> **Supported now:** the `claude-agent-sdk` provider (no API key) wired into **Claude Code (the
> CLI)** — the only path the installer sets up and the one we verify. The server speaks standard
> MCP, so other providers (`anthropic`, `openai`) and clients (Claude Desktop, Cursor, VS Code,
> OpenAI Codex CLI) are planned for a future release.

It is the agent's full toolset re-exposed over the Model Context Protocol: **37 tools**, **5
workflow prompts**, and the agent's entire **knowledge base** (~50 playbooks & heuristics) as
readable resources — so a generic agent behaves like a benchmarking expert, not a blank slate.

> Transport is **stdio / local single-user**: the server runs on *your* machine against *your*
> kubeconfig, trusted like any local tool you launch. There is no network/remote mode (see
> [Security & scope](#security--scope)).

## Install (one command)

The installer fetches the project, clones the read-only sibling repos, builds a virtualenv,
configures the `claude-agent-sdk` provider, and registers the server with Claude Code for you
(or prints the config to paste yourself):

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/TalBenAmii/llm-d-benchmarking-agent/main/llm-d-benchmarking-agent-project/scripts/install-mcp.sh)
```

Prefer to clone first? The same script runs from inside a checkout:

```bash
git clone https://github.com/TalBenAmii/llm-d-benchmarking-agent.git
cd llm-d-benchmarking-agent/llm-d-benchmarking-agent-project
./scripts/install-mcp.sh
```

It is idempotent (safe to re-run). The provider is `claude-agent-sdk`, which needs **no API key**
— it authenticates through your installed `claude` CLI login, so the only prerequisite is that the
`claude` CLI is installed and logged in.

## What your agent gets

### Tools (37)

| Group | What your agent can do | Examples |
|---|---|---|
| Sense & ground *(read-only, auto-run)* | Inspect the environment, GPUs, catalog, docs, knowledge | `probe_environment`, `advise_accelerators`, `list_catalog`, `discover_stack`, `read_knowledge`, `fetch_key_docs` |
| Plan before you spend | Map a use case to a validated plan; check it fits | `propose_session_plan`, `check_capacity`, `write_and_validate_config`, `generate_doe_experiment` |
| Deploy & run *(approval-gated)* | Set up repos, run the CLI, orchestrate Jobs & sweeps | `ensure_repos`, `run_setup`, `execute_llmdbenchmark`, `orchestrate_benchmark_run`, `orchestrate_sweep`, `provision_hf_secret` |
| Make sense of results *(read-only)* | Parse reports, compare runs/harnesses, track trends | `locate_and_parse_report`, `analyze_results`, `compare_reports`, `compare_harness_runs`, `result_history` |
| Tune & observe | Closed-loop search to an SLO; live cluster metrics | `autotune_search`, `observe_run_metrics`, `manage_orchestrated_runs`, `cancel_run` |
| Trust & recover | Provenance bundles, reproduce a run, resilience drill | `export_run_bundle`, `reproduce_run`, `run_resilience_drill` |

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
| `autotune_to_slo` | `slo` | Closed-loop auto-tuning toward an SLO |

### Resources & instructions

Every knowledge file is exposed as a `doc://knowledge/<name>` resource, so your agent can read
the same playbooks and heuristics the standalone agent reasons over. The server also advertises a
role/workflow preamble in its MCP `instructions` ("probe first, ground in docs, propose a plan,
run only with approval") that capable clients fold into their system prompt.

## Manual config (Claude Code)

The installer does this for you, but here's the block if you'd rather wire it up by hand. The
launch command is the console entry point created by `pip install -e .` — use its **absolute**
path in your venv (e.g. `/abs/path/.venv/bin/llm-d-bench-mcp`):

```bash
claude mcp add llm-d-bench -s user -- /ABS/PATH/.venv/bin/llm-d-bench-mcp
# verify:  claude mcp list   (or /mcp inside a session)
```

A gated-model `HF_TOKEN` is optional — add it with `-e HF_TOKEN=hf_xxx`; your `.env` already
carries the LLM provider config and is loaded by the server regardless of how it's launched. If
you skip `pip install -e .`, launch via the module instead:

```bash
claude mcp add llm-d-bench -s user -e PYTHONPATH=/ABS/PATH -- /ABS/PATH/.venv/bin/python -m app.mcp
```

Smoke-test it without a client using the official inspector:

```bash
npx @modelcontextprotocol/inspector /ABS/PATH/.venv/bin/llm-d-bench-mcp
```

> Other MCP clients — Claude Desktop, Cursor, VS Code, OpenAI Codex CLI — are planned for a future
> release; for now the installer wires up and verifies only Claude Code (the CLI).

## Requirements & scope

- **Python ≥ 3.11** and `git` (the installer handles the venv via `uv`, or `python3 -m venv`).
- **LLM provider**: `claude-agent-sdk` — no API key, authenticated via your installed `claude`
  CLI login. (Other providers — `anthropic`, `openai` — are planned for a future release.)
- **Client**: Claude Code (the CLI). (Claude Desktop, Cursor, VS Code, and OpenAI Codex CLI
  support is planned for a future release.)
- **No cluster needed** for the advisory tools and knowledge resources. The deploy/run/orchestrate
  tools need a reachable Kubernetes cluster + `kubeconfig` (and `HF_TOKEN` for gated models).
- The read-only sibling repos (`llm-d`, `llm-d-benchmark`, `llm-d-skills`) must sit next to the
  project — the installer clones them automatically.

## Security & scope

- **stdio / local single-user only.** The server has no network listener and no per-caller auth; it
  acts with your own kubeconfig. This is acceptable *only* for local use — HTTP/remote/shared
  transport is deliberately deferred, and "who may connect, whose credentials, what blast radius"
  become blocking questions before any such mode.
- **Approval is re-homed to your client.** Every tool call is gated by your MCP client's own
  tool-permission prompt; the richer `SessionPlan` approval uses MCP elicitation where the client
  supports it (with a graceful fallback otherwise). Nothing mutating runs without your say-so — it
  is never a silent auto-approve.

This repository is a monorepo: the agent lives in
[`llm-d-benchmarking-agent-project/`](llm-d-benchmarking-agent-project/) alongside the read-only
upstream repos it reads at runtime. Design of record and rationale for the server:
[`app/mcp/DESIGN.md`](llm-d-benchmarking-agent-project/app/mcp/DESIGN.md). Full project overview and
feature showcase (the web UI, orchestrator, analyzer, deploy):
[the project README](llm-d-benchmarking-agent-project/README.md). Licensed under Apache-2.0.
