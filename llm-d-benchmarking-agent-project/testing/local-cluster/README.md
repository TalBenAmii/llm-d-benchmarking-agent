# Local mock-GPU cluster harness — DEBUG-ONLY

This directory lets you exercise the agent's **multi-GPU orchestration / scheduling** paths on
your laptop with **no real GPUs and no cloud spend**, by standing up a real Kubernetes cluster
that *advertises* fake `nvidia.com/gpu` resources.

> **This is debugging infrastructure, NOT part of the product.** Nothing here is imported by
> `app/`, referenced by the Helm/Kustomize charts, or baked into the container image. See
> [§ Product safety](#product-safety-how-we-keep-this-out-of-the-shipped-artifact).

## Why this works (no app code, no fake hardware)

The agent learns about GPUs **only from what the cluster advertises** as allocatable
`nvidia.com/gpu` — it never runs `nvidia-smi` or otherwise verifies real silicon
(`app/orchestrator/job.py:41` calls it "the conventional extended-resource name a GPU
device-plugin advertises"). `scheduling.gpu_count` simply becomes a pod
`resources.limits["nvidia.com/gpu"]`. So a node that *claims* to have GPUs is indistinguishable
from a real one to every scheduling code path: `gpu_count`, `gpu_resource`, `gpu_type_label`
(node affinity on `nvidia.com/gpu.product`), `node_selector`, `tolerations`, pod anti-affinity,
and topology spread (`avoid_topology_key`) all get genuinely exercised against the real K8s
scheduler.

## What it does and does NOT give you

| Goal | This harness? |
|---|---|
| Agent's **scheduling / placement / orchestration** logic (incl. `orchestrate_sweep` fan-out, retry/dead-letter, checkpoint, multi-replica topology) | ✅ **yes, free** |
| Multi-replica topologies that **actually respond** (CPU sim engine) → a real BR-v0.2 report | ✅ yes (kind mode + sim engine) |
| **Real performance numbers** (true TTFT/TPOT/throughput under a multi-GPU topology) | ❌ no — needs real GPUs (see `docs/GPU_CLUSTER_RUNBOOK.md` for a single real GPU; rent a 2-GPU box for real multi-GPU) |

## Which mode

| Mode | Pods really run? | Real report? | Best for |
|---|---|---|---|
| **`kind`** (default) | ✅ on CPU | ✅ (sim-valued) | multi-replica topologies, end-to-end deploy→bench→observe with fake GPUs |
| **`kwok`** | ❌ faked | ❌ | scheduling / fan-out **at scale** (high `orchestrate_sweep` `max_parallel`, autoscaling), dozens of fake nodes |

- **kind mode** creates a *separate* multi-node cluster named `llmd-mock` (it does **not** touch
  your CPU-sim `llmd-quickstart` cluster) and PATCHes the worker nodes to advertise fake GPUs via
  the upstream [node-status extended-resource](https://kubernetes.io/docs/tasks/administer-cluster/extended-resource-node/)
  mechanism. Because no NVIDIA device plugin is running, kubelet leaves the resource alone and the
  capacity sticks — yet the pods are ordinary CPU pods that actually run.
- **kwok mode** installs the [kwok](https://github.com/kubernetes-sigs/kwok) controller into the
  current cluster and adds fake GPU nodes (no kubelet; pods are faked Running).

## Usage

```bash
cd testing/local-cluster

# kind mode: cluster + fake GPUs on every node
./setup.sh                       # multi-node, 4 fake GPUs each
./setup.sh --single-node         # 1-node cluster — USE THIS ON WSL2 (see caveat below)
./setup.sh --gpus 8              # 8 fake GPUs per node
kubectl get nodes -o custom-columns=NODE:.metadata.name,GPU:.status.capacity.'nvidia\.com/gpu'

# ... point the agent at it (its kubeconfig context is now kind-llmd-mock) and drive a run that
#     requests GPUs, e.g. orchestrate_sweep with scheduling.gpu_count / gpu_type_label ...

./teardown.sh                    # delete the llmd-mock cluster

# kwok mode: fake-node fleet for scheduling-at-scale (applied to the CURRENT context)
./setup.sh --mode kwok
./teardown.sh --mode kwok
```

Requirements: `kubectl` + `kind` (kind mode) on PATH; an internet connection the first time
(kwok mode pulls the pinned kwok release manifests). All free.

> **⚠️ WSL2 caveat.** Multi-node real-kubelet kind does **not** come up on WSL2 — the worker
> nodes fail to join (`kubelet not healthy` / cgroup limitation; the control-plane is fine). Use
> **`./setup.sh --single-node`** there: a single node still proves GPU-**resource** scheduling
> end-to-end (Jobs request `nvidia.com/gpu`, schedule onto the fake-GPU node, run, complete). For
> cross-node **placement** (anti-affinity / topology spread) on WSL2, use **`--mode kwok`** (faked
> nodes, so no real report). *Verified end-to-end on WSL2 2026-06-20: a real `orchestrate_sweep`
> ran 3 Jobs requesting fake GPUs, all Complete under `max_parallel=2`, with a checkpoint ConfigMap.*

## Product safety — how we keep this out of the shipped artifact

The product is exactly what the `Dockerfile` COPYs (`app/ security/ knowledge/ ui/` + two
metadata files) plus the `deploy/` charts. This harness lives entirely outside that set and is
guarded three ways:

1. **`.dockerignore` excludes `testing/`** — it can't even enter the build context.
2. **`tests/test_product_boundary.py`** asserts (a) the Dockerfile COPY set never names
   `testing/`, (b) `.dockerignore` excludes it, and (c) no module under `app/` imports it. A
   future change that wires the mock into the product fails CI loudly.
3. **No custom images / no app code** — the fake-GPU mechanisms are upstream (kind node PATCH,
   kwok). There is nothing here to maintain inside the product, and the agent drives the mock
   cluster unchanged because it can't tell fake GPUs from real ones.

If you need the override maps that swap upstream multi-GPU guides onto the CPU sim engine, put
them here as fixtures and feed them to the agent at runtime via
`write_and_validate_config(content=…)` — do **not** add them to `knowledge/` (which ships).

## Related

- `docs/GPU_CLUSTER_RUNBOOK.md` — the real **single**-GPU path (your RTX 4060): real vLLM, real
  numbers, one replica. This harness is its mock multi-GPU counterpart.
- `app/orchestrator/job.py` — `Scheduling` (how `gpu_count` / affinity / tolerations become a
  manifest); `app/tools/orchestrate.py` — `orchestrate_sweep`.
