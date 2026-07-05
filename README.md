# llm-d Benchmarking Assistant Agent

**Benchmark [`llm-d`](https://github.com/llm-d/llm-d) by describing what you want in plain
English — no `llm-d-benchmark` expertise required.**

You say *"benchmark a chat app for ~500 concurrent users, p99 latency under 500 ms."* The agent
interviews you, inspects your environment, proposes a plan you approve, deploys an `llm-d` stack
if one isn't running, runs the benchmark, and explains the results — driving the real
`llmdbenchmark` CLI inside a strict security sandbox. Nothing changes your system without your
approval.

Beyond single runs it is a full benchmarking workbench: a Kubernetes-native run orchestrator, a
results analyzer (goodput / SLO / Pareto), multi-harness comparison, capacity pre-flight,
cross-session history & trends, shareable HTML reports, Prometheus/Grafana observability, a
one-command Helm deploy, and a companion **MCP server**
([`llm-d-bench-mcp`](https://github.com/TalBenAmii/llm-d-bench-mcp)) that plugs the whole toolset
into Claude Code. The evidence-backed feature inventory is
[`FEATURES.md`](llm-d-benchmarking-agent-project/docs/FEATURES.md).

All code lives in [`llm-d-benchmarking-agent-project/`](llm-d-benchmarking-agent-project/); the
sibling folders are read-only upstream repos the agent reads at runtime
([layout](#repository-layout)). Licensed Apache-2.0.

---

## Contents

- [Who this is for](#who-this-is-for)
- [Quick start](#quick-start)
- [Use it from Claude Code (MCP)](#use-it-from-claude-code-mcp)
- [Your first benchmark](#your-first-benchmark)
- [How you talk to it](#how-you-talk-to-it)
- [Feature showcase](#feature-showcase)
- [Reading the results](#reading-the-results)
- [How it stays safe](#how-it-stays-safe)
- [Going further](#going-further)
- [Under the hood](#under-the-hood)

---

## Who this is for

- **You want to benchmark `llm-d`** without learning the `<spec, harness, workload>` model, the
  CLI flags, or Kubernetes.
- **You have a use case, not a config.** "A chat assistant", "a RAG backend", "code completion
  at 200 req/s" — the agent maps it to a real benchmark profile.
- **You want it done safely.** Every system-changing step is shown to you and waits for a click.

The `llm-d-benchmark` expertise lives in the agent's editable brain
([`knowledge/`](llm-d-benchmarking-agent-project/knowledge/)), not in your head.

---

## Quick start

```bash
# One-liner — clones into ~/llm-d-benchmarking-agent, then installs everything:
bash <(curl -fsSL https://raw.githubusercontent.com/TalBenAmii/llm-d-benchmarking-agent/main/llm-d-benchmarking-agent-project/scripts/install.sh)

# …or clone first:
git clone https://github.com/TalBenAmii/llm-d-benchmarking-agent.git
cd llm-d-benchmarking-agent/llm-d-benchmarking-agent-project

./scripts/install.sh            # upstream repos + llm-d toolchain + benchmark CLI + this app
./scripts/install.sh --prereqs  # …also Docker + kind for the local quickstart (needs passwordless sudo)

./scripts/run.sh --open         # start the server and open http://127.0.0.1:8000
```

Then give it an LLM to think with. The installer's last step offers to wire your **Claude
subscription** (Pro/Max plan) for you — login, model pick, and a verified test call
(`./scripts/setup-claude-plan.sh` re-runs it anytime; `--no-llm-setup` skips it). Or set one
of these in `.env` by hand:

| Provider | `.env` settings |
|---|---|
| Your **Claude plan** — no API key | `LLM_PROVIDER=claude-agent-sdk` (or run `./scripts/setup-claude-plan.sh`) |
| **Anthropic** API (default) | `ANTHROPIC_API_KEY=...` |
| Any **OpenAI-compatible** endpoint | `LLM_PROVIDER=openai` + `OPENAI_API_KEY` (+ `OPENAI_BASE_URL`) |

To try it without a cluster, set `SIMULATE=1` (see [Simulate Mode](#simulate-mode)).

---

## Use it from Claude Code (MCP)

Prefer Claude Code (the CLI) over the web UI? A companion repo,
**[`llm-d-bench-mcp`](https://github.com/TalBenAmii/llm-d-bench-mcp)**, re-exposes this agent's
tools, workflow prompts, and full knowledge base as a standalone **MCP server** (`llm-d-bench`).
One command installs it — cloning this repo as its engine, building a venv, and registering the
server with Claude Code:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/TalBenAmii/llm-d-bench-mcp/main/scripts/install.sh)
```

No API key needed — it authenticates through your `claude` CLI login. It runs over stdio on your
machine; mutations are gated by Claude Code's own approval prompt. Full tool list, manual config,
and security model live in that repo (quick pointer:
[`docs/MCP.md`](llm-d-benchmarking-agent-project/docs/MCP.md)).

---

## Your first benchmark

The out-of-the-box path is the **quickstart**: a tiny `llm-d` stack on a local
[kind](https://kind.sigs.k8s.io/) cluster with a *simulated* inference engine — **no GPU, no
model download**. Open the UI and type:

> **Benchmark a small chat model on my laptop. I care about responsiveness — first token under
> 400 ms for most users.**

What unfolds (you approve the system-changing steps; read-only ones just run):

1. **Probe** — senses your environment (Docker? cluster? a stack already running?) and reads
   the real quickstart procedure from the repo docs.
2. **Plan** — proposes a **SessionPlan** as an Approve/Decline card (spec, harness, workload,
   namespace, the exact steps) and runs a capacity pre-flight. → *You approve.*
3. **Prepare** — installs missing prerequisites, clones the repos, creates the kind cluster.
   *Each step is a separate approval.*
4. **Run** — `standup` → `smoketest` → `run`, with output streaming live.
5. **Explain** — parses the validated **Benchmark Report** and answers in plain words:
   *"Median TTFT 180 ms, p99 320 ms — under your 400 ms target (goodput ≈ 96%)."*
6. **Teardown** — offers to tear everything down when you're done.

The only things that came from *you*: the goal, one SLO, and the approvals.

---

## How you talk to it

| Phase | The agent does | You do |
|---|---|---|
| **Interview** | Asks 2–3 questions to pin the use case + SLOs | Answer in plain language |
| **Probe** *(auto, read-only)* | Senses the environment, reads the real procedure from repo docs | Watch |
| **Plan** | Proposes a **SessionPlan** card + capacity check | **Approve / Reject** |
| **Prepare** | Installs prereqs, clones, creates the cluster | Approve each step |
| **Run** | `standup` → `smoketest` → `run`, streaming live | Approve the mutating steps |
| **Explain** | Parses the validated report, ties numbers to your goal | Ask follow-ups |
| **Teardown** | Offers to tear it all down | Approve when done |

**Tell it your SLOs up front** ("p99 TTFT under 500 ms, at least 1000 tok/s") — it turns a wall
of numbers into a **pass/fail verdict** and a **goodput** estimate.

Every command the agent runs appears in the chat, tagged read-only or mutating (the `>_` Debug
view lists the full trail). Navigate away mid-run and an approved benchmark keeps running; its
result shows up when you return. Chats are saved and replayable from the sidebar.

---

## Feature showcase

Everything below is triggered by *asking* — the agent picks the right tool.

### Run & deploy

| You want… | Say something like… |
|---|---|
| A basic benchmark | *"benchmark a small chat model"* |
| To benchmark a stack that's **already up** | *"I already have a stack running — just benchmark it"* |
| **Just a preview**, no changes | *"show me exactly what you'd run, but don't deploy anything"* |
| A specific model / config | *"benchmark Llama-3.1-8B with max-model-len 4096"* (authors a validated config) |
| To run it **as a Kubernetes Job** | *"orchestrate this run as a Job on my cluster"* (lifecycle, fault classification, retry/dead-letter) |

### Plan before you spend

| You want… | Say something like… |
|---|---|
| **"Will this even fit?"** | *"can I run an 8B model on one GPU?"* → GPU-memory pre-flight |
| To know which accelerator you have | *"what GPU does my cluster advertise?"* |
| The available specs/harnesses/workloads | *"what benchmark profiles can I choose from?"* → live catalog |

### Make sense of results

| You want… | Say something like… |
|---|---|
| **The best config for my SLOs** | *"which configuration is best for p99 < 500 ms?"* → goodput + **Pareto frontier** |
| To **compare two runs** | *"compare these two runs — which won?"* → per-metric deltas + winner |
| **Two harnesses contrasted** | *"run inference-perf and guidellm and compare them"* → cross-validated, no false winner |
| **"Has performance regressed?"** | *"how has TTFT trended over time?"* → cross-session history + sparkline |
| A **shareable report** | *"export this result as a report I can send my team"* → self-contained HTML |
| The harness's own charts | Rendered **inline** in the result card — latency-vs-QPS, throughput curves |

> The agent **only reports numbers from a validated Benchmark Report**. If a report is missing
> or invalid, it says so plainly — it never invents or estimates a metric.

### Explore the design space

| You want… | Say something like… |
|---|---|
| A **sweep** across configs | *"sweep batch size and concurrency and find the knee"* → design-of-experiments matrix, run in parallel under a concurrency cap |
| **Goal-seek to an SLO** | *"find the config that hits p99 < 400 ms at best goodput"* → iterative sweep rounds narrowed by the SLO-feasible frontier |
| **Live resource usage** during a run | *"is the server near its memory limit right now?"* |

### Trust, reproduce, recover

| You want… | Say something like… |
|---|---|
| A **reproducible bundle** | *"export a provenance bundle for this run"* → repo SHAs + resolved config + report digest |
| To **re-run an old experiment** | *"reproduce run X"* → re-derives the plan back through the approval + dry-run gates |
| To **cancel** a run started elsewhere | *"cancel the run in my other chat"* → frees its concurrency slot |

---

## Reading the results

The agent explains each in plain language; this is the glossary:

- **TTFT** (time to first token) — responsiveness; how long until the model starts replying.
- **TPOT / ITL** (time per output token / inter-token latency) — how fast text streams once started.
- **Throughput** — output tokens per second; how much work the stack sustains.
- **Percentiles (p50/p90/p95/p99)** — tail behavior; p99 latency is what your slowest users feel.
- **Goodput** — the fraction of requests that **meet your SLOs** (needs targets from you).

It also extracts richer signals when the harness emits them — **KV-cache hit rate, schedule
delay, GPU utilization** — and reports `None` rather than guessing when a harness doesn't.

---

## How it stays safe

- **Deny-by-default allowlist**
  ([`security/allowlist.yaml`](llm-d-benchmarking-agent-project/security/allowlist.yaml)) — the
  dedicated command tools can run *only* an explicit set of commands: `llmdbenchmark`; read-only
  `kubectl`/`kind`/`docker` probes; `git clone` of the llm-d repos; the install scripts;
  `kind create`/`delete cluster`. Allowlisted commands run as argv lists with `shell=False`. The
  policy is **data** — widening it is a reviewed config change, not a code change.
- **Per-action approval.** Read-only commands auto-run; every *mutating or unknown* command —
  including anything the general `run_shell` tool proposes — shows you the **exact command**
  and waits for Approve/Reject.
- **Full transparency.** Every command appears in the chat with a read-only/mutating badge.
  Nothing runs off-screen.
- **Secrets stay server-side.** LLM/HF keys live only in the backend; the browser never sees
  them, and child-process env is scrubbed.

### The four determinism gates

Reliability comes from **schema-validated handoffs**, not hard-coded scripts:

1. The LLM can act **only** through schema-validated tool calls.
2. Before any deployment it proposes a **SessionPlan** you approve.
3. Any generated config is validated via the CLI's own `--dry-run` / `plan` before it runs.
4. Results are parsed from the validated **Benchmark Report v0.2** — never scraped from logs.

---

## Going further

### Run on a real GPU

The quickstart is CPU-simulated. For **real vLLM inference and real numbers** on a single
NVIDIA GPU (e.g. a laptop card under WSL2), follow
[`docs/GPU_CLUSTER_RUNBOOK.md`](llm-d-benchmarking-agent-project/docs/GPU_CLUSTER_RUNBOOK.md) —
host enablement, a GPU-capable minikube, and a tiny-model scenario sized for 8 GB. The agent is
cluster-agnostic; it targets whatever your kubeconfig points at.

### Simulate Mode

Set `SIMULATE=1` in `.env` for a full dry run: the agent walks the **entire** workflow
(probe → plan → standup → smoketest → run → report) without deploying or benchmarking.
**Read-only** commands run **for real** so it gathers genuine context; **mutating** actions are
announced but no-opped to synthetic success, and a synthetic report is produced. The best way to
watch a guide end-to-end without touching a cluster.

### Observability

The agent exposes Prometheus metrics at `/metrics`, plus `/healthz` (liveness) and `/readyz`
(readiness + startup self-check). A Grafana dashboard, scrape config, and alert rules ship in
[`deploy/observability/`](llm-d-benchmarking-agent-project/deploy/observability/). During a run
it can show live cluster CPU/memory (needs the in-cluster metrics-server, which it offers to
install).

### Deploy to Kubernetes

```bash
cd llm-d-benchmarking-agent-project
docker build -t llm-d-benchmarking-agent:0.1.0 .
# make the image visible to your cluster, e.g.:  kind load docker-image llm-d-benchmarking-agent:0.1.0

helm install bench-agent deploy/helm/llm-d-benchmarking-agent \
  --namespace llmd-bench --create-namespace \
  --set image.repository=llm-d-benchmarking-agent \
  --set secret.anthropicApiKey=$ANTHROPIC_API_KEY
```

Runs **non-root** with a read-only root filesystem, sources keys from a Kubernetes Secret,
probes `/healthz`, exposes `/metrics`, and grants a **namespaced least-privilege Role**.
Optional Bearer-token auth (`AUTH_ENABLED`/`AUTH_TOKEN`) and a token-bucket rate limit
(`RATE_LIMIT_*`) harden a shared instance. See
[`docs/DEPLOYMENT.md`](llm-d-benchmarking-agent-project/docs/DEPLOYMENT.md).

---

## Under the hood

### Design in one line

**Thin code, thick agent.** The Python is only *mechanism* — a chat UI, an agent loop, the
tools, a command allowlist, schema validation. All *judgment* (which spec/harness/workload,
what flags, how to read results) lives in the LLM plus editable knowledge files under
[`knowledge/`](llm-d-benchmarking-agent-project/knowledge/). **Editing those Markdown/YAML
files changes the agent's behavior without touching code.**

### Verify it runs the *right* commands

A hermetic **flow-validation harness** replays each end-to-end flow (quickstart, teardown,
benchmarking a running stack, dry-run previews, out-of-policy refusals) through the *real*
agent loop + allowlist + approval gating, capturing every command without executing anything:

```bash
cd llm-d-benchmarking-agent-project
make validate        # deterministic & hermetic — no API key, Docker, kind, or repos needed
make flows           # list the known flows
make validate-live   # the real LLM drives each flow from natural language (needs a key)
pytest tests/        # the full suite (also hermetic)
```

### Layout

All paths under [`llm-d-benchmarking-agent-project/`](llm-d-benchmarking-agent-project/):

| Path | What |
|---|---|
| `app/` | FastAPI backend: agent loop, tools, orchestrator, security, validation (mechanism only) |
| `security/allowlist.yaml` | The deny-by-default command policy (data) |
| `knowledge/` | The agent's editable brain — playbooks & heuristics (no code) |
| `ui/` | Static chat UI (HTML/JS/CSS) |
| `scripts/` | Entry points (`install.sh`, `run.sh`, `setup-claude-plan.sh`) + helpers |
| `deploy/` | Dockerfile assets, Helm chart, observability |
| `tests/` | pytest (unit + integration + flow validation) |

### Documentation

| Doc | For |
|---|---|
| [docs/USER_GUIDE.md](llm-d-benchmarking-agent-project/docs/USER_GUIDE.md) | Using the agent end-to-end |
| [docs/MCP.md](llm-d-benchmarking-agent-project/docs/MCP.md) | Pointer to the standalone MCP server repo ([llm-d-bench-mcp](https://github.com/TalBenAmii/llm-d-bench-mcp)) |
| [docs/GPU_CLUSTER_RUNBOOK.md](llm-d-benchmarking-agent-project/docs/GPU_CLUSTER_RUNBOOK.md) | From CPU-sim to a real single-GPU cluster |
| [docs/ARCHITECTURE.md](llm-d-benchmarking-agent-project/docs/ARCHITECTURE.md) | Layers, components, determinism gates, trust boundaries |
| [docs/API.md](llm-d-benchmarking-agent-project/docs/API.md) | The HTTP/WebSocket API + tool surface + `SessionPlan` |
| [docs/DEPLOYMENT.md](llm-d-benchmarking-agent-project/docs/DEPLOYMENT.md) | Local and in-cluster (Helm), config, secrets, RBAC |
| [docs/VALIDATION.md](llm-d-benchmarking-agent-project/docs/VALIDATION.md) | The flow-validation harness |
| [docs/FEATURES.md](llm-d-benchmarking-agent-project/docs/FEATURES.md) | The evidence-backed feature inventory + how to verify each |

Full index: [`docs/README.md`](llm-d-benchmarking-agent-project/docs/README.md).

### Repository layout

This repo is a monorepo: the project sits alongside **read-only** upstream repos it reads at
runtime, plus one **owned** sibling — the split-out MCP server:

```
llm-d-benchmarking-agent/
├── llm-d/                            # deployment guides (read-only)
├── llm-d-benchmark/                  # the llmdbenchmark CLI (read-only)
├── llm-d-skills/                     # upstream skills library (read-only)
├── llm-d-bench-mcp/                  # the standalone MCP server — owned sibling (its own git repo)
└── llm-d-benchmarking-agent-project/ # this project — the app's code and docs
```
