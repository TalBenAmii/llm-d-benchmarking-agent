# Preconditions & the "don't redeploy" rule

Always call `probe_environment` before proposing a plan or doing anything. Read the
structured result and reason about it — do not assume.

## What the signals mean
- `container_runtime.daemon_up == false` → Docker/Podman is installed but not running. If
  `socket_permission_error == true`, it's a permissions issue (docker group / rootless),
  not a "down" daemon — explain the fix. To (re)start the Docker daemon you may run
  `run_command argv=["install_prereqs.sh","--docker"]` (it skips the install if docker is
  already present and just tries to start it); on WSL/Docker-Desktop it may still need to
  be started manually. Never run raw `sudo` — only the pinned `install_prereqs.sh` is
  allowlisted.
- `repos.<name>.present == false` → clone with `ensure_repos` before anything else.
- `tools.<x> == false` → that tool is missing from PATH. `run_setup` (install.sh) installs
  most of them (kubectl, helm, helmfile, jq, yq, kustomize, skopeo, crane, and uv→Python
  3.11). It does NOT install `docker` or `kind` — but you can, with the vetted installer:
  - `tools.docker == false` (and no podman) → install Docker Engine with
    `run_command argv=["install_prereqs.sh","--docker"]` (mutating; needs root or
    passwordless sudo). Relay any warning the installer prints (e.g. daemon couldn't
    auto-start, or the user must re-login for docker-group membership).
  - `tools.kind == false` → install the kind binary with
    `run_command argv=["install_prereqs.sh","--kind"]` (combine as `--docker --kind` or
    `--all` to do both at once).

## Guide-based deploy: the UPSTREAM client prerequisites (install-deps.sh)
When the user wants to deploy a published **llm-d well-lit-path guide** (the guide deploy path
— `optimized-baseline` is the reference; see `deploy_path_playbook.md`), the guide expects the
**deployment client toolchain** to be present: `helm` + the **helm-diff** plugin, `helmfile`,
`kustomize`, `yq` (mikefarah), and `kubectl`. The llm-d guides ship their own installer for
exactly this — `helpers/client-setup/install-deps.sh` in the llm-d repo (allowlisted as the
bare `install-deps.sh`). OFFER to run it when those client tools are missing **before** a
guide-based deploy:
- `run_command argv=["install-deps.sh"]` → install the basic guide client tools.
- `run_command argv=["install-deps.sh","--dev"]` → also install `chart-testing` (ct), the
  Helm chart-testing tool (only needed if the user will lint/test charts).
It is **mutating** (needs root or passwordless sudo) → the user must Approve; relay any warning
it prints. Know the THREE distinct install steps and pick the right one — do NOT conflate them:
- `install_prereqs.sh` (project) → the Docker **daemon** + the **kind** binary (local cluster
  substrate). Needed for the kind/sim path AND as the host substrate for a local guide deploy.
- `install.sh` (benchmark repo, via `run_setup`) → the **benchmark framework** venv/toolchain
  (also pulls helm/helmfile/kustomize/yq/kubectl) — run it when you'll drive `llmdbenchmark`.
- `install-deps.sh` (llm-d guide repo) → the GUIDE's **deployment client** toolchain
  (helm/helm-diff/helmfile/kustomize/yq/kubectl) — run it when the user deploys an llm-d guide
  directly and `run_setup`/`install.sh` hasn't already provided those tools. If `install.sh`
  already ran and the client tools are present, you do NOT also need `install-deps.sh`.
- `venv.exists == false` → run `run_setup` before any `llmdbenchmark` command.
- `kind_clusters.clusters` empty (but `kind` is present) → no local cluster yet. For the
  quickstart, create one yourself: `run_command argv=["kind","create","cluster","--name","llmd-quickstart"]`.
- `kind_clusters.clusters` non-empty → a local cluster exists; it may host a stack.
- `kube_context` / `cluster_info.reachable` → whether kubectl can talk to a cluster.

## The redeploy decision (important)
Before standing up anything, decide using `stack` (pass the target `namespace`):
- `stack.detected == true` (ready pods exist) → a stack is ALREADY running. Do **not**
  redeploy. Tell the user what's running and offer to:
  (a) benchmark the existing stack (go straight to `run`), or
  (b) tear it down and redeploy fresh (requires approval).
- `stack.exists == true` but `ready_count == 0` (pods Pending/CrashLoop/Error) → the stack
  is **stale**. Surface the pod states, and offer a gated `teardown` then a fresh standup.
- `stack.exists == false` → safe to deploy.

When unsure whether the running stack matches the use case, you can also preview endpoints
read-only with `execute_llmdbenchmark subcommand=run flags={list_endpoints:true}`.

## Endpoint readiness before a benchmark (stronger than pod presence)
`stack.detected` proves a pod is *Ready*, but that is NOT the same as the inference
**endpoint** actually serving traffic: a pod can be Running yet failing its readiness probe,
so it is absent from any Service's ready backing endpoints. Before benchmarking an existing
stack, run `check_endpoint_readiness(namespace=...)` — a read-only gate that reads
`kubectl get endpoints` (does a Service have a *ready backing endpoint*?) and corroborates
with the CLI's `run --list-endpoints`. It returns a structured `ready` verdict; when not
ready it includes a `standup_suggestion`.
- `ready == true` → the endpoint is serving; go ahead and benchmark.
- `ready == false` → do **not** submit a benchmark. Tell the user, and **offer** the
  approval-gated standup the suggestion names (`execute_llmdbenchmark subcommand="standup"`).
  Standing up is mutating — never do it without the user's explicit approval. The DECISION to
  stand up is yours and the user's; the readiness check is only the mechanism.
`orchestrate_benchmark_run` applies this same gate automatically before submitting a Job
(see knowledge/orchestrator.md).

## Targeting a remote cluster (-k / kubeconfig, URL, token)
By default every `llmdbenchmark` command runs against your **ambient kube context** — for the
quickstart that is the local Kind cluster `probe_environment` already sees. You only need to
target a **different** cluster when the user explicitly points you at a remote one (e.g. "run
this on our GKE/OpenShift staging cluster", "use this kubeconfig", "here's an API URL + token").
WHEN to do this is the user's call — never reach for a remote cluster on your own.

There are two ways to target a non-ambient cluster, both threaded through `execute_llmdbenchmark`:

1. **A non-default kubeconfig FILE** — the simplest and preferred lever. Pass the top-level
   `kubeconfig="<path>"` argument; it is emitted as the CLI's `-k/--kubeconfig <path>` (upstream
   `LLMDBENCH_KUBECONFIG`) and is valid on every subcommand. The path is a **non-secret** file
   path (allowlist-pinned, no `..` traversal); it appears normally in the command trail. Use this
   whenever the user already has a kubeconfig for the target cluster — it also carries the right
   context, CA, and auth in one file.
2. **An API-server URL + bearer TOKEN** — when there is no kubeconfig file, pass
   `flags={"cluster_url": "https://api.cluster:6443", "cluster_token": "<token>"}`. These are
   carried **backend-only** as the `LLMDBENCH_CLUSTER_URL` / `LLMDBENCH_CLUSTER_TOKEN` child-env
   vars; the benchmark CLI's `kube_connect` uses them as the API host + `Bearer` token.

**The token is a SECRET. Treat it exactly like an HF token:**
- It is **never** a CLI flag and **never** an argv token, so it can never appear in a `command`
  event, the executed-command trail, a log line, or anything the browser sees. It rides only the
  scrubbed backend child env.
- **Never echo the token back to the user**, never put it in a plan summary, and never read it
  into any other tool's arguments. If the user pastes a token in chat, use it via
  `flags.cluster_token` and do not repeat it.
- The cluster **URL** is not secret (you may name it); only the token is.

Whichever lever you use, the cluster you target is still subject to all the usual gates — run
`probe_environment` / `check_endpoint_readiness` against it first, and keep the same
"don't redeploy a healthy stack" and approval-before-mutation rules below. Prefer `kubeconfig`
when a file exists; use URL+token only when that's all the user has.

## Real-cluster pre-flight gates (before a long standup)
These read-only gates catch a doomed standup UP FRONT instead of letting an opaque stall eat
minutes. Each returns FACTS ONLY — load its guide for the verdict. The **Kind / CPU-sim
quickstart (`cicd/kind`)** is largely EXEMPT (modern K8s, the inference-**sim** image, a small
node), so don't block it on these.
- **K8s version / image tags** — `probe_environment(checks=["cluster_preconditions"], spec=…)`
  returns the cluster's server `major.minor` plus the spec's pinned image tags. The verdict
  (sidecar prefill/decode needs K8s 1.33+; 1.29 is the minimum; flag below-minimum vLLM / NIXL /
  UCX / NVSHMEM tags) lives in `read_knowledge("infrastructure_preconditions")`.
- **Accelerator / CPU floor** — `advise_accelerators` reports which extended-resource key each
  node advertises (`nvidia.com/gpu` or the amd / gaudi / tpu / Intel-XPU siblings, vs CPU-only)
  plus allocatable cpu/memory. The verdict (CUDA driver minimums; Device-Plugin vs DRA; the real
  non-sim 64-core/64GB-per-replica floor that Kind/CPU-sim is exempt from) lives in
  `read_knowledge("accelerators")`. Pair it with `check_capacity` (weights + KV cache vs GPU memory).
- **Cloud provider** — `probe_environment(checks=["provider_detection"])` reports the provider
  (openshift / gke / doks / aks vs kind) from node labels, plus GPU taints that leave pods
  `Pending`. The verdict (oc-vs-kubectl, the matching tolerations, per-provider known issues)
  lives in `read_knowledge("infra_providers")`. Any toleration/patch is the user's to approve and
  is authored into the session workspace (the Phase 45 scenario-override path, validated via
  plan / `--dry-run`) — the sibling repos stay read-only.
