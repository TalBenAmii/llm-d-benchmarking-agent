# Packaging & deployment (the production image + one-command K8s deploy)

This is the *judgment* layer for how the agent is packaged and deployed. The mechanism — the
`Dockerfile` and the Helm chart (`deploy/helm/llm-d-benchmarking-agent/`) — is data; this
file is how to reason about using it.

## Two distinct ways to run the agent

1. **Local (the default for the quickstart / a laptop demo).** `./scripts/run.sh` or
   `uvicorn app.main:app`. The agent drives the *local* `llmdbenchmark` CLI
   (`execute_llmdbenchmark`) and shells out to your local `kubectl`/`kind`. No container, no
   in-cluster RBAC. This remains the simplest path and the one the kind/sim quickstart uses.

2. **In-cluster (the packaging deploy).** The agent service runs as a Deployment in the
   cluster, reachable via its Service (port 8000). Use this when you want the agent itself to
   live next to the workloads it benchmarks, or to expose it to a team. Deploy it with **one
   command** via Helm (below).

## Deploying

Helm (templated, values-driven):

```
helm install bench-agent deploy/helm/llm-d-benchmarking-agent \
  --namespace llmd-bench --create-namespace \
  --set secret.anthropicApiKey=$ANTHROPIC_API_KEY
```

The in-cluster **default provider is `claude-agent-sdk`** (Claude Max/Pro subscription) authed by
`secret.claudeCodeOauthToken` (a `claude setup-token` token); the `secret.anthropicApiKey` above is the
API-key **fallback** (`config.llmProvider=anthropic`). Full runbook: `docs/guides/CLUSTER_SERVICE_DEPLOY.md`.

Then `kubectl -n <ns> port-forward svc/bench-agent-llm-d-benchmarking-agent 8000:8000` and open
the UI (the Service name is `<release>-<chart>` via the fullname template — adjust if you chose a
different release name or set `fullnameOverride`).

## Image pinning (prefer digests)

The image tag (`image.tag`, default = the chart's `appVersion`) is convenient but **mutable**.
For reproducible rollouts pin by **digest**: set `image.digest: sha256:...` in Helm values
(it wins over the tag). Tell
the user this when they ask about production hardening; for a demo a tag is fine.

## RBAC: least privilege, and why an orchestrated run needs it

`orchestrate_benchmark_run` submits a benchmark as a **Kubernetes Job** and then watches it,
reads pods, and streams logs — all by shelling out to `kubectl` (see
`app/orchestrator/kube.py`). When the agent runs *in-cluster*, those `kubectl` calls
authenticate as the pod's ServiceAccount, so that SA must be allowed to do exactly those
things. The chart creates a **namespaced Role** granting only:

- `batch`/`jobs`: create, get, list, watch, patch, delete
- `pods`: get, list, watch
- `pods/log`: get
- `configmaps`: get, list, watch, create, patch — the agent's OWN per-sweep checkpoint
  ConfigMaps (DOE sweep resume); no delete

No ClusterRole, no secrets/exec/portforward — the only writes are the benchmark Jobs and its
own sweep-checkpoint ConfigMaps in its own namespace. This is what lets an orchestrated Job actually run live
(the capability Phase 3 deferred to packaging) without handing the agent broad cluster power.

Set `ORCHESTRATOR_SERVICE_ACCOUNT` in the backend env (the chart does not set it today) so the
benchmark Jobs the agent submits also run under that least-privilege SA rather than the
namespace default. If it isn't
set (local dev), the Job uses the namespace default SA — fine for kind/sim.

## Enabling in-cluster orchestrated runs

`orchestrate_benchmark_run` refuses unless an `ORCHESTRATOR_IMAGE` is configured (or `image` is
passed): an orchestrated run is a real Job and needs an image carrying the `llmdbenchmark` CLI.
Set `config.orchestratorImage` (Helm) to that image.
Until it's set, the agent correctly falls back to the local CLI path (`execute_llmdbenchmark`).

## Security posture baked into the deploy

- **Non-root, hardened pod:** `runAsNonRoot` (uid 10001), `readOnlyRootFilesystem`, all Linux
  capabilities dropped, `RuntimeDefault` seccomp, no privilege escalation. Writable scratch
  (`/workspace`, `/tmp`) is an `emptyDir`, so session state never persists on the image layer.
- **Secrets stay server-side:** LLM/HF keys come from a Kubernetes Secret (chart-managed or a
  pre-existing one you point at via `secret.existingSecret`), surfaced as env to the backend
  only. They are never baked into the image (`.dockerignore` excludes `.env`) and never reach
  the browser.
- **Health & metrics:** liveness probe `/healthz`, readiness probe `/readyz`; Prometheus
  scrapes `/metrics` (pods are annotated `prometheus.io/scrape`). All three match `app/main.py`.
