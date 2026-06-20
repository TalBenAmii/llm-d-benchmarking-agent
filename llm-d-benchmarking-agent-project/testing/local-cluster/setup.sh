#!/usr/bin/env bash
# Stand up the local mock-GPU cluster — DEBUG-ONLY (see ./README.md).
#
# This is host-side test infrastructure; it is NOT driven by the agent and NEVER ships in the
# product image (.dockerignore excludes testing/, enforced by tests/test_product_boundary.py).
#
# Two modes:
#   --mode kind   (default)  Real multi-node kind cluster; the workers are PATCHED to advertise
#                            fake `nvidia.com/gpu` via the upstream node-status extended-resource
#                            mechanism. Pods that request a GPU schedule onto them AND really run
#                            (use the CPU sim engine) → you get a real BR-v0.2 report. Best for
#                            exercising multi-replica topologies + producing real (sim-valued)
#                            results.
#   --mode kwok              kwok controller + fake GPU nodes (no kubelet; pods are faked). Best
#                            for SCHEDULING / fan-out at scale (high orchestrate_sweep
#                            max_parallel, autoscaling) — no real benchmark report.
#
# Usage:
#   ./setup.sh                          # kind mode, 2 workers, 4 fake GPUs each
#   ./setup.sh --mode kind --gpus 8     # 8 fake GPUs per worker
#   ./setup.sh --mode kwok              # fake-node fleet for scheduling tests
set -euo pipefail

MODE=kind
GPUS_PER_NODE=4
CLUSTER=llmd-mock
KWOK_VER=v0.6.0
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)  MODE="$2"; shift 2 ;;
    --gpus)  GPUS_PER_NODE="$2"; shift 2 ;;
    --cluster) CLUSTER="$2"; shift 2 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
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
  echo ">> creating multi-node kind cluster '${CLUSTER}'"
  if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
    echo "   cluster '${CLUSTER}' already exists — reusing"
  else
    kind create cluster --config "${SCRIPT_DIR}/kind-multigpu.yaml"
  fi
  kubectl config use-context "kind-${CLUSTER}"

  echo ">> advertising fake GPUs on worker nodes"
  # Worker nodes are everything that is not the control-plane.
  mapfile -t workers < <(kubectl get nodes \
    -l '!node-role.kubernetes.io/control-plane' -o name | sed 's|node/||')
  for w in "${workers[@]}"; do patch_fake_gpu "$w" "$GPUS_PER_NODE"; done

  echo ">> done. ${#workers[@]} worker(s), ${GPUS_PER_NODE} fake GPU(s) each."
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
