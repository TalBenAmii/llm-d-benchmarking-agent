# cluster-service-sim — local cluster-service smoke test

A **local test adapter** that deploys the agent as a Kubernetes **service** onto a throwaway
[`kind`](https://kind.sigs.k8s.io/) cluster — exercising the real service installer
(`scripts/install_service.sh`) and the project Helm chart — and asserts that **the application
fully works** end to end.

> **This is test scaffolding — it never ships in the product image.** `testing/` is excluded by
> `.dockerignore` and `tests/test_product_boundary.py` turns "the harness never enters the build
> context" into a checked invariant. This adapter imports nothing from `app/`.

## What it does

1. **Preflight** — checks `docker`, `kind`, `kubectl`, `helm`, `curl`, `timeout` are present.
2. **Image** — reuses `llm-d-benchmarking-agent:0.1.0` if it exists locally, else builds it
   (`make image`, falling back to `docker build`) under a hard timeout. `--no-build` fails fast
   instead of building.
3. **kind cluster** — creates a throwaway cluster (`--name csvc-sim`, `--wait 120s`). An existing
   same-named cluster is deleted first for a clean slate (unless `--keep`, which reuses it).
4. **Load image** — `kind load docker-image` so the node never reaches a registry.
5. **Deploy** — with an Anthropic key it invokes **`scripts/install_service.sh`** directly (the
   real installer, provider `anthropic`, `--image-pull-policy Never`, `--context kind-csvc-sim`);
   without a key it does an equivalent `helm upgrade --install` that pins
   `config.llmProvider=claude-agent-sdk` (the installer has no provider flag, and `claude-agent-sdk`
   is the provider that passes readiness **keyless**).
6. **Rollout** — `kubectl rollout status` (bounded).
7. **Port-forward** the service and poll `/healthz` with a **bounded** retry (never an unbounded loop).
8. **Assertions** (below), each printed `PASS`/`FAIL`, with a final summary.
9. **Teardown** — a trap kills the port-forward and deletes the cluster on exit (unless `--keep`).

## Prerequisites

- `docker` (daemon running), `kind`, `kubectl`, `helm`, `curl`, and GNU `timeout` (coreutils) on `PATH`.
- `python3` (stdlib only) for the live-chat WebSocket round-trip. If it is absent, the chat check is
  **approximated** (asserts `/api/provider` shows anthropic built + ready) and clearly logged as such.
- Optional: an **Anthropic API key** (`--anthropic-key`, or `$ANTHROPIC_API_KEY`, or a project
  `.env` the script will source). With a key the live-chat check runs; without one it is skipped.
- Enough disk/RAM for the ~1GB full-bake image and a single-node kind cluster.

## How to run

```bash
# From the project root (or anywhere — the script resolves its own paths):
testing/cluster-service-sim/run.sh                 # build/reuse image, deploy, assert, tear down
testing/cluster-service-sim/run.sh --keep          # leave the cluster up for inspection afterwards
testing/cluster-service-sim/run.sh --no-build      # require the image to already exist locally

# With a key -> deploys anthropic AND runs the live-chat round-trip:
ANTHROPIC_API_KEY=sk-ant-... testing/cluster-service-sim/run.sh
#   or: testing/cluster-service-sim/run.sh --anthropic-key sk-ant-...
```

Useful flags (`--help` lists all): `--cluster`, `--namespace`, `--release`, `--port`, `--image`,
`--tag`, `--build-timeout SECS`, `--phase-timeout SECS`. Exit status is **0 only if every required
check passed**.

## What each assertion proves

| Check | What a PASS proves |
|-------|--------------------|
| `/healthz` 200 + `{"ok": true}` | The process is up and serving (liveness). |
| `/readyz` 200 | The startup self-check is green in-Pod: workspace writable, provider coherent, the read-only sibling repos are present on disk (baked into the image), runner/auth OK. On failure the JSON body (which names the failed probe) is printed. |
| `/api/provider` 200 | The provider surface answers; the log shows which provider built (`anthropic` with a key, else `claude-agent-sdk`). |
| **RBAC boundary** | Running `kubectl delete ns kube-system` **from inside the Pod** is refused with `Forbidden`. This proves the namespaced least-privilege Role holds — the agent's ServiceAccount cannot touch cluster-scoped resources or other namespaces. A *success* here would be a security failure; the refusal is the required pass. |
| **Live chat** (key only) | One real `user_message` over the `/ws` WebSocket comes back as a non-error `assistant_text` — the full LLM path works in-cluster. Skipped without a key (there is no keyless live LLM to talk to); the `claude-agent-sdk` deploy in that case exists only to prove a keyless-green `/readyz`. |

## Running it on the throwaway `kind-fresh` distro (WSL)

To exercise this on a clean box, use the repo's fresh-WSL-distro harness. From your normal distro:

```bash
bash fresh-env/run-app.sh --with-runtime      # builds the throwaway 'kind-fresh' distro WITH
                                              # docker + kind + kubectl pre-installed
```

`--with-runtime` provisions docker + kind + kubectl (`helm` is installed by the agent's own
`install_local.sh` during setup). Then run **this** adapter *inside* that distro — it deploys its own
cluster-service copy, independent of the local `uvicorn` app `run-app.sh` also starts:

```bash
wsl.exe -d kind-fresh -u root -- bash -lc \
  'cd /root/llm-d-benchmarking-agent-project && testing/cluster-service-sim/run.sh'
```

(If `helm` is not on `PATH` in the distro, install it first, then re-run.)

## Self-terminating by design

The maintainer's hard requirement is that this **never wedges**:

- Every wait is bounded — `kubectl wait`/`rollout --timeout`, `helm --wait --timeout`, `kind create
  --wait`, `timeout`-wrapped builds/loads/execs, and a **bounded** curl-retry for the port-forward
  (a fixed retry count, never `while true`).
- A **post-build watchdog** (`--phase-timeout`, default 1800s) SIGTERMs the run if the whole cluster
  phase overruns, so the exit trap still tears everything down.
- The live-chat WebSocket probe (`ws_chat_probe.py`) enforces its own wall-clock deadline and is
  additionally wrapped in an outer `timeout`.
- An `EXIT`/`INT`/`TERM` trap always kills the port-forward and (unless `--keep`) `helm uninstall`s +
  `kind delete cluster`s — each teardown command itself `timeout`-wrapped so cleanup can't hang.

## Troubleshooting

- **Deploy/rollout/health failures** print a best-effort cluster diagnostics dump (pods, recent
  events, describe, logs) before teardown. Re-run with `--keep` to poke at the live cluster.
- **kind + WSL networking:** if a Docker/WSL restart wiped the kind bridge's iptables `FORWARD`
  rules you may see cluster networking oddities — this adapter avoids in-cluster image pulls (it
  `kind load`s the image with `pullPolicy=Never`), but see the project's Docker/WSL setup notes if
  `kind create` itself struggles.

## Files

- `run.sh` — the adapter (all steps + assertions + teardown).
- `ws_chat_probe.py` — dependency-free (stdlib-only) WebSocket chat round-trip probe used by step 8.
