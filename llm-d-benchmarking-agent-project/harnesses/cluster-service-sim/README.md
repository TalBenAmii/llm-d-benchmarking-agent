# cluster-service-sim: local cluster-service smoke test

A local test adapter that deploys the agent as a Kubernetes service onto a throwaway
[`kind`](https://kind.sigs.k8s.io/) cluster via the real service installer
(`scripts/install/install_service.sh`) + the project Helm chart, and asserts the application
fully works end to end.

> Test scaffolding only — never ships in the product image; this adapter imports nothing from
> `app/`. Full boundary statement + the test that enforces it:
> [`../local-cluster/README.md`](../local-cluster/README.md#product-safety-how-we-keep-this-out-of-the-shipped-artifact).

## What it does

`run.sh` is the truth; in short it:

- Preflights the prerequisite tools (below); reuses a local `llm-d-benchmarking-agent:0.1.0`
  image or builds it under a hard timeout (`--no-build` fails fast instead).
- Creates a throwaway kind cluster `csvc-sim` (an existing one is deleted first for a clean
  slate, unless `--keep` reuses it) and `kind load`s the image so the node never reaches a registry.
- Deploys via the real `scripts/install/install_service.sh` (`--image-pull-policy Never`,
  `--context kind-csvc-sim`); the installer owns provider selection from the auth flag it is
  handed (OAuth token → `claude-agent-sdk`, API key → `anthropic`, none → keyless
  `claude-agent-sdk` with chat disabled so `/readyz` still goes green) — no `helm` bypass.
- Waits for rollout, port-forwards, polls `/healthz` (bounded retry), runs the assertions below
  (each printed `PASS`/`FAIL` with a final summary), and tears down on exit (unless `--keep`).

## Prerequisites

- `docker` (daemon running), `kind`, `kubectl`, `helm`, `curl`, GNU `timeout` (coreutils) on
  `PATH`; disk/RAM for the ~1GB full-bake image and a single-node kind cluster.
- `python3` (stdlib only) for the live-chat WebSocket round-trip; if absent, the chat check is
  approximated (asserts `/api/provider` shows the authed provider built + ready) and clearly logged.
- Optional auth — any one enables the live-chat check (skipped if none). PRIMARY: a Claude
  subscription OAuth token from `claude setup-token` (`--oauth-token` / `$CLAUDE_CODE_OAUTH_TOKEN` /
  a project `.env` the script reads) → deploys `claude-agent-sdk`. FALLBACK: an Anthropic API key
  (`--anthropic-key` / `$ANTHROPIC_API_KEY` / `.env`) → deploys `anthropic`.

## How to run

```bash
# From the project root (or anywhere — the script resolves its own paths):
harnesses/cluster-service-sim/run.sh                 # build/reuse image, deploy, assert, tear down
harnesses/cluster-service-sim/run.sh --keep          # leave the cluster up for inspection
harnesses/cluster-service-sim/run.sh --no-build      # require the image to already exist locally

# With auth the live-chat round-trip runs too:
CLAUDE_CODE_OAUTH_TOKEN=... harnesses/cluster-service-sim/run.sh    # or --oauth-token ...
ANTHROPIC_API_KEY=sk-ant-... harnesses/cluster-service-sim/run.sh   # or --anthropic-key ...
```

`--help` lists all flags (`--cluster`, `--namespace`, `--release`, `--port`, `--image`, `--tag`,
`--build-timeout SECS`, `--phase-timeout SECS`, ...). Exit status is 0 only if every required
check passed.

## What each assertion proves

| Check | What a PASS proves |
|-------|--------------------|
| `/healthz` 200 + `{"ok": true}` | The process is up and serving (liveness). |
| `/readyz` 200 | The startup self-check is green in-Pod: workspace writable, provider coherent, the read-only sibling repos are present on disk (baked into the image), runner/auth OK. On failure the JSON body (which names the failed probe) is printed. |
| `/api/provider` 200 | The provider surface answers; the log shows which provider built (`claude-agent-sdk` with an OAuth token or keyless, `anthropic` with an API key). |
| RBAC boundary | `kubectl delete ns kube-system` from inside the Pod is refused with `Forbidden`: the namespaced least-privilege Role holds. The refusal IS the pass; a success would be a security failure. |
| Live chat (auth only) | One real `user_message` over the `/ws` WebSocket returns a non-error `assistant_text`: the full LLM path works in-cluster. Skipped without auth (the keyless deploy exists only to prove a keyless-green `/readyz`). |

## Running it on the throwaway `kind-fresh` distro (WSL)

To exercise this on a clean box, use the repo's fresh-WSL-distro harness from your normal distro:

```bash
bash fresh-env/run-app.sh --with-runtime   # throwaway 'kind-fresh' distro WITH docker+kind+kubectl
```

(`helm` comes from the agent's own `install_local.sh` during setup; if it's not on `PATH` in the
distro, install it first.) Then run the adapter inside the distro — it deploys its own
cluster-service copy, independent of the local `uvicorn` app `run-app.sh` also starts:

```bash
wsl.exe -d kind-fresh -u root -- bash -lc \
  'cd /root/llm-d-benchmarking-agent-project && harnesses/cluster-service-sim/run.sh'
```

## Self-terminating by design

The maintainer's hard requirement is that this never wedges:

- Every wait is bounded: `--timeout`/`--wait` flags everywhere, `timeout`-wrapped
  builds/loads/execs, a fixed-count curl retry (never `while true`), and the WebSocket probe
  (`ws_chat_probe.py`) enforces its own wall-clock deadline inside an outer `timeout`.
- A post-build watchdog (`--phase-timeout`, default 1800s) SIGTERMs the run if the whole
  cluster phase overruns, so the exit trap still fires.
- An `EXIT`/`INT`/`TERM` trap always kills the port-forward and (unless `--keep`)
  `helm uninstall`s + `kind delete cluster`s, each teardown command itself `timeout`-wrapped.

## Troubleshooting

- Deploy/rollout/health failures print a best-effort cluster diagnostics dump (pods, recent
  events, describe, logs) before teardown; re-run with `--keep` to poke at the live cluster.
- kind + WSL networking: a Docker/WSL restart can wipe the kind bridge's iptables `FORWARD`
  rules. This adapter avoids in-cluster image pulls (`kind load` + `pullPolicy=Never`), but see
  the project's Docker/WSL setup notes if `kind create` itself struggles.

## Files

- `run.sh`: the adapter (all steps + assertions + teardown).
- `ws_chat_probe.py`: stdlib-only WebSocket chat round-trip probe (the live-chat assertion).
