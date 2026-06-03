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

## Infra precondition gate (before a real-cluster standup)
Before committing to a **long real-cluster standup** — especially a sidecar-based prefill/decode
(P/D) guide — run `probe_environment` with `checks=["cluster_preconditions"]` and the planned
`spec` (e.g. `spec="cicd/kind"`). This is a read-only go/no-go you give the user UP FRONT,
instead of letting them watch an opaque `Init:0/1` stall eat minutes into the standup before it
fails.

The check returns FACTS only:
- `cluster_preconditions.server_version` — the cluster's **Kubernetes server** `major.minor`
  (from `kubectl version --output json`; a trailing `+` like GKE's `"29+"` is stripped). `null`
  when there's no reachable cluster yet or kubectl returned client-only output.
- `cluster_preconditions.image_tags` — the spec's pinned image tags parsed from its scenario
  YAML (`{name, repository, tag, path}` for each, e.g. `images.vllm` → `v0.8.2`).

The check makes **no verdict** — that judgment lives in
`read_knowledge("infrastructure_preconditions")`. Load it and reason over its thresholds:
- **K8s 1.27 (≤ 1.28):** the sidecar P/D guide won't init (stuck in `Init:0/1`) — tell the user
  to **upgrade to 1.33+** or **pick a non-sidecar path** (e.g. `cicd/kind` / optimized-baseline).
- **K8s 1.29 (1.29–1.32):** runs (clears the 1.29 minimum), but **1.33+ is recommended** for
  full sidecar init-container support — green for non-sidecar paths, "should work, recommend
  1.33+" for sidecar P/D.
- **K8s 1.33+:** green.
- Flag any **below-minimum** image tags (vLLM 0.10.0 / NIXL 0.5.0 / UCX 0.19.0 / NVSHMEM 3.3.9).

This gate is **advisory for the Kind / CPU-sim MVP** — a freshly created Kind cluster runs a
modern Kubernetes, and the quickstart path uses the inference-**sim** image (not real vLLM), so
the vLLM/NIXL minimums don't apply to it. It **matters on a real cluster** the user points us at.

## Accelerator / CPU-inferencing preconditions ("can my hardware run this?")
Before a standup, decide whether the cluster's hardware can actually serve the model. Call
`advise_accelerators` — a read-only tool that reads each node's ADVERTISED resources
(`kubectl get nodes -o json`) and tells you which extended-resource key a node advertises
(`nvidia.com/gpu`, or the `amd.com/gpu` / `habana.ai/gaudi` / `google.com/tpu` / Intel XPU
`gpu.intel.com/{i915,xe}` siblings) **vs CPU-only**, plus each node's allocatable/capacity
cpu + memory. It returns facts only (`any_accelerator`, `cpu_only`, `advertised_resources`,
per-node `accelerators`/`cpu`/`memory`) — then call `read_knowledge('accelerators')` to turn
them into a verdict:
- **GPU-advertised** → name the vendor/resource; for NVIDIA, the node driver must satisfy the
  CUDA 12.9.1 minimum (`>= 525.60.13`, `< 580`; `575.x` recommended) for today's images
  (CUDA 13.0.2 will require `580.65.06`); exposure is via a Device Plugin **or** a DRA driver.
- **CPU-only for a REAL deployment** → a real (non-sim) replica needs **64 cores + 64GB RAM
  each**; check the node's allocatable cpu/memory against that floor and pair the warning with
  `check_capacity`'s GPU-memory sizing.
- **Kind / CPU-sim (`cicd/kind`)** → supported and **exempt** from the 64c/64GB floor (it runs
  llm-d-inference-sim, not a real CPU engine); a small Kind node is fine.

This **complements** `check_capacity` (which sizes weights + KV cache against GPU memory): one
asks "does a node ADVERTISE the accelerator / meet the CPU floor?", the other "will the model
FIT in that accelerator's memory?". The feasibility judgment lives in
knowledge/accelerators.yaml — never in Python.

## Provider-aware precondition pack (oc-vs-kubectl, GPU taints, known issues)
On a real cluster the agent must adapt to the **cloud provider** so its commands work and it can
unstick the common `Pending` / `PROGRAMMED=False` failures. Run `probe_environment` with
`checks=["provider_detection"]` to get the FACTS:
- `provider_detection.provider` — the detected provider (`openshift` / `gke` / `doks` / `aks` /
  `minikube` vs `kind`), inferred from node **labels** (e.g. `cloud.google.com/*` → gke,
  `doks.digitalocean.com/*` → doks, `node.openshift.io/*` → openshift). `kind` when no node
  carries a cloud-provider label (the local quickstart).
- `provider_detection.providers_seen` — every provider hint seen across nodes (mixed/migrating).
- `provider_detection.gpu_taints` — per-node `{node, key, value, effect}` for each taint whose
  key names a GPU; **these are what leave model-server pods `Pending`** until you author a
  matching toleration.

The probe makes **no decision** — that judgment lives in
`read_knowledge("infra_providers")`. Load it and reason over its tables:
- **OpenShift** → prefer **`oc`** (not `kubectl`) for read-only diagnostics (the `oc` allowlist
  entry mirrors kubectl's read-only subcommands); BEFORE standup confirm **no ServiceMesh(OSSM)/
  Istio** install conflicts with the gateway; if a `gpu_taint` leaves pods `Pending`, author the
  matching `nvidia.com/gpu` toleration (Equal+value when the taint is value-bearing — e.g.
  `NVIDIA-L40S-PRIVATE` — else Exists).
- **DOKS / GKE / AKS** → keep `kubectl`; if a `gpu_taint` leaves pods `Pending`, author the
  `nvidia.com/gpu` Exists toleration; surface the provider's known issues — **GKE**: Google
  Managed Prometheus (GMP) is the metrics path, the **"Undetected platform"** vLLM 0.10.0 fix,
  and the **NVSHMEM** DeepEP caveats; **DOKS**: LoadBalancer-`<pending>`; **AKS**: NRI
  locked-memory + `nvidia-peermem`.

Any toleration/patch is **advice the user approves** — author it **into the session workspace**
(prefer the Phase 45 scenario-override path so it is validated via plan/`--dry-run`) and apply it
only as an **approval-gated mutating step**; the sibling repos stay read-only. Every which-CLI /
which-toleration / which-known-issue decision is sourced from knowledge/infra_providers.yaml,
**never** a Python `if/elif`.
