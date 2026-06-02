# llm-d Benchmarking Assistant Agent

A **local, chat-based assistant** that helps people who don't know the `llm-d-benchmark`
API run benchmarks anyway. You describe a use case in plain language
(*"benchmark a chat app with ~500 concurrent users"*); the agent interviews you, checks
your environment, deploys an `llm-d` stack if one isn't already running, runs the
benchmark, and explains the results.

It does this by driving the real `llmdbenchmark` CLI on your behalf — inside a strict
security sandbox, asking for your approval before anything that changes your system.

> **Status: implemented & verified.** The end-to-end vertical works — chat UI → agent loop
> → schema-validated, approval-gated tools → real `llmdbenchmark` execution → validated
> Benchmark Report summary — and has grown well past the MVP. Beyond the quickstart it now
> includes a **Kubernetes-native benchmark orchestrator** (Job lifecycle, fault
> classification, retry/dead-letter, parallel sweeps), a **results analyzer** (goodput, SLO
> filtering, Pareto/DoE), **multi-harness comparison**, a **capacity pre-flight**,
> **cross-session result history + trends**, **Prometheus/Grafana observability**, and a
> **hardened image + one-command Helm/Kustomize deploy** with least-privilege RBAC. The
> agent exposes **18 tools**. The headline supported path remains the `llm-d-benchmark`
> *quickstart* (local [kind](https://kind.sigs.k8s.io/) cluster, CPU-only simulated engine),
> which the agent can bootstrap end-to-end — install the prerequisites `install.sh` doesn't
> (Docker + the kind binary, via a vetted installer), create/delete the kind cluster, then
> deploy and benchmark — each step approval-gated. A live LLM session needs an API key in
> `.env`. The full pytest suite is hermetic (no API key, Docker, kind, or live cluster
> needed). See [`docs/`](docs/) for the full documentation suite and
> [`plan.md`](plan.md#implementation-status) for the status record.

## Design in one line
**Thin code, thick agent.** The Python here is only *mechanism* — a chat UI, an agent
loop, a handful of tools, a command allowlist, and schema validation. All the *judgment*
(which spec/harness/workload, what flags, how to read results) lives in the LLM plus
editable knowledge files under [`knowledge/`](knowledge/). Reliability comes from
**schema-validated handoffs** at every boundary, not from hard-coded scripts.

## How it stays safe
- **Deny-by-default allowlist** ([`security/allowlist.yaml`](security/allowlist.yaml)): the
  agent can only run a small, explicit set of commands — `llmdbenchmark`; read-only
  `kubectl`/`kind`/`docker` probes; `git clone` of the llm-d repos; `install.sh`;
  `kind create`/`delete cluster`; and the vetted [`scripts/install_prereqs.sh`](scripts/install_prereqs.sh),
  which is the *only* way it can install Docker + the kind binary (the allowlist grants no
  raw `apt`/`curl`/`sudo`; the script is the single reviewed artifact, pinned by name and flags).
- **No shell, ever.** Commands run as argv lists with `shell=False` — command injection is
  structurally impossible.
- **Per-action approval.** Read-only probes run automatically; every *mutating* command
  (installing Docker/kind, creating/deleting the cluster, standup, run, teardown) shows you
  the exact command and waits for you to click Approve.
- **Full command transparency.** The UI shows *every* command the agent runs — including the
  read-only probes that run automatically — and a one-click **Debug view** lists just the
  executed-command trail (with read-only/mutating badges). Nothing runs off-screen.
- **Secrets stay server-side.** Your LLM API key never reaches the browser.

## The four determinism gates
1. The LLM can only act through **schema-validated tool calls**.
2. Before any deployment it proposes a **SessionPlan** you approve.
3. Any generated config is validated via the CLI's own `--dry-run` / `plan`.
4. Results are parsed from the repo's validated **Benchmark Report v0.2**, never from logs.

## Run it
The quickest way — `run.sh` sets up the venv, installs the app, ensures a `.env`,
and starts the server (reads `HOST`/`PORT` from `.env`; defaults to 127.0.0.1:8000):
```bash
./run.sh            # then open http://127.0.0.1:8000
./run.sh --open     # ...and open it in a browser automatically
# add your ANTHROPIC_API_KEY (or OpenAI-compatible creds) to .env to enable live sessions
```

Or do it manually:
```bash
cp .env.example .env          # add your ANTHROPIC_API_KEY (or OpenAI-compatible creds)
pip install -e .              # or: uv pip install -e .
uvicorn app.main:app --reload
# open http://127.0.0.1:8000
```

Set `SIMULATE=1` in `.env` for a dry run: the agent drives the whole workflow but executes
nothing (every command is a no-op returning synthetic success, per-command approvals are
skipped, and a synthetic report is produced) — watch a guide end-to-end without a cluster.

Run the tests:
```bash
pip install -e '.[dev]'
pytest tests/
```

## Deploy to Kubernetes (production image + one command)

A hardened container image and a one-command cluster deploy ship in
[`Dockerfile`](Dockerfile) + [`deploy/`](deploy/). Build the image, then deploy with **either**
Helm or Kustomize:

```bash
docker build -t llm-d-benchmarking-agent:0.1.0 .

# Helm:
helm install bench-agent deploy/helm/llm-d-benchmarking-agent \
  --namespace llmd-bench --create-namespace \
  --set secret.anthropicApiKey=$ANTHROPIC_API_KEY

# ...or Kustomize (Helm-free):
kubectl apply -k deploy/kustomize/base
```

The deploy runs the agent **non-root** with a read-only root filesystem, sources LLM/HF keys
from a Kubernetes Secret (never baked into the image), probes `/healthz`, exposes `/metrics`
for Prometheus, and grants a **namespaced least-privilege Role** so the agent can orchestrate
benchmark Jobs (the verbs in [`app/orchestrator/kube.py`](app/orchestrator/kube.py) and nothing
more). Prefer pinning the image by digest in production. See
[`knowledge/packaging.md`](knowledge/packaging.md) for the full deployment guide.

## Validate the agent runs the *right commands*
A **flow-validation harness** proves the agent drives the correct command sequence for
each end-to-end flow (the kind quickstart, the optimized-baseline guide, teardown,
benchmarking an already-running stack, dry-run previews, and out-of-policy refusals):

```bash
make validate        # deterministic & hermetic — no API key, Docker, kind, or repos needed
make flows           # list the known flows
make validate-live   # the real LLM drives each flow from natural-language input (needs a key)
```

It replays each flow through the **real** agent loop + allowlist + approval gating,
capturing every command without executing anything, and asserts the right commands run
with correct read-only/mutating gating. This is what
[`.github/workflows/agent-flow-validation.yml`](../.github/workflows/agent-flow-validation.yml)
runs on every push/PR. See [`docs/VALIDATION.md`](docs/VALIDATION.md) for the full design
and how to add a flow.

## Layout
| Path | What |
|---|---|
| `app/` | FastAPI backend: agent loop, tools, security, validation (mechanism only) |
| `security/allowlist.yaml` | The deny-by-default command policy (data) |
| `knowledge/` | The agent's editable brain — playbooks & heuristics (no code) |
| `ui/` | Static chat UI (HTML/JS/CSS) |
| `workspace/` | Gitignored runtime scratch (sessions, configs, logs) |
| `tests/` | pytest |

## Documentation

The full technical documentation suite lives under [`docs/`](docs/):

| Doc | For |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design: layers, components, the four determinism gates, trust boundaries |
| [docs/API.md](docs/API.md) | The HTTP/WebSocket API + the 18-tool agent surface + the `SessionPlan` |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Running locally and in-cluster (Helm/Kustomize), config, secrets, RBAC, observability |
| [docs/USER_GUIDE.md](docs/USER_GUIDE.md) | Using the agent end-to-end with no `llm-d-benchmark` expertise |
| [docs/VALIDATION.md](docs/VALIDATION.md) | The flow-validation harness — does the agent run the *right* commands? |

See also [`CLAUDE.md`](CLAUDE.md) for the full set of working rules and
[`plan.md`](plan.md) for the implementation plan.

## Relationship to the repos
This project sits alongside two **read-only** repos and never modifies them:
```
kind-quickstart-guide/
├── llm-d/                            # deployment guides (read-only context)
├── llm-d-benchmark/                 # provides the llmdbenchmark CLI (read-only)
└── llm-d-benchmarking-agent-project/ # this project
```
