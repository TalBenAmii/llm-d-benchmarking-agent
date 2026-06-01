# Deployment Guide

How to run the agent in each of its two modes — **local** (a laptop / dev box, the
quickstart path) and **in-cluster** (a hardened Deployment via Helm or Kustomize) — plus
configuration, secrets, RBAC, and observability wiring.

The mechanism (the `Dockerfile`, the Helm chart under
`deploy/helm/llm-d-benchmarking-agent/`, the Kustomize base under `deploy/kustomize/base/`)
is *data*; the operational *judgment* lives in [`knowledge/packaging.md`](../knowledge/packaging.md).
This guide ties them together.

---

## Mode 1 — Local (the default; quickstart / laptop demo)

The agent drives the *local* `llmdbenchmark` CLI and shells out to your local
`kubectl`/`kind`/`docker`. No container, no in-cluster RBAC. This is the simplest path and
the one the kind/sim quickstart uses.

### Prerequisites
- Python 3.11+ (the project), plus whatever the agent installs for you (Docker + the kind
  binary via the vetted `scripts/install_prereqs.sh`, and the benchmark repo's toolchain via
  `install.sh`).
- An LLM API key for *live* sessions (Anthropic, or any OpenAI-compatible endpoint). Without
  a key the server still boots and the deterministic test/validation paths run.

### Run it
The quickest way — `run.sh` sets up a venv, installs the app, ensures a `.env`, and starts
the server (reads `HOST`/`PORT` from `.env`; defaults to `127.0.0.1:8000`):

```bash
./run.sh            # then open http://127.0.0.1:8000
./run.sh --open     # ...and open it in a browser automatically
```

Or manually:

```bash
cp .env.example .env          # add ANTHROPIC_API_KEY (or OpenAI-compatible creds)
pip install -e .              # or: uv pip install -e .
uvicorn app.main:app --reload
# open http://127.0.0.1:8000
```

### Configuration (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER` | `anthropic` | `anthropic` or `openai`. |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` | — / `claude-opus-4-8` | Anthropic creds + model. |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` | — / `…/v1` / `gpt-4o` | OpenAI-compatible creds; `BASE_URL` may point at a self-hosted vLLM/llm-d endpoint. |
| `REPOS_DIR` | parent of the project | Where the `llm-d` / `llm-d-benchmark` repos are (or will be cloned). |
| `WORKSPACE_DIR` | `./workspace` | Runtime scratch (sessions, configs, logs, history). |
| `HF_TOKEN` | — | Only for gated models on real (non-sim) deploys; backend-only, never echoed. |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Server bind. |
| `MAX_CONCURRENT_RUNS` | `2` | Cross-session cap on concurrent *mutating* runs (`<=0` = unlimited). |
| `ORCHESTRATOR_IMAGE` | — | Image for orchestrated (K8s Job) runs; empty → `orchestrate_benchmark_run` refuses and the local CLI path is used. |
| `ORCHESTRATOR_SERVICE_ACCOUNT` | — | SA the orchestrated Jobs run under; empty → namespace default SA. |

> Secrets live only in the backend env and are never sent to the browser or to child
> processes (the runner scrubs them out).

---

## Mode 2 — In-cluster (Helm / Kustomize, one command)

Run the agent as a Deployment inside the cluster, reachable via its Service (port 8000). Use
this to live the agent next to the workloads it benchmarks, or to expose it to a team.

### Build the image

```bash
docker build -t llm-d-benchmarking-agent:0.1.0 .
```

The image is **hardened**: non-root (uid 10001), read-only root filesystem, all Linux
capabilities dropped, `RuntimeDefault` seccomp, no baked-in secrets (`.dockerignore`
excludes `.env`), pinned kubectl. Prefer pinning by **digest** in production (below).

### Deploy with Helm

```bash
helm install bench-agent deploy/helm/llm-d-benchmarking-agent \
  --namespace llmd-bench --create-namespace \
  --set secret.anthropicApiKey=$ANTHROPIC_API_KEY
```

Key chart values (`deploy/helm/llm-d-benchmarking-agent/values.yaml`):

| Value | Default | Purpose |
|---|---|---|
| `image.repository` / `image.tag` / `image.digest` | `ghcr.io/llm-d/llm-d-benchmarking-agent` / `0.1.0` / `""` | Image; **digest wins over tag** when set. |
| `replicaCount` | `1` | The agent keeps in-memory per-session state; `>1` needs sticky ingress (out of scope). |
| `config.llmProvider` / `config.anthropicModel` / `config.openaiBaseUrl` / `config.openaiModel` | provider config | Non-secret env. |
| `config.maxConcurrentRuns` | `2` | Concurrency cap. |
| `config.orchestratorImage` | `""` | Enables orchestrated K8s-Job runs when set. |
| `secret.create` / `secret.existingSecret` / `secret.anthropicApiKey` / `secret.openaiApiKey` / `secret.hfToken` | `true` / `""` / … | Chart-managed Secret, or point at a pre-existing one (recommended for real deploys). |
| `serviceAccount.create` / `serviceAccount.name` / `rbac.create` | `true` / `""` / `true` | The least-privilege SA + namespaced Role/RoleBinding. |
| `service.type` / `service.port` | `ClusterIP` / `8000` | Networking. |
| `resources`, `podSecurityContext`, `securityContext` | hardened defaults | Requests/limits + the non-root/read-only-rootfs posture. |
| `metrics.podAnnotations` | `true` | Annotate the pod for Prometheus scraping of `/metrics`. |

### Deploy with Kustomize (Helm-free)

```bash
kubectl apply -k deploy/kustomize/base                  # plain defaults
# or, with a namespace + API-key Secret + pinned image:
kubectl apply -k deploy/kustomize/overlays/example
```

The base renders the same Deployment + Service + ServiceAccount + Role/RoleBinding. Copy
`deploy/kustomize/overlays/example/secret.env.example` to `secret.env` and pin the image
with `kustomize edit set image .../agent@sha256:...`.

### Reach the UI

```bash
kubectl -n llmd-bench port-forward svc/llm-d-benchmarking-agent 8000:8000
# open http://127.0.0.1:8000
```

### Image pinning (production)

The tag is convenient but mutable. For reproducible rollouts pin by **digest**: set
`image.digest: sha256:...` in Helm values (it wins over the tag), or
`kustomize edit set image .../agent@sha256:...` for Kustomize.

---

## RBAC: least privilege

`orchestrate_benchmark_run` submits a benchmark as a Kubernetes Job and then watches it,
reads pods, and streams logs — all via `kubectl` (`app/orchestrator/kube.py`). In-cluster
those calls authenticate as the pod's ServiceAccount, so the chart/base create a
**namespaced Role** granting only:

- `batch`/`jobs`: `create, get, list, watch, patch, delete`
- `pods`: `get, list, watch`
- `pods/log`: `get`

and nothing else — no ClusterRole, no secrets/exec/portforward, no write to anything but the
benchmark Jobs in its own namespace. This resolves the RBAC capability Phase 3 deferred to
packaging, without handing the agent broad cluster power.

Set `ORCHESTRATOR_SERVICE_ACCOUNT` (the deploy does this) so the *submitted* Jobs also run
under that least-privilege SA. Unset (local dev) → the namespace default SA, fine for
kind/sim.

## Enabling in-cluster orchestrated runs

`orchestrate_benchmark_run` refuses unless `ORCHESTRATOR_IMAGE` is configured (or `image` is
passed) — an orchestrated run is a real Job and needs an image carrying the `llmdbenchmark`
CLI. Set `config.orchestratorImage` (Helm) / the `ORCHESTRATOR_IMAGE` env (Kustomize). Until
then the agent correctly falls back to the local CLI path (`execute_llmdbenchmark`).

## Health & observability

- **Probes:** the `livenessProbe` hits `/healthz` (minimal — process up?); the `readinessProbe`
  hits `/readyz` (per-component: provider configured, repos present, runner ok, workspace
  writable — `200` ready / `503` not).
- **Metrics:** Prometheus scrapes `/metrics` (the agent + orchestrator counters/timers in
  text exposition). The pod is annotated `prometheus.io/scrape` when `metrics.podAnnotations`
  is on. A ready-made scrape config and Grafana dashboard ship under
  `deploy/observability/` (`prometheus-scrape.yaml`, `grafana-dashboard.json`).
- **Live run metrics** (CPU/memory of the model server / harness *during* a run) come from
  the `observe_run_metrics` tool via `kubectl top`, which needs the in-cluster metrics-server
  (the `cicd/kind` spec enables it).

## Security posture baked into the deploy

- Non-root (uid 10001), `readOnlyRootFilesystem`, all caps dropped, `RuntimeDefault`
  seccomp, no privilege escalation; writable scratch (`/workspace`, `/tmp`) is an `emptyDir`,
  so session state never persists on the image layer.
- LLM/HF keys come from a Kubernetes Secret (chart-managed or pre-existing), surfaced as env
  to the backend only — never baked into the image, never sent to the browser.

See [`knowledge/packaging.md`](../knowledge/packaging.md) for the operational reasoning and
[`ARCHITECTURE.md`](ARCHITECTURE.md) for how packaging fits the whole system.
