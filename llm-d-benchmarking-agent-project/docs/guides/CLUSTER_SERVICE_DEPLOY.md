# Cluster-service deploy runbook

Everything a human has to do **by hand** to build, publish, install, and test the llm-d Benchmarking
Assistant as an **in-cluster Kubernetes service** (as opposed to running it on a laptop). Organized by
audience: **maintainer** (publish the image), **operator** (install the service), **tester** (verify
locally before shipping). Every command/flag/path below is copied from the actual scripts, Makefile,
Dockerfile, chart, and CI scaffold — not paraphrased.

## Maintainer one-time checklist

- [ ] Build the image — `make image` (~1 GB, multi-minute, needs network egress).
- [ ] `docker login ghcr.io` with a PAT that has `write:packages`.
- [ ] Publish — `make image-publish` (pushes `ghcr.io/llm-d/llm-d-benchmarking-agent:0.1.0`).
- [ ] Make the GHCR package **public** (so the chart's `IfNotPresent` pull needs no imagePullSecret) — or decide to keep it private and require operators to set an imagePullSecret.
- [ ] Keep `VERSION` (Makefile) ↔ chart `appVersion` (`deploy/helm/.../Chart.yaml`) in sync.
- [ ] **Move** the CI scaffold `.github/workflows/image-publish.yml` to the **repo-root** `.github/workflows/` and verify its `context:`/`file:` for the monorepo layout (TODO — see §2).
- [ ] **Sign off the three build decisions**: (a) skopeo source, (b) GHCR public vs private, (c) repin upstream SHAs to a tag (see §2).

---

## 1. Overview — two install paths

There are two entirely separate installers; pick by where the agent runs.

| | `scripts/install_local.sh` | `scripts/install_service.sh` |
|---|---|---|
| **Runs the agent** | on your laptop/dev box (a `.venv` + `./scripts/run.sh`) | as a Pod inside a Kubernetes cluster (Helm-deployed) |
| **What it sets up** | clones the 3 sibling repos, installs the client toolchain + `llmdbenchmark` CLI + this app's venv + `.env` + MCP server | `helm upgrade --install` of the **pre-built published image** into an existing cluster, with a namespace-scoped SA + least-privilege RBAC |
| **Prereqs** | a dev box (Debian/Ubuntu); optional `--prereqs` for Docker+kind | `kubectl` + `helm` + a reachable cluster |
| **Use when** | you're developing, or benchmarking from a workstation | you want the assistant to live in the cluster as a shared service |

`install_local.sh` is **unchanged** by this work; `install_service.sh` + the container image + the Helm
chart are the cluster-service path.

**The image is a self-contained "full-bake" (~1 GB).** Beyond the FastAPI app it carries the
`llmdbenchmark` CLI (in its own venv at `/repos/llm-d-benchmark/.venv`), all **three** sibling upstream
repos under `/repos` (`llm-d`, `llm-d-benchmark`, `llm-d-skills` — the app's readiness self-check and
skill-grounding gate require all three on disk), and the client toolchain the CLI shells out to
(`kubectl`, `helm` + the `helm-diff` plugin, `helmfile`, `kustomize`, `yq`, `jq`, `skopeo`, `crane`,
`git`). That is why the **verified local-CLI benchmark path works in-Pod with no host mounts**. The Pod
runs non-root (uid 10001) under a read-only root filesystem; only `/workspace` and `/tmp` are writable.

---

## 2. MAINTAINER — publishing the image

These are the manual steps you do **once per release**.

### Build

```bash
make image          # docker build -t ghcr.io/llm-d/llm-d-benchmarking-agent:0.1.0 --build-arg BENCH_REF=v0.7.0 .
```

Know before you run it: the image is **~1 GB**, the build **needs network egress** (it git-clones the
three pinned upstream repos and pip-installs the CLI + planner from PyPI/GitHub — the sibling repos live
outside the build context and can't be `COPY`ed), and it takes several minutes. Override coordinates on
the CLI, e.g. `make image VERSION=0.2.0` or `make image BENCH_REF=v0.7.1`. Defaults come from the
Makefile: `IMAGE=ghcr.io/llm-d/llm-d-benchmarking-agent`, `VERSION=0.1.0`, `BENCH_REF=v0.7.0`.

### Log in to GHCR

```bash
echo "$GH_PAT" | docker login ghcr.io -u <your-github-username> --password-stdin
```

`$GH_PAT` must be a GitHub Personal Access Token with the **`write:packages`** scope.

### Publish

```bash
make image-publish  # runs `make image` then `docker push ghcr.io/llm-d/llm-d-benchmarking-agent:0.1.0`
```

### Make the GHCR package public

By default the chart pulls with `IfNotPresent` and **no imagePullSecret**, which only works if the GHCR
package is public. After the first push:

1. GitHub → your profile/org → **Packages** → `llm-d-benchmarking-agent`.
2. **Package settings** → **Danger Zone** → **Change visibility** → **Public**.

If you keep it **private** instead, operators must configure an `imagePullSecret` — tell them to create a
docker-registry secret and pass `--set imagePullSecrets[0].name=<secret>` (the chart's `imagePullSecrets`
list is wired through to the Deployment).

### Keep the version in sync

`VERSION` in the `Makefile` and `appVersion` in `deploy/helm/llm-d-benchmarking-agent/Chart.yaml` must
match (both are `0.1.0` today). The chart's default `image.tag` (`values.yaml`) also tracks it. Bump all
three together when you cut a new release.

### CI option (TODO — not yet active)

`.github/workflows/image-publish.yml` is a **scaffold** and is currently under the **project** dir.
**GitHub only reads workflows from the repo-root `.github/workflows/`**, so as written it does nothing.
To enable automated publish-on-tag:

1. **Move** the file to the **repo root**: `<repo-root>/.github/workflows/image-publish.yml`.
2. Verify its build paths still point into the monorepo subdir — it already sets
   `context: llm-d-benchmarking-agent-project` and `file: llm-d-benchmarking-agent-project/Dockerfile`,
   plus `working-directory: llm-d-benchmarking-agent-project`. Confirm these against the final layout.
3. Ensure the GHCR package is public (or pull secrets are configured) and org **package-write**
   permissions are granted to Actions.

Once wired, **pushing a `v*` git tag** (e.g. `git tag v0.1.0 && git push origin v0.1.0`) builds and
pushes `${IMAGE}:<tag>` **and** `${IMAGE}:latest` automatically, authenticating with the built-in
`GITHUB_TOKEN` (no PAT needed in CI). `timeout-minutes: 60` accounts for the heavy build.

### Decisions to sign off (flag these before release)

- **(a) skopeo binary source.** `containers/skopeo` ships **no upstream Linux binary**, so the Dockerfile
  fetches skopeo from the third-party static-build mirror `lework/skopeo-binary` (pinned to
  `SKOPEO_VERSION=1.20.1`). **Accept the third-party mirror, or switch to Debian's apt `skopeo`**
  (apt-installed = simpler provenance but **unpinned/floating** version). Recorded in `NOTICE`.
- **(b) GHCR public vs private.** Public = zero-config pulls (`IfNotPresent`, no secret). Private = every
  operator must configure an imagePullSecret. Pick one and document it for operators.
- **(c) Repin upstream refs when a tag lands.** The image pins `llm-d` and `llm-d-skills` to exact
  **main-branch SHAs** (`LLMD_REF`, `SKILLS_REF` in the Dockerfile / `NOTICE`) — reproducible, but not
  release tags — and `llm-d-benchmark` to the tag `v0.7.0`. **Decide whether to repin the two
  SHA-pinned repos to a release tag once upstream cuts one.**

---

## 3. OPERATOR — installing the service

Steps a cluster user runs to stand up the agent.

### Prereqs

- `kubectl` and `helm` on PATH (the installer preflights both).
- A reachable cluster — the current kube-context, or pass `--kubeconfig` / `--context`. Works with kind or a real cluster.
- Enough rights to **create the namespace + a namespaced Role/RoleBinding + ServiceAccount** (effectively cluster-admin, or admin on a pre-created namespace).

### Basic install

```bash
./scripts/install_service.sh --oauth-token <TOKEN>
# or: export CLAUDE_CODE_OAUTH_TOKEN=<TOKEN>   &&   ./scripts/install_service.sh
```

On success it prints the port-forward command to reach the UI. `<TOKEN>` is a Claude subscription
token from `claude setup-token` — the PRIMARY auth path (see "Provide auth for chat" below). The
`--anthropic-key` API-key path is the documented fallback.

### Common flags

```
-n, --namespace NS          target namespace          (default: llmd-bench; created if absent)
-r, --release NAME          Helm release name         (default: bench-agent)
    --image REPO            image repository          (default: ghcr.io/llm-d/llm-d-benchmarking-agent)
    --tag TAG               image tag / VERSION       (default: 0.1.0)
    --image-pull-policy P   Always|IfNotPresent|Never (default: IfNotPresent)
    --build                 docker-build the image locally + use it (air-gapped/dev; pullPolicy→Never)
    --oauth-token TOKEN     Claude subscription token (default: $CLAUDE_CODE_OAUTH_TOKEN) → claude-agent-sdk  [PRIMARY]
    --anthropic-key KEY     Anthropic API key         (default: $ANTHROPIC_API_KEY) → anthropic  [FALLBACK]
    --orchestrator-image IMG   image for in-cluster orchestrated benchmark Jobs (config.orchestratorImage)
    --kubeconfig PATH       kubeconfig file           (default: $KUBECONFIG / ~/.kube/config)
    --context NAME          kube-context              (default: current-context)
    --timeout DUR           helm --wait timeout       (default: 5m)
    --dry-run               render + validate via `helm --dry-run`; apply nothing
```

Validate without applying anything first:

```bash
./scripts/install_service.sh --namespace bench --release bench-agent --dry-run
```

For local/kind or air-gapped clusters, `--build` builds the image on the spot (docker) and defaults its
pullPolicy to `Never` — you must then load it onto the nodes yourself, e.g.
`kind load docker-image ghcr.io/llm-d/llm-d-benchmarking-agent:0.1.0`.

### Provide auth for chat (required for a usable service)

The chat provider defaults to the **Claude Agent SDK**, authenticated by your **Claude Max/Pro
subscription** via a **`CLAUDE_CODE_OAUTH_TOKEN`** — no metered API key needed. The baked `claude` CLI
reads this token from the environment **headlessly** (no browser, no TTY), so the subscription auth
**does work inside a Pod**. (This corrects earlier guidance: an `ANTHROPIC_API_KEY` is no longer
required — it is now the fallback.)

**Get the token** — on a machine already logged into your Claude plan:

```bash
claude setup-token      # prints a long-lived (~1 year) OAuth token; copy it
```

**Provide it** to the service (pick one):

```bash
./scripts/install_service.sh --oauth-token <TOKEN>                        # installer flag
export CLAUDE_CODE_OAUTH_TOKEN=<TOKEN> && ./scripts/install_service.sh    # env var
# or straight to Helm:
helm upgrade --install bench-agent deploy/helm/llm-d-benchmarking-agent -n <ns> --create-namespace \
  --set-string secret.claudeCodeOauthToken=<TOKEN>
```

More secure alternative (keeps the token off the CLI / Helm values) — create the Secret yourself and
reference it:

```bash
kubectl -n <ns> create secret generic bench-agent-llm \
  --from-literal=CLAUDE_CODE_OAUTH_TOKEN=<TOKEN> \
  --from-literal=ANTHROPIC_API_KEY= \
  --from-literal=OPENAI_API_KEY= \
  --from-literal=HF_TOKEN=
helm upgrade --install bench-agent deploy/helm/llm-d-benchmarking-agent \
  -n <ns> --create-namespace \
  --set secret.create=false --set secret.existingSecret=bench-agent-llm
```

(`secret.existingSecret` must carry the keys `CLAUDE_CODE_OAUTH_TOKEN` / `ANTHROPIC_API_KEY` /
`OPENAI_API_KEY` / `HF_TOKEN`.)

**Fallback — a metered Anthropic API key.** To bill per-token against the API instead of a
subscription, pass an API key; the installer then selects `LLM_PROVIDER=anthropic` +
`secret.anthropicApiKey`:

```bash
./scripts/install_service.sh --anthropic-key sk-ant-...
# or: export ANTHROPIC_API_KEY=sk-ant-...  &&  ./scripts/install_service.sh
```

**Neither?** The app still deploys (SDK, chat disabled): `/healthz` and the keyless `/readyz` serve,
but chat stays off (`/readyz` reports the provider component not ready) until a token or key is set.

> **On terms of service.** Anthropic's auth docs support running your Claude subscription **headlessly**
> via `CLAUDE_CODE_OAUTH_TOKEN` for the `claude` CLI — exactly what this Pod does (your own subscription
> token, in your own Pod). Their Agent-SDK guidance separately steers third-party developers who expose
> *claude.ai login to their own end-users* toward API keys. Running **your own** token in **your own**
> cluster is the intended CLI-headless use; if you plan to expose the service to other users as a
> product, prefer the API-key fallback. Make an informed choice.

### Reach the UI

```bash
kubectl -n <ns> port-forward svc/<release>-llm-d-benchmarking-agent 8000:8000
# then browse http://localhost:8000
```

With the defaults the service name is `bench-agent-llm-d-benchmarking-agent` in namespace `llmd-bench`.
Full post-install notes: `helm get notes <release> -n <ns>`.

### Security model (one paragraph)

The chart creates a **namespace-scoped ServiceAccount + least-privilege Role** (no ClusterRole). The Role
grants exactly: `batch/jobs` → `create,get,list,watch,patch,delete`; `pods` → `get,list,watch`;
`pods/log` → `get`; `configmaps` → `get,list,watch,create,patch` (the agent's own sweep-checkpoint
ConfigMaps). It grants **no access to Secrets or Roles** and nothing cluster-wide — so the agent's
commands can only affect **this one namespace**. Residual risk to keep in mind: **because it can create
Jobs, a Job it defines could mount any Secret that lives in this namespace.** Therefore **keep sensitive
Secrets out of the agent's namespace** (give it a dedicated namespace with only its own LLM/HF Secret).

### Enable persistence (optional)

By default `/workspace` is an ephemeral `emptyDir` — **sessions do not survive a pod restart.** Back it
with a PVC so they do:

`install_service.sh` has no dedicated persistence flag, so pass these to `helm` directly (or via an
`-f values.yaml`):

```bash
helm upgrade --install bench-agent deploy/helm/llm-d-benchmarking-agent -n <ns> --create-namespace \
  --set-string secret.claudeCodeOauthToken=<TOKEN> \
  --set workspace.persistence.enabled=true \
  --set workspace.persistence.storageClass=<sc> \
  --set workspace.persistence.size=5Gi \
  --set workspace.persistence.accessMode=ReadWriteOnce   # storageClass "" = cluster default
```

(`workspace.sizeLimit` caps the emptyDir/PVC; `/tmp` is always ephemeral.)

### Enable the orchestrated K8s-Job benchmark path (optional / advanced)

By default the agent runs `llmdbenchmark` **in its own Pod** (the local-CLI path). To instead have it
submit benchmark runs as **separate Kubernetes Jobs**, point `config.orchestratorImage` at the **same
agent image ref** (the baked image has `llmdbenchmark` symlinked onto PATH, which the Job path resolves):

```bash
./scripts/install_service.sh --oauth-token <TOKEN> \
  --orchestrator-image ghcr.io/llm-d/llm-d-benchmarking-agent:0.1.0
```

Left empty (default), the `orchestrate_benchmark_run` tool refuses rather than submitting an unrunnable Job.

---

## 4. TESTING — verify locally before shipping

Steps **you** run to prove the cluster-service path works end-to-end before publishing.

### Add auth to `.env`

The live-chat assertion needs real auth — the OAuth token (primary) or an API key (fallback). The
adapter reads either from the project `.env`:

```bash
echo 'CLAUDE_CODE_OAUTH_TOKEN=...' >> .env   # PRIMARY (from `claude setup-token`); project .env (gitignored)
# or the fallback:
echo 'ANTHROPIC_API_KEY=sk-ant-...' >> .env
```

### Run the kind adapter

```bash
bash testing/cluster-service-sim/run.sh
```

> Note: `testing/cluster-service-sim/run.sh` is the kind end-to-end adapter for this feature. It builds
> the image, spins up a kind cluster, deploys via the real `install_service.sh` + chart, and asserts
> `/healthz` + `/readyz` + `/api/provider`, plus the **RBAC least-privilege boundary** (an in-Pod
> `kubectl delete ns kube-system` must be refused `Forbidden`) and — only when an OAuth token or an
> Anthropic key is present — one live-chat round-trip over `/ws`. Useful flags: `--keep` (leave the cluster up to inspect
> it), `--no-build` (reuse an already-built image and skip the ~1 GB rebuild).

### Run on a fresh environment (WSL)

To prove it on a pristine box with nothing pre-installed, use the throwaway-distro harness at the
**monorepo root** (run these from the monorepo root, inside your normal WSL distro):

```bash
# Provision a clean 'kind-fresh' WSL distro WITH docker + kind + kubectl pre-installed.
# Add --detach so the app runs in the background and your shell returns (default runs it in foreground).
bash fresh-env/run-app.sh --with-runtime --detach

# Then run the adapter INSIDE that distro (the project is injected at /root/llm-d-benchmarking-agent-project):
wsl -d kind-fresh -u root -- bash -lc \
  'cd /root/llm-d-benchmarking-agent-project && bash testing/cluster-service-sim/run.sh'
```

To wipe the distro and rebuild from the golden image, just re-run `bash fresh-env/run-app.sh` (add
`--reuse` to skip the wipe). One-time prereq for the harness: `bash fresh-env/setup-base.sh` (builds the
golden base image).

---

## 5. Quick reference

### Key commands

| Task | Command |
|---|---|
| Build image | `make image` |
| Build with a version | `make image VERSION=0.2.0` |
| Log in to GHCR | `echo $GH_PAT \| docker login ghcr.io -u <user> --password-stdin` |
| Publish image | `make image-publish` |
| Install service | `./scripts/install_service.sh --oauth-token <TOKEN>` |
| Validate only | `./scripts/install_service.sh --dry-run` |
| Local build + install | `./scripts/install_service.sh --build` (+ `kind load docker-image <img>`) |
| Reach the UI | `kubectl -n <ns> port-forward svc/<release>-llm-d-benchmarking-agent 8000:8000` |
| Post-install notes | `helm get notes <release> -n <ns>` |
| Test (kind adapter) | `bash testing/cluster-service-sim/run.sh` |
| Test (fresh WSL env) | `bash fresh-env/run-app.sh --with-runtime --detach` |

### Key config knobs

| Knob | Flag / value | Default |
|---|---|---|
| Image repo/tag | `--image` / `--tag`, or `image.repository` / `image.tag` | `ghcr.io/llm-d/llm-d-benchmarking-agent` / `0.1.0` |
| Pin by digest | `image.digest` (wins over tag) | `""` |
| Chat auth — OAuth token (PRIMARY) | `--oauth-token` / `$CLAUDE_CODE_OAUTH_TOKEN`, or `secret.claudeCodeOauthToken` → claude-agent-sdk | empty |
| Chat auth — Anthropic key (FALLBACK) | `--anthropic-key` / `$ANTHROPIC_API_KEY`, or `secret.anthropicApiKey` → anthropic | empty |
| No token & no key | chat disabled; `/healthz` + keyless `/readyz` still serve | — |
| Existing Secret | `secret.create=false` + `secret.existingSecret=<name>` | `create=true` |
| Namespace / release | `--namespace` / `--release` | `llmd-bench` / `bench-agent` |
| Persistence | `workspace.persistence.enabled=true` (+ `storageClass`/`size`/`accessMode`) | `false` (ephemeral) |
| Orchestrated Jobs | `--orchestrator-image <img>` / `config.orchestratorImage` | `""` (in-Pod local CLI) |
| Pull policy | `--image-pull-policy` / `image.pullPolicy` | `IfNotPresent` (`Never` with `--build`) |
| Pull secret (private GHCR) | `imagePullSecrets[0].name=<secret>` | `[]` |
