# Resource management: GPU selection, node placement, and anti-starvation

This is the JUDGMENT for the optional `scheduling` argument of `orchestrate_benchmark_run`.
The tool is pure mechanism — it places whatever you give it into the correct Job-manifest
paths (`nodeSelector`, `affinity`, `tolerations`, the GPU resource request). **You** decide
*which* hardware and *where* to run, using this file. Omit `scheduling` entirely and the Job
is the generic cpu/memory baseline (exactly as before).

The proposal (§4) demands two things of a benchmark Job: (1) it requests the **right
hardware** so the load generator / harness can actually drive the system under test, and (2)
it does **not starve the llm-d stack being measured** by co-scheduling onto the same nodes.

> For **autoscaling** the served stack (scale replicas under load) rather than placing a one-off
> benchmark Job, see `read_knowledge('autoscaling')` — the WVA/HPA skill adapter.

## The `scheduling` object (what each key is for)

| key | what it does | when to set it |
|-----|--------------|----------------|
| `gpu_count` | request N GPUs (added to requests **and** limits) | only if the in-Job workload itself needs a GPU (rare for a pure load generator; common if the harness runs the model) |
| `gpu_resource` | the extended-resource name (default `nvidia.com/gpu`) | non-NVIDIA accelerators: `amd.com/gpu`, `habana.ai/gaudi`, `google.com/tpu` |
| `gpu_type_label` | `[node_label_key, value]` pinning the GPU **type** | when a scenario needs a *specific* GPU (A100 vs L4 vs H100) for a fair/representative measurement |
| `node_selector` | exact node-label matches | pin to a node pool, a zone, an arch, an instance type |
| `tolerations` | tolerate node taints | the GPU pool is tainted `dedicated=gpu:NoSchedule` (common) |
| `affinity` | a raw Kubernetes affinity block, merged verbatim | full power: node affinity expressions, pod affinity, custom anti-affinity |
| `avoid_labels` | schedule the pod AWAY from nodes already running pods with these labels | **the anti-starvation lever** — keep the benchmark off the nodes serving the measured stack |
| `avoid_topology_key` | the topology domain `avoid_labels` spreads across (default `kubernetes.io/hostname`) | use `topology.kubernetes.io/zone` to avoid a whole zone instead of a node |

## Picking a GPU type (representative, not just "a GPU")

A benchmark number is only meaningful relative to the accelerator it ran on. Choose the GPU
TYPE to match the scenario you're characterizing, then pin it with `gpu_type_label` so the
scheduler can't quietly land you on a different card:

- **Large model / long context / disaggregated prefill** → high-memory data-center GPU
  (A100-80GB, H100). Pin e.g. `["nvidia.com/gpu.product", "NVIDIA-A100-SXM4-80GB"]`.
- **Small/quantized model, cost-sensitive serving, high-throughput-per-dollar** → L4 / L40S /
  A10. Pin the corresponding product label.
- **CPU-only sim (the kind/quickstart path, `cicd/kind`)** → set **no** GPU fields at all. The
  baseline cpu/memory request is correct; requesting a GPU here makes the Job unschedulable.

The `nvidia.com/gpu.product` (and `.memory`, `.count`) node labels come from the
**NVIDIA GPU Operator / GPU Feature Discovery**. Confirm the exact label values on the target
cluster first (`kubectl get nodes --show-labels`) — don't guess the product string. If GFD
isn't installed, fall back to a plain `node_selector` on whatever label the pool uses.

## Requesting GPUs correctly

- Extended resources (`nvidia.com/gpu` etc.) must have **request == limit** — the tool sets
  both for you from `gpu_count`. You cannot over-commit a GPU.
- Only request a GPU for the Job if the **in-Job process** needs one. A pure load generator
  (inference-perf / guidellm hitting an endpoint over HTTP) usually needs **no** GPU — the
  GPUs belong to the *served* model, which is a separate deployment. Requesting a GPU you
  don't use steals it from the stack under test (the opposite of what we want).
- If the cluster has no GPUs (kind/CI), never set GPU fields — the Job will sit `Pending`
  (Unschedulable). The `check_capacity` pre-flight will also flag this.

## Anti-starvation: keep the benchmark off the served nodes

This is the heart of proposal §4. If the load generator lands on the **same node** as the
model-server pods it's measuring, it competes for CPU/network/memory bandwidth and **corrupts
the measurement** (you measure contention, not the system). Two ways to prevent it:

1. **`avoid_labels` (simplest).** Give the labels the measured llm-d stack's pods carry, e.g.
   `{"llm-d.ai/role": "decode"}` or `{"app.kubernetes.io/part-of": "llm-d"}`. The tool renders
   a **required pod anti-affinity** so the benchmark pod will not be scheduled onto a node
   (the default `avoid_topology_key`) already running such a pod. Verify the real label keys on
   the deployed stack first (`kubectl get pods -n <ns> --show-labels`).
2. **`node_selector` / `tolerations` (a dedicated pool).** If the cluster has a separate
   node pool for load generation, pin the Job there with `node_selector` and tolerate its
   taint. This is the most robust isolation when a pool exists.

Prefer **required** anti-affinity (what `avoid_labels` emits) over *preferred*: a benchmark
that silently co-locates is worse than one that stays `Pending` until you fix capacity, because
the silent one produces a confidently-wrong number.

### Quotas / fairness

- If the namespace has a `ResourceQuota`, the Job's requests count against it — keep the
  benchmark's `cpu`/`memory`/`gpu_count` modest so a measurement run can't exhaust the quota
  the served stack also draws from.
- For a **sweep**, the orchestrator already caps parallelism (`max_parallel`); combine that
  cap with a small per-Job footprint so N concurrent treatments don't collectively starve the
  cluster. More parallelism ≠ faster if the treatments fight over the same nodes/GPUs.

## Worked examples

- **CPU-only kind quickstart** (`cicd/kind`, inference-perf sanity): omit `scheduling` — the
  baseline is correct.
- **Load generator vs a GPU-served stack, single node pool:** no GPU on the Job, but
  `{"avoid_labels": {"llm-d.ai/role": "decode"}}` so the generator stays off the serving node.
- **In-Job harness that itself needs one A100, on a tainted GPU pool:**
  `{"gpu_count": 1, "gpu_type_label": ["nvidia.com/gpu.product", "NVIDIA-A100-SXM4-80GB"],
  "tolerations": [{"key": "dedicated", "operator": "Equal", "value": "gpu", "effect": "NoSchedule"}]}`.
- **Pin to a zone and avoid the served zone:** `node_selector` for the desired zone +
  `avoid_labels` with `avoid_topology_key: "topology.kubernetes.io/zone"`.
