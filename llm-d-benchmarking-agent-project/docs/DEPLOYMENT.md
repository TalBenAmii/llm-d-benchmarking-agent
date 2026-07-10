# Deployment Guide

How to run the agent in each of its two modes — **local** (a laptop / dev box, the
direct / dev path) and **in-cluster** (a hardened Deployment via Helm; on a local
`kind` cluster this is the recommended POC path) — plus configuration, secrets, RBAC,
and observability wiring.

The mechanism (the `Dockerfile`, the Helm chart under
`deploy/helm/llm-d-benchmarking-agent/`)
is *data*; the operational *judgment* lives in [`knowledge/packaging.md`](../knowledge/packaging.md).
This guide ties them together.

---

## Mode 1 — Local (dev / non-service, on your laptop)

The agent drives the *local* `llmdbenchmark` CLI and shells out to your local
`kubectl`/`kind`/`docker`. No container, no in-cluster RBAC. This is the simplest path and
the one the kind/sim quickstart uses.

### Prerequisites
- `uv` (the Python package/venv manager) — `scripts/install_local.sh` / `scripts/run.sh` auto-bootstrap it
  if it's missing, and it fetches a matching Python 3.11 itself. Plus whatever the agent installs for
  you (Docker + the kind binary via the vetted `scripts/install_prereqs.sh`, and the benchmark repo's
  toolchain via `install.sh`).
- An LLM API key for *live* sessions (Anthropic, or any OpenAI-compatible endpoint). Without
  a key the server still boots and the deterministic test/validation paths run.

### Run it
The quickest way — `scripts/run.sh` syncs the venv from `uv.lock` (via `uv sync`), ensures a `.env`,
and starts the server (reads `HOST`/`PORT` from `.env`; defaults to `127.0.0.1:8000`):

```bash
./scripts/run.sh            # then open http://127.0.0.1:8000
./scripts/run.sh --open     # ...and open it in a browser automatically
```

Or manually (uv is required — it builds `.venv` from the committed `uv.lock`, the source of truth):

```bash
cp .env.example .env          # add ANTHROPIC_API_KEY (or OpenAI-compatible creds)
uv sync                       # runtime deps from uv.lock  (uv sync --extra dev  for the test/lint toolchain)
uv run uvicorn app.main:app --reload
# open http://127.0.0.1:8000
```

### Configuration (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER` | `claude-agent-sdk` | `claude-agent-sdk` (default — your Claude Pro/Max plan via the local `claude` CLI login or a `claude setup-token` token, no API key; `scripts/setup-claude-plan.sh` wires it interactively) or `anthropic`. |
| `AGENT_SDK_MODEL` / `AGENT_SDK_EFFORT` | `claude-sonnet-5` / `high` | Model + reasoning effort for the `claude-agent-sdk` route. |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` | — / `claude-opus-4-8` | Anthropic creds + model. |
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

## Mode 2 — In-cluster (Helm, one command)

Run the agent as a Deployment inside the cluster, reachable via its Service (port 8000). Use
this to live the agent next to the workloads it benchmarks, or to expose it to a team. **This is
the supported POC path**, exercised on a local `kind` cluster.

### Laptop (kind) — `./install.sh`

One command auto-installs any missing prereqs (docker/kind/kubectl/helm) via `sudo`, builds the
image, creates a `kind` cluster, `kind load`s the image, deploys the chart via
`scripts/install_service.sh`, verifies `/healthz`+`/readyz`, leaves the service running, and opens
the UI in your browser. Run it either **from scratch** (the curl one-liner self-clones) or **from a
checkout / repo root** (`./install.sh`):

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/TalBenAmii/llm-d-benchmarking-agent/main/install.sh)  # self-clones + runs
./install.sh                     # …or run from a clone of the repo
./install.sh --no-open           # deploy but don't open a browser (prints the port-forward line)
```

If Docker was newly installed, `install.sh` stops once for you to log out/in (docker-group
activation), then re-run. On first run it also offers to wire your **Claude subscription**
interactively (no manual `.env` step). Chat auth otherwise defaults to that subscription: `install.sh` reads
`CLAUDE_CODE_OAUTH_TOKEN` (a `claude setup-token` token) from the project `.env` or `--oauth-token`,
falling back to `ANTHROPIC_API_KEY` / `--anthropic-key`. Keyless still serves the health endpoints
(chat disabled). Teardown: `kind delete cluster --name bench-agent`.

### Real cluster (not yet tested) — same deploy, minimal delta

`scripts/install_service.sh` deploys to whatever cluster your `kubectl` context points at, so a
real cluster is the *same* steps with two changes: (a) push the image to a registry the cluster
can pull (`make image-publish`, or your own registry) instead of `kind load`; (b) point at your
context and skip `kind create`:

```bash
cd llm-d-benchmarking-agent-project
./scripts/install_service.sh \
  --image <registry-repo> --context <your-context> \
  --oauth-token "$CLAUDE_CODE_OAUTH_TOKEN"        # selects the default claude-agent-sdk provider
```

`--oauth-token` (or `$CLAUDE_CODE_OAUTH_TOKEN`) is the default auth; `--anthropic-key` is the
API-key fallback. This path should work but is **untested** today. Full operator runbook,
including auth and RBAC: **`docs/CLUSTER_SERVICE_DEPLOY.md`**.

### Manual / API-key fallback (build + raw Helm)

`install_service.sh` wraps these; run them by hand only for an air-gapped or API-key-only deploy:

```bash
cd llm-d-benchmarking-agent-project
docker build -t llm-d-benchmarking-agent:0.1.0 .
# make the image visible to your cluster, e.g.:  kind load docker-image llm-d-benchmarking-agent:0.1.0
helm install bench-agent deploy/helm/llm-d-benchmarking-agent \
  --namespace llmd-bench --create-namespace \
  --set image.repository=llm-d-benchmarking-agent \
  --set config.llmProvider=anthropic \
  --set secret.anthropicApiKey=$ANTHROPIC_API_KEY
```

The image is **hardened**: non-root (uid 10001), read-only root filesystem, all Linux
capabilities dropped, `RuntimeDefault` seccomp, no baked-in secrets (`.dockerignore` excludes
`.env`), pinned kubectl. Prefer pinning by **digest** in production (below). The chart's default
provider is `claude-agent-sdk` (`secret.claudeCodeOauthToken`, a `claude setup-token` token; the
`claude` CLI is baked into the image); the `secret.anthropicApiKey` example above is the API-key
fallback — pair it with `config.llmProvider=anthropic`.

Key chart values (`deploy/helm/llm-d-benchmarking-agent/values.yaml`):

| Value | Default | Purpose |
|---|---|---|
| `image.repository` / `image.tag` / `image.digest` | `ghcr.io/llm-d/llm-d-benchmarking-agent` / `0.1.0` / `""` | Image; **digest wins over tag** when set. |
| `replicaCount` | `1` | The agent keeps in-memory per-session state; `>1` needs sticky ingress (out of scope). |
| `config.llmProvider` / `config.anthropicModel` | provider config | Non-secret env. |
| `config.maxConcurrentRuns` | `2` | Concurrency cap. |
| `config.orchestratorImage` | `""` | Enables orchestrated K8s-Job runs when set. |
| `secret.claudeCodeOauthToken` | `""` | **Primary chat auth** — the `claude setup-token` subscription token for the default `claude-agent-sdk` provider. |
| `secret.create` / `secret.existingSecret` / `secret.anthropicApiKey` / `secret.hfToken` | `true` / `""` / … | Chart-managed Secret, or point at a pre-existing one (recommended for real deploys). |
| `serviceAccount.create` / `serviceAccount.name` / `rbac.create` | `true` / `""` / `true` | The least-privilege SA + namespaced Role/RoleBinding. |
| `service.type` / `service.port` | `ClusterIP` / `8000` | Networking. |
| `resources`, `podSecurityContext`, `securityContext` | hardened defaults | Requests/limits + the non-root/read-only-rootfs posture. |
| `metrics.podAnnotations` | `true` | Annotate the pod for Prometheus scraping of `/metrics`. |

### Reach the UI

```bash
kubectl -n llmd-bench port-forward svc/bench-agent-llm-d-benchmarking-agent 8000:8000
# open http://127.0.0.1:8000
```

### Image pinning (production)

The tag is convenient but mutable. For reproducible rollouts pin by **digest**: set
`image.digest: sha256:...` in Helm values (it wins over the tag).

---

## RBAC: least privilege

`orchestrate_benchmark_run` submits a benchmark as a Kubernetes Job and then watches it,
reads pods, and streams logs — all via `kubectl` (`app/orchestrator/kube.py`). In-cluster
those calls authenticate as the pod's ServiceAccount, so the chart creates a
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
CLI. Set `config.orchestratorImage` (Helm). Until
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
  the `observe_run_metrics` tool via `kubectl top`, which needs the in-cluster metrics-server.
  It is NOT installed by kind or the `cicd/kind` spec — add it to the cluster separately (on
  kind, with `--kubelet-insecure-tls`). The agent ships a vetted, approval-gated installer for
  this: `run_shell("install_metrics_server.sh --kubelet-insecure-tls")` (it is per-cluster
  — install once and every run on that cluster gets live stats).

## Security posture baked into the deploy

- Non-root (uid 10001), `readOnlyRootFilesystem`, all caps dropped, `RuntimeDefault`
  seccomp, no privilege escalation; writable scratch (`/workspace`, `/tmp`) is an `emptyDir`,
  so session state never persists on the image layer.
- LLM/HF keys come from a Kubernetes Secret (chart-managed or pre-existing), surfaced as env
  to the backend only — never baked into the image, never sent to the browser.

See [`knowledge/packaging.md`](../knowledge/packaging.md) for the operational reasoning and
[`ARCHITECTURE.md`](ARCHITECTURE.md) for how packaging fits the whole system.
