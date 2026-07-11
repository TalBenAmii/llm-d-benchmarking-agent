#!/usr/bin/env bash
# Stand up the local mock-GPU cluster — DEBUG-ONLY (see ./README.md).
#
# This is host-side test infrastructure; it is NOT driven by the agent and NEVER ships in the
# product image (.dockerignore excludes harnesses/, enforced by tests/test_product_boundary.py).
#
# Two modes:
#   --mode kind   (default)  Real kind cluster; every node is PATCHED to advertise fake
#                            `nvidia.com/gpu` via the upstream node-status extended-resource
#                            mechanism. GPU-requesting pods schedule onto it AND really run (use
#                            the CPU sim engine) → you get a real BR-v0.2 report. Best for
#                            exercising multi-replica topologies + producing real (sim-valued)
#                            results.
#                            • Multi-node by default (cross-node anti-affinity / topology spread).
#                            • --single-node : 1-node cluster. REQUIRED on WSL2, where multi-node
#                              kind worker kubelets fail to join (cgroup limitation). A single node
#                              still proves GPU-resource scheduling end-to-end; for cross-node
#                              PLACEMENT on WSL2 use --mode kwok instead.
#   --mode kwok              kwok controller + fake GPU nodes (no kubelet; pods are faked). Best
#                            for SCHEDULING / fan-out at scale (high orchestrate_sweep
#                            max_parallel, autoscaling) and multi-node placement on WSL2 — but
#                            pods don't really run, so no benchmark report.
#
# Usage:
#   ./setup.sh                          # kind mode, multi-node, 4 fake GPUs each
#   ./setup.sh --single-node            # 1-node cluster (use this on WSL2)
#   ./setup.sh --mode kind --gpus 8     # 8 fake GPUs per node
#   ./setup.sh --mode kwok              # fake-node fleet for scheduling tests
set -euo pipefail

MODE=kind
GPUS_PER_NODE=4
CLUSTER=llmd-mock
SINGLE_NODE=0
GPU_PRODUCT=NVIDIA-A100-SXM4-80GB
KWOK_VER=v0.6.0
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)  MODE="$2"; shift 2 ;;
    --gpus)  GPUS_PER_NODE="$2"; shift 2 ;;
    --cluster) CLUSTER="$2"; shift 2 ;;
    --single-node) SINGLE_NODE=1; shift ;;
    -h|--help) sed -n '2,/^set /p' "$0" | sed '$d'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing required tool: $1" >&2; exit 1; }; }
need kubectl

# Advertise a fake extended resource (nvidia.com/gpu) on a real node via the node /status
# subresource. kubelet leaves resources it does not manage alone (no NVIDIA device plugin is
# running here), so the capacity sticks. ~1 is the JSON-Pointer escape for the '/' in the name.
patch_fake_gpu() {
  local node="$1" n="$2"
  kubectl patch node "$node" --subresource=status --type=json \
    -p "[{\"op\":\"add\",\"path\":\"/status/capacity/nvidia.com~1gpu\",\"value\":\"${n}\"}]"
  echo "  advertised nvidia.com/gpu=${n} on ${node}"
}

if [[ "$MODE" == "kind" ]]; then
  need kind
  if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
    echo ">> cluster '${CLUSTER}' already exists — reusing"
  elif [[ "$SINGLE_NODE" == 1 ]]; then
    echo ">> creating single-node kind cluster '${CLUSTER}'"
    kind create cluster --name "$CLUSTER"
  else
    echo ">> creating multi-node kind cluster '${CLUSTER}'"
    kind create cluster --config "${SCRIPT_DIR}/kind-multigpu.yaml" || {
      echo "!! multi-node create failed. On WSL2 the worker kubelets cannot join (cgroup" >&2
      echo "   limitation). Retry single-node:  $0 --single-node" >&2
      echo "   ...or for cross-node placement use faked nodes:  $0 --mode kwok" >&2
      exit 1
    }
  fi
  kubectl config use-context "kind-${CLUSTER}"

  echo ">> advertising fake GPUs on schedulable nodes"
  # Patch + label EVERY node. In single-node kind the control-plane is schedulable; in multi-node
  # the tainted control-plane simply won't receive GPU pods (harmless). The product label lets
  # the agent's scheduling.gpu_type_label node-affinity resolve.
  mapfile -t nodes < <(kubectl get nodes -o name | sed 's|node/||')
  for nd in "${nodes[@]}"; do
    patch_fake_gpu "$nd" "$GPUS_PER_NODE"
    kubectl label node "$nd" "nvidia.com/gpu.product=${GPU_PRODUCT}" --overwrite >/dev/null
  done

  echo ">> done. ${#nodes[@]} node(s), ${GPUS_PER_NODE} fake GPU(s) each."
  echo "   Verify:  kubectl get nodes -o custom-columns=NODE:.metadata.name,GPU:.status.capacity.'nvidia\.com/gpu'"
  echo "   Point the agent at it:  KUBECONFIG stays default (~/.kube/config now -> kind-${CLUSTER})"

elif [[ "$MODE" == "kwok" ]]; then
  echo ">> installing kwok controller (${KWOK_VER}) into the current cluster"
  echo "   current context: $(kubectl config current-context)"
  kubectl apply -f "https://github.com/kubernetes-sigs/kwok/releases/download/${KWOK_VER}/kwok.yaml"
  kubectl apply -f "https://github.com/kubernetes-sigs/kwok/releases/download/${KWOK_VER}/stage-fast.yaml"
  echo ">> applying fake GPU nodes"
  kubectl apply -f "${SCRIPT_DIR}/fake-gpu-nodes-kwok.yaml"
  echo ">> done. Verify:  kubectl get nodes -l type=kwok -o custom-columns=NODE:.metadata.name,GPU:.status.capacity.'nvidia\.com/gpu'"

else
  echo "unknown --mode '${MODE}' (expected: kind | kwok)" >&2
  exit 2
fi
