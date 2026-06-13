# llm-d Benchmarking Assistant Agent

**Benchmark `llm-d` by describing what you want in plain English — no `llm-d-benchmark`
expertise required.**

You say *"benchmark a chat app for ~500 concurrent users, p99 latency under 500 ms."* The
agent interviews you, inspects your environment, proposes a plan you approve, deploys an
`llm-d` stack if one isn't already running, runs the benchmark, and explains the results —
driving the real `llmdbenchmark` CLI on your behalf, inside a strict security sandbox, asking
your approval before anything changes your system.

It exposes **32 tools** and has grown well past the original quickstart MVP into a full
benchmarking workbench: a Kubernetes-native run orchestrator, a results analyzer (goodput /
SLO / Pareto), multi-harness comparison, capacity pre-flight, cross-session history & trends,
Prometheus/Grafana observability, and a one-command Helm/Kustomize deploy.

---

## Contents

- [Who this is for](#who-this-is-for)
- [Quick start](#quick-start) — get it running in two commands
- [Your first benchmark](#your-first-benchmark) — a guided walkthrough
- [How you talk to it](#how-you-talk-to-it) — the conversation loop
- [Feature showcase](#feature-showcase) — *say this → the agent does that*, covering everything
- [Reading the results](#reading-the-results) — the metrics, in plain words
- [How it stays safe](#how-it-stays-safe) — approvals, the allowlist, determinism gates
- [Going further](#going-further) — GPU, Simulate Mode, Kubernetes deploy
- [Under the hood](#under-the-hood) — design, layout, docs map

---

## Who this is for

- **You want to benchmark `llm-d`** but don't know (or don't want to learn) the
  `<spec, harness, workload>` model, the CLI flags, or Kubernetes.
- **You have a use case, not a config.** "A chat assistant", "a RAG backend", "code
  completion at 200 req/s" — the agent maps that to a real benchmark profile.
- **You want it done safely.** Everything that touches your system is shown to you and waits
  for a click. Nothing runs off-screen.

You do **not** need: the CLI syntax, kubectl/Helm, vLLM flags, or the report schema. That
knowledge lives in the agent's editable "brain" ([`knowledge/`](knowledge/)), not in your head.

---

## Quick start

**First-time, full bootstrap** — `install.sh` sets up *everything* needed to actually run a
benchmark: on a fresh Debian/Ubuntu box it bootstraps the base tools it needs (`git`, `curl`,
`python3-venv`) automatically, then clones the two upstream repos if missing, installs the
`llm-d` client toolchain (kubectl/helm/helmfile/kustomize/yq) and the `llmdbenchmark` CLI, and
finally builds this project's venv + `.env`. Add `--prereqs` to also install Docker + kind
(needs passwordless sudo):

```bash
./install.sh            # repos + client deps + benchmark CLI + this app
./install.sh --prereqs  # …and Docker + kind for the local kind quickstart
```

**Then launch** — `run.sh` builds the venv (if needed), installs the app, ensures a `.env`,
and starts the server:

```bash
./run.sh            # then open http://127.0.0.1:8000
./run.sh --open     # …and open it in your browser automatically
```

**Give it an LLM to think with** (one of):

- An **Anthropic** key: set `ANTHROPIC_API_KEY` in `.env` (default `LLM_PROVIDER=anthropic`).
- Any **OpenAI-compatible** endpoint: `LLM_PROVIDER=openai` + `OPENAI_API_KEY` (+ `OPENAI_BASE_URL`).
- The **local Claude Code** login — **no API key needed**: `LLM_PROVIDER=claude-agent-sdk`
  (authenticates through your installed `claude` CLI).

That's the only configuration most people need. To try it **without a cluster at all**, set
`SIMULATE=1` (see [Simulate Mode](#simulate-mode)).

Manual install instead of `run.sh`:

```bash
cp .env.example .env          # set your provider + key
pip install -e .              # or: uv pip install -e .
uvicorn app.main:app --reload # open http://127.0.0.1:8000
```

---

## Your first benchmark

The out-of-the-box supported path is the **quickstart**: a tiny `llm-d` stack on a local
[kind](https://kind.sigs.k8s.io/) cluster using a *simulated* inference engine — **no GPU, no
model download**. Open the UI and type:

> **Benchmark a small chat model on my laptop. I care about responsiveness — first token
> under 400 ms for most users.**

Here's what unfolds (you approve the system-changing steps; read-only ones just run):

1. **Probe** — it senses your environment (Docker? cluster? a stack already running?) and
   reads the *real* quickstart procedure from the repo docs, so it works from truth, not memory.
2. **Plan** — it proposes a **SessionPlan** as an Approve/Decline card:
   `spec=cicd/kind`, `harness=inference-perf`, `workload=sanity_random.yaml`, namespace
   `llmd-quickstart`, plus the exact steps. It also runs a **capacity pre-flight** to confirm
   it'll fit. → *You approve.*
3. **Prepare** — installs missing prerequisites (Docker + the kind binary, via one vetted
   installer), clones the repo, builds the tooling, creates the kind cluster. *Each step is a
   separate approval.*
4. **Run** — `standup` → `smoketest` → `run`, with output streaming live into the console.
5. **Explain** — it parses the validated **Benchmark Report** and tells you, in plain words:
   *"Median TTFT 180 ms, p99 320 ms — under your 400 ms target (goodput ≈ 96%). Throughput
   held 1,240 tok/s."*
6. **Teardown** — it offers to tear everything down when you're done.

The only things that came from *you*: the goal, one SLO, and the approvals.

---

## How you talk to it

| Phase | The agent does | You do |
|---|---|---|
| **Interview** | Asks 2–3 brief clarifying questions to pin the use case + SLOs | Answer in plain language |
| **Probe** *(auto, read-only)* | Senses the environment, reads the real procedure from repo docs | Watch |
| **Plan** | Proposes a **SessionPlan** card + runs a capacity check | **Approve / Reject** |
| **Prepare** | Installs prereqs, clones, builds, creates the cluster | Approve each |
| **Run** | `standup` → `smoketest` → `run`, streaming live | Approve the mutating steps |
| **Explain** | Parses the validated report, ties numbers back to your goal | Read; ask follow-ups |
| **Teardown** | Offers to tear it all down | Approve when done |

**Tell it your SLOs up front** ("p99 TTFT under 500 ms, at least 1000 tok/s") — it's the
single most valuable thing you can volunteer. It turns a wall of numbers into a **pass/fail
verdict** and a **goodput** estimate.

Every command the agent runs — including the automatic read-only probes — appears in the chat.
The one-click **Debug view** (`>_`) lists the executed-command trail inline, each tagged
read-only or mutating. Navigate away mid-run and an already-approved benchmark keeps running;
its result shows up when you return. Chats are saved and replayable from the sidebar.

---

## Feature showcase

Everything below is something you trigger by *asking* — the agent picks the right tool. This
is the full surface (the 32 tools, grouped by what you'd want).

### Run & deploy

| You want… | Say something like… |
|---|---|
| A basic benchmark | *"benchmark a small chat model"* |
| To benchmark a stack that's **already up** | *"I already have a stack running — just benchmark it"* (it detects the stack and skips redeploy) |
| **Just a preview**, no changes | *"show me exactly what you'd run, but don't deploy anything"* (read-only `plan` / `--dry-run`) |
| A specific model / config | *"benchmark Llama-3.1-8B with max-model-len 4096"* (it authors a validated config) |
| To run it **as a Kubernetes Job** | *"orchestrate this run as a Job on my cluster"* (Job lifecycle, fault classification, retry/dead-letter) |

### Plan before you spend

| You want… | Say something like… |
|---|---|
| **"Will this even fit?"** | *"can I run an 8B model on one GPU?"* → `check_capacity` GPU-memory pre-flight |
| To know which accelerator you have | *"what GPU does my cluster advertise?"* → `advise_accelerators` |
| To see the available specs/harnesses/workloads | *"what benchmark profiles can I choose from?"* → live catalog |

### Make sense of results

| You want… | Say something like… |
|---|---|
| **The best config for my SLOs** | *"which configuration is best for p99 < 500 ms?"* → goodput + **Pareto frontier** |
| To **compare two runs** | *"compare these two runs — which won?"* → per-metric deltas + winner |
| **Two harnesses contrasted** | *"run inference-perf and guidellm and compare them"* → cross-validated, no false winner |
| **"Has performance regressed?"** | *"how has TTFT trended over time?"* → cross-session history + sparkline |
| The harness's own charts | They're surfaced **inline** — latency-vs-QPS, throughput curves render in the result card |

> The agent **only ever reports numbers from a validated Benchmark Report**. If a report is
> missing or invalid, it says so plainly — it never invents or estimates a metric.

### Explore the design space

| You want… | Say something like… |
|---|---|
| A **sweep** across configs | *"sweep batch size and concurrency and find the knee"* → `generate_doe_experiment` (parallel, concurrency-capped) |
| **Auto-tune to an SLO** | *"find the config that hits p99 < 400 ms at best goodput"* → `autotune_search` closed-loop search |
| **Live resource usage** during a run | *"is the server near its memory limit right now?"* → `observe_run_metrics` (`kubectl top`) |

### Trust, reproduce, recover

| You want… | Say something like… |
|---|---|
| A **reproducible bundle** | *"export a provenance bundle for this run"* → repo SHAs + resolved config + report digest |
| To **re-run an old experiment** | *"reproduce run X"* → re-derives the plan back through the approval + dry-run gates |
| To **cancel** a run started elsewhere | *"cancel the run in my other chat"* → frees its concurrency slot |
| To prove it **survives faults** (opt-in) | *"run a resilience drill"* → injects faults in an in-process cluster (double-gated, never touches a real one) |

---

## Reading the results

The agent explains each in plain language; this is the glossary:

- **TTFT** (time to first token) — responsiveness; how long until the model starts replying.
- **TPOT / ITL** (time per output token / inter-token latency) — how fast text streams once started.
- **Throughput** — output tokens per second; how much work the stack sustains.
- **Percentiles (p50/p90/p95/p99)** — tail behavior; p99 latency is what your slowest users feel.
- **Goodput** — the fraction of requests that would **meet your SLOs** (only meaningful if you gave it targets).

It also extracts richer signals when the harness emits them — **KV-cache hit rate, schedule
delay, GPU utilization** — and reports `None` rather than guessing when a harness doesn't.

---

## How it stays safe

The whole point is that you can hand it real install/deploy privileges without worry.

- **Deny-by-default allowlist** ([`security/allowlist.yaml`](security/allowlist.yaml)) — the
  agent can run *only* an explicit set of commands: `llmdbenchmark`; read-only
  `kubectl`/`kind`/`docker` probes; `git clone` of the llm-d repos; `install.sh`;
  `kind create`/`delete cluster`; and the vetted
  [`scripts/install_prereqs.sh`](scripts/install_prereqs.sh) — the *only* way it can install
  Docker + the kind binary. There is **no** raw `apt`/`curl`/`sudo`. The policy is **data**;
  widening it is a reviewed config change, not a code change.
- **No shell, ever.** Commands run as argv lists with `shell=False` — command injection is
  structurally impossible.
- **Per-action approval.** Read-only probes auto-run; every *mutating* command (install,
  cluster create/delete, standup, run, teardown) shows you the **exact command** and waits for
  Approve/Reject.
- **Full transparency.** Every command appears in the chat; the Debug view lists the
  executed-command trail with read-only/mutating badges. Nothing runs off-screen.
- **Secrets stay server-side.** Your LLM/HF keys live only in the backend; the browser never
  sees them, and child-process env is scrubbed.

### The four determinism gates

Reliability comes from **schema-validated handoffs**, not hard-coded scripts:

1. The LLM can act **only** through schema-validated tool calls.
2. Before any deployment it proposes a **SessionPlan** you approve.
3. Any generated config is validated via the CLI's own `--dry-run` / `plan` before it runs.
4. Results are parsed from the repo's validated **Benchmark Report v0.2** — never scraped from logs.

---

## Going further

### Run on a real GPU

The quickstart is CPU-simulated. To get **real vLLM inference and real throughput/latency
numbers** on a single NVIDIA GPU (e.g. a laptop card under WSL2), follow
[`docs/GPU_CLUSTER_RUNBOOK.md`](docs/GPU_CLUSTER_RUNBOOK.md) — it covers host enablement, a
GPU-capable minikube, and a tiny-model scenario sized for 8 GB, then hands the cluster to the
agent's normal deploy → benchmark → observe loop (the agent is cluster-agnostic; it targets
whatever your kubeconfig points at).

### Simulate Mode

Set `SIMULATE=1` in `.env` for a full dry run: the agent walks the **entire** workflow
(probe → plan → standup → smoketest → run → report) but **executes nothing** — every command
is a no-op returning synthetic success, per-command approvals are skipped (the upfront plan
approval is kept), and a synthetic report is produced. The best way to *watch a guide
end-to-end without touching a cluster.*

### Observability

The agent exposes its own Prometheus metrics at `/metrics` (command counts/durations,
orchestrator run/fault counters), `/healthz` (liveness) and `/readyz` (readiness + startup
self-check). A Grafana dashboard, scrape config, and alert rules ship in
[`deploy/observability/`](deploy/observability/). During a run, `observe_run_metrics` shows
live cluster CPU/memory (needs the in-cluster metrics-server, which the agent offers to install).

### Deploy to Kubernetes (production image + one command)

```bash
docker build -t llm-d-benchmarking-agent:0.1.0 .

# Helm:
helm install bench-agent deploy/helm/llm-d-benchmarking-agent \
  --namespace llmd-bench --create-namespace \
  --set secret.anthropicApiKey=$ANTHROPIC_API_KEY

# …or Kustomize (Helm-free):
kubectl apply -k deploy/kustomize/base
```

Runs **non-root** with a read-only root filesystem, sources keys from a Kubernetes Secret
(never baked into the image), probes `/healthz`, exposes `/metrics`, and grants a **namespaced
least-privilege Role**. Optional Bearer-token auth (`AUTH_ENABLED`/`AUTH_TOKEN`) and a
token-bucket rate limit (`RATE_LIMIT_*`) harden a shared instance. See
[`knowledge/packaging.md`](knowledge/packaging.md) and [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

---

## Under the hood

### Design in one line

**Thin code, thick agent.** The Python is only *mechanism* — a chat UI, an agent loop, the
tools, a command allowlist, schema validation. All *judgment* (which spec/harness/workload,
what flags, how to read results) lives in the LLM plus editable knowledge files under
[`knowledge/`](knowledge/). **Editing those Markdown/YAML files changes the agent's behavior
without touching code** — the mechanism is fixed; the brain is yours to tune.

### Verify it runs the *right* commands

A hermetic **flow-validation harness** replays each end-to-end flow (kind quickstart, the
optimized-baseline guide, teardown, benchmarking an already-running stack, dry-run previews,
out-of-policy refusals) through the *real* agent loop + allowlist + approval gating, capturing
every command without executing anything:

```bash
make validate        # deterministic & hermetic — no API key, Docker, kind, or repos needed
make flows           # list the known flows
make validate-live   # the real LLM drives each flow from natural language (needs a key)
pytest tests/        # the full suite (also hermetic)
```

### Layout

| Path | What |
|---|---|
| `app/` | FastAPI backend: agent loop, tools, orchestrator, security, validation (mechanism only) |
| `security/allowlist.yaml` | The deny-by-default command policy (data) |
| `knowledge/` | The agent's editable brain — playbooks & heuristics (no code) |
| `ui/` | Static chat UI (HTML/JS/CSS) |
| `deploy/` | Dockerfile assets, Helm chart, Kustomize base/overlay, observability |
| `workspace/` | Gitignored runtime scratch (sessions, configs, logs) |
| `tests/` | pytest (unit + integration + flow validation) |

### Documentation

| Doc | For |
|---|---|
| [docs/USER_GUIDE.md](docs/USER_GUIDE.md) | Using the agent end-to-end with no `llm-d-benchmark` expertise |
| [docs/GPU_CLUSTER_RUNBOOK.md](docs/GPU_CLUSTER_RUNBOOK.md) | Going beyond CPU-sim to a real single-GPU cluster |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Layers, components, the four determinism gates, trust boundaries |
| [docs/API.md](docs/API.md) | The HTTP/WebSocket API + the 32-tool agent surface + the `SessionPlan` |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Running locally and in-cluster (Helm/Kustomize), config, secrets, RBAC |
| [docs/VALIDATION.md](docs/VALIDATION.md) | The flow-validation harness — does the agent run the *right* commands? |
| [FEATURES.md](FEATURES.md) | The authoritative, evidence-backed inventory of every feature + how to verify each |

See [`docs/README.md`](docs/README.md) for the full index, and [`CLAUDE.md`](CLAUDE.md) for the
project's working rules.

### Relationship to the repos

This project sits alongside two **read-only** repos and never modifies them:

```
kind-quickstart-guide/
├── llm-d/                            # deployment guides (read-only context)
├── llm-d-benchmark/                  # provides the llmdbenchmark CLI (read-only)
└── llm-d-benchmarking-agent-project/ # this project
```
