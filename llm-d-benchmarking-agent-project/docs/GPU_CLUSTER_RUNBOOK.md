# GPU cluster runbook — exercise the full agent on a real single GPU

This runbook takes you **beyond the CPU `cicd/kind` quickstart** to a real GPU cluster, so the
agent's deploy → benchmark → observe loop runs **real vLLM inference** and produces **real
throughput/latency numbers** instead of the CPU `llm-d-inference-sim`.

It is written **general-first** — any modest **single NVIDIA GPU** reachable from a local
Kubernetes cluster — with a **Windows + WSL2 + RTX 4060 (8 GB)** worked example throughout
(the example commands assume that host). The same flow works for any single-GPU box (bare-metal
Linux, a cloud VM with one L4/A10, etc.); only the host/driver steps differ.

> **Read this first — the honest ceiling on one 8 GB card.** The agent can drive a real
> *single-replica* serve + benchmark on a consumer GPU. It **cannot** really exercise llm-d's
> *flagship* multi-GPU features — **P/D disaggregation, multi-replica EPP/prefix-aware routing,
> wide-EP, autoscaling** — those need **≥ 2 GPUs** to mean anything. On one card those stay
> **simulated** (`SIMULATE=1`) or you read the published guide numbers. Everything else is real.
> See [§5 Feature coverage](#5-feature-coverage--what-is-real-vs-simulated-on-one-gpu).

---

## 0. Mental model — what the agent already does, and what this runbook adds

The agent is **cluster-agnostic by design** and most of the machinery you need already exists:

- It **targets any cluster you bring** via the kubeconfig — every CLI subcommand accepts
  `--kubeconfig` (`app/tools/schemas/probe.py` `ProbeEnvironmentInput.kubeconfig`,
  `app/tools/execute.py` `build_argv`), and `KUBECONFIG` is passed through to child processes
  (`app/security/runner.py`). minikube writes `~/.kube/config` and sets the current context, so
  **the agent sees your GPU cluster with no extra config**.
- It is **provider- and accelerator-aware** out of the box: `probe_environment` detects the
  provider (openshift / gke / doks / aks / **minikube** / kind) and GPU taints; `advise_accelerators`
  reads advertised `nvidia.com/gpu`; `check_capacity` sizes the run; it authors tolerations from
  taints, provisions the HF secret, installs metrics-server, and offers the guide client toolchain.

What the agent **does not** do, and this runbook supplies:

1. **Host GPU enablement** (Windows driver, NVIDIA Container Toolkit) — outside Kubernetes, §1.
2. **Creating the GPU-capable cluster** — it only auto-creates *kind*; you stand up minikube, §2.
3. **A scenario sized for 8 GB** — upstream GPU specs default to datacenter models (the
   `optimized-baseline` guide is Qwen3-32B on 16× H100). You author a tiny-model override, §3.
4. **Nudging it off the default `cicd/kind` path** — the agent treats GPU deploy as a
   non-default ("future") path today, so you tell it explicitly to use your GPU spec, §4.

---

## 1. Host GPU enablement (Windows + WSL2)

One-time. The goal: `docker run --gpus all … nvidia-smi` works **inside WSL2**.

1. **Install/Update the NVIDIA Windows driver** (GeForce Game Ready or Studio). On WSL2 the CUDA
   driver is provided *by Windows* through `/usr/lib/wsl/lib`. **Do not** `apt install` a Linux
   GPU driver inside WSL — that breaks the WSL CUDA bridge.

2. **Verify the GPU is visible in WSL:**
   ```bash
   nvidia-smi          # must list your RTX 4060
   ls /usr/lib/wsl/lib # libcuda.so.1, libnvidia-ml.so.1, …
   ```

3. **Install the NVIDIA Container Toolkit** in the WSL distro and wire it into Docker:
   ```bash
   # add the nvidia-container-toolkit apt repo (per NVIDIA docs), then:
   sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
   sudo nvidia-ctk runtime configure --runtime=docker
   sudo service docker restart        # or restart Docker Desktop if you use its WSL integration
   ```

4. **Smoke-test GPU-in-Docker** (the single most important gate before touching Kubernetes):
   ```bash
   docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
   ```
   If this prints your GPU, the host is ready. If it fails, fix it here — minikube will not
   expose a GPU the Docker runtime can't.

---

## 2. Stand up minikube with the GPU

minikube's `--gpus all` is the lowest-friction way to schedule `nvidia.com/gpu` on WSL2. It
**requires the docker driver + docker runtime** (not containerd) and **minikube ≥ v1.32.0**.

```bash
minikube start --driver=docker --container-runtime=docker --gpus all

# observability for `observe_run_metrics` / `kubectl top`:
minikube addons enable metrics-server

# minikube wires the NVIDIA device plugin when --gpus is set; if the node does not advertise a
# GPU (see verify step), enable it explicitly:
minikube addons enable nvidia-device-plugin
```

**Verify the GPU is schedulable** (do not proceed until this prints `1`):
```bash
kubectl get nodes -o jsonpath='{.items[*].status.allocatable.nvidia\.com/gpu}{"\n"}'
# → 1
```
Optional end-to-end pod check:
```bash
kubectl run gpu-test --rm -it --restart=Never \
  --image=nvidia/cuda:12.4.1-base-ubuntu22.04 \
  --overrides='{"spec":{"containers":[{"name":"gpu-test","image":"nvidia/cuda:12.4.1-base-ubuntu22.04","args":["nvidia-smi"],"resources":{"limits":{"nvidia.com/gpu":1}}}]}}'
# → nvidia-smi output from inside the pod
```

minikube has now set your kubeconfig context to `minikube`, so the agent will target this
cluster automatically.

---

## 3. Author a tiny-model scenario that fits 8 GB

Upstream ships **no** consumer/single-GPU scenario — its GPU specs assume datacenter cards. You
create a sized-down override. **You don't write a file by hand** — you ask the agent to author it
with `write_and_validate_config(artifact_type="scenario", …)`, which:

- deep-merges your dotted-path overrides onto the repo's stock `defaults.yaml` (a single decode
  deployment — **no** P/D, so nothing multi-GPU is implied),
- SHAPE-validates each knob against the repo's live scenario examples (a typo/stale key fails
  fast, **no file written**, no cluster touched),
- writes the scenario **plus a companion `<name>.spec.yaml`** into the session workspace and
  returns its **`spec_path`** — that's what you feed to `--spec`.

Judgment about *which* knobs lives in `knowledge/vllm_overrides.md`; field defaults live in
`llm-d-benchmark/config/templates/values/defaults.yaml` (read it with
`read_repo_doc`). A **known-good override map for an 8 GB card**:

```jsonc
// write_and_validate_config(artifact_type="scenario", target_filename="single-gpu-8gb.yaml", content = …)
{
  "name": "single-gpu-8gb",
  "model.name":                 "Qwen/Qwen2.5-0.5B-Instruct",
  "model.shortName":            "qwen2.5-0.5b",
  "model.huggingfaceId":        "Qwen/Qwen2.5-0.5B-Instruct",
  "model.maxModelLen":          2048,     // → --max-model-len   (keep small on 8 GB / WSL2)
  "model.gpuMemoryUtilization": 0.8,      // → --gpu-memory-utilization (vLLM reserves this up front)
  "vllmCommon.tensorParallelism": 1,      // → --tensor-parallel-size 1 (single GPU)
  "storage.modelPvc.storageClassName": "standard",  // minikube's default hostpath class
  "storage.modelPvc.size":      "10Gi"    // a 0.5B model is ~1–2 GB; 10Gi is ample
}
```

Notes:
- **Single replica is the default** — keep it. A second replica needs a second GPU.
- **Affinity is optional on single-node minikube** (the only node *is* the GPU node). On a
  multi-node box add `"affinity.enabled": true, "affinity.nodeSelector": "auto"` (auto-detects
  the cluster's GPU label).
- **Qwen2.5 is ungated → no `HF_TOKEN` needed.** If you choose a gated model, set `HF_TOKEN` in
  `.env` and let the agent `provision_hf_secret`.
- **Model size on 8 GB** (start small, climb only if it serves cleanly):

  | Model | Params | Fits 8 GB? | suggested `maxModelLen` |
  |---|---|---|---|
  | `Qwen/Qwen2.5-0.5B-Instruct` | 0.5B | ✅ safe (**start here**) | 2048–4096 |
  | `TinyLlama/TinyLlama-1.1B-Chat-v1.0` | 1.1B | ✅ | 2048 |
  | `Qwen/Qwen2.5-1.5B-Instruct` | 1.5B | ✅ tight (drop util to ~0.7) | 1024–2048 |
  | `Qwen/Qwen2.5-3B-Instruct` | 3B | ⚠️ risky on WSL2 | 512–1024 |
  | 7B and up | ≥7B | ❌ won't fit unquantized | — use `SIMULATE=1` |

  Serve fp16/bf16 to start. FP8 is a per-arch call: the GeForce 40-series (Ada) lacks the
  datacenter-Ada FP8 path so skip it there; 50-series (Blackwell, sm_120) **does** have FP8, but
  still validate fp16/bf16 first.

---

## 4. Hand the cluster to the agent

1. **`.env`:** set `SIMULATE=0` (real execution). Leave `KUBECONFIG` unset — the default
   `~/.kube/config` already points at minikube. (Set `HF_TOKEN` only for a gated model.)

2. **Start the app** and open it:
   ```bash
   uvicorn app.main:app    # http://127.0.0.1:8000
   ```

3. **Drive the real GPU run.** The agent defaults to the `cicd/kind` quickstart, so be explicit
   that you have a real GPU cluster. A working sequence (the agent does each step; mutating ones
   are approval-gated):

   1. *"Probe my environment and advise on accelerators."* → confirm it reports **minikube** and
      **`nvidia.com/gpu`** (real GPU detected).
   2. Let it run its prerequisite installers **when it offers them** — `install.sh` (the
      benchmark framework venv) and, for a guide/modelservice deploy, `install-deps.sh` (the
      helm / helmfile / kustomize / yq client toolchain).
   3. *"Author a single-GPU scenario for `Qwen/Qwen2.5-0.5B-Instruct`"* — give it the override
      map from §3. It returns a `spec_path` in the workspace.
   4. *"`check_capacity` for this model on my GPU."* → real GPU-memory sizing.
   5. **Dry-run gate (required):** *"Plan/dry-run that spec."* → the agent runs
      `execute_llmdbenchmark(subcommand="plan", spec=<spec_path>, flags={"dry_run": true})`. A
      clean dry-run is the acceptance gate before any mutation.
   6. **Deploy + benchmark:** *"Stand it up, smoketest, then run."* → `standup --spec <spec_path>`
      (Approve) → `smoketest` → `run -l inference-perf -w sanity_random.yaml` → it parses the
      **Benchmark Report v0.2** and analyzes it. These are **real vLLM** numbers from your 4060.
   7. *"Tear it down"* when finished.

> The only difference from the kind quickstart is the **`--spec <your workspace spec>`** instead
> of `cicd/kind`. Once the cluster advertises a GPU, the rest of the agent's flow is identical.

---

## 5. Feature coverage — what is real vs simulated on one GPU

Most of the agent's tools never needed a GPU; the GPU upgrade specifically makes the
**deploy / serve / benchmark / observe** loop real. The table is the authoritative checklist for
"check all features."

| Feature / tool | On your RTX 4060 | Note |
|---|---|---|
| `probe_environment`, `advise_accelerators`, `discover_stack` | ✅ **real** | now reports minikube + real `nvidia.com/gpu` |
| `check_capacity` | ✅ **real** | real GPU-memory sizing instead of the CPU floor |
| `list_catalog`, `read_knowledge`, `search_knowledge`, `read_repo_doc` | ✅ always | read-only, GPU-independent |
| `propose_session_plan`, `write_and_validate_config`, `convert_guide_to_scenario`, `set_vllm_flags` | ✅ **real** | authoring + the GPU SessionPlan |
| `execute_llmdbenchmark` standup / smoketest / **run** / teardown | ✅ **real vLLM** | the headline upgrade — real inference on the tiny model |
| `orchestrate_benchmark_run` (K8s Job) | ✅ real | runs as a Job on minikube |
| `locate_and_parse_report`, `analyze_results` (goodput / SLO / Pareto) | ✅ real | over a real report |
| `compare_reports`, `compare_harness_runs` | ✅ real | run twice / two harnesses, then compare |
| `generate_doe_experiment` | ✅ real | each design point is a real run (slower) |
| `observe_run_metrics` | ✅ real | **requires the metrics-server addon** (§2) |
| `export_run_bundle`, `reproduce_run`, `result_history` | ✅ real | provenance + history of real runs |
| `provision_hf_secret`, `cancel_run` | ✅ real | secret only needed for gated models |
| **P/D disaggregation** (`guides/pd-disaggregation`) | 🟡 **simulate only** | needs a prefill **and** a decode pod = ≥ 2 GPUs |
| **Multi-replica EPP / precise-prefix-cache / predicted-latency routing** | 🟡 **simulate only** | routing across replicas is meaningless with 1 replica = 1 GPU |
| **tiered-prefix-cache, wide-ep-lws, workload-autoscaling (scale-up)** | 🟡 **simulate only** | multi-GPU topologies |
| **7B+ / large models** | 🟡 **simulate only** | won't fit 8 GB unquantized |

For every 🟡 row: run it under `SIMULATE=1` to walk the whole workflow and see the result cards
without a cluster, or read the published numbers with `read_repo_doc` on the relevant guide.
**A genuine end-to-end test of the multi-GPU features needs a ≥ 2-GPU cluster** — point the agent
at one with `KUBECONFIG=…` and the same flow applies.

---

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| `docker run --gpus all … nvidia-smi` fails on the host | Fix this before Kubernetes. Update the Windows driver; re-run `nvidia-ctk runtime configure --runtime=docker`; restart Docker. |
| Node never advertises `nvidia.com/gpu` (verify step prints empty) | `minikube addons enable nvidia-device-plugin`; ensure the device-plugin pod runs under the **nvidia runtime** (a common WSL2 trap — it reports 0 GPUs otherwise); `minikube stop && minikube start … --gpus all`. |
| minikube rejects `--gpus all` | Use **`--driver=docker --container-runtime=docker`** (not containerd) and minikube **≥ v1.32.0**. |
| `nvidia-smi` not found inside WSL | Update the **Windows** NVIDIA driver; do **not** apt-install a Linux driver in WSL. |
| Serve pod dies at `standup`/`smoketest` with **"no kernel image is available for execution on the device"** | The vLLM image lacks kernels for your GPU's compute capability — the top risk on a **Blackwell 50-series (sm_120)** card, which needs an image built with **CUDA 12.8+**. Pin a newer vLLM image tag in the scenario, or fall back to `SIMULATE=1`. |
| vLLM **CUDA OOM** at startup | Lower `model.gpuMemoryUtilization` (0.8 → 0.7), shorten `model.maxModelLen`, or drop to `Qwen2.5-0.5B`. WSL2 fragments VRAM, so leave headroom. |
| Serving pod stuck **Pending** | Check the model PVC is **Bound** (storageClass `standard`); confirm the node has GPU capacity free; single-node minikube usually has no GPU taint, so no toleration is needed. |
| `observe_run_metrics` returns nothing | `minikube addons enable metrics-server`; wait for the pod to be Ready. |
| The agent keeps choosing `cicd/kind` | Tell it explicitly: *"I have a real GPU cluster (minikube, `nvidia.com/gpu`). Do not use `cicd/kind`; deploy with the workspace spec I authored."* GPU deploy is a non-default path in the agent today. |

---

## Related

- `knowledge/deploy_path_playbook.md` — how the agent chooses a deploy path (kind/sim vs GPU
  guides). The GPU path is documented there as a non-default ("future") flow; this runbook is the
  hands-on companion for making it real on a small GPU.
- `knowledge/vllm_overrides.md` — the per-knob scenario-override guide (the judgment behind §3).
- `knowledge/capacity.md` / `knowledge/accelerators.yaml` — GPU sizing + accelerator detection.
- `docs/INTERACTIVE_TEST_GUIDE.md` — the by-hand feature walk-through (CPU/kind oriented; this runbook
  is its GPU counterpart).
