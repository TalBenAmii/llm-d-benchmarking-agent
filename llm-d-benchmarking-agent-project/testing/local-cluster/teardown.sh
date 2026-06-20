#!/usr/bin/env bash
# Tear down the local mock-GPU cluster — DEBUG-ONLY (see ./README.md).
#
# Usage:
#   ./teardown.sh                 # delete the kind 'llmd-mock' cluster
#   ./teardown.sh --mode kwok     # remove kwok fake nodes + controller from the current cluster
set -euo pipefail

MODE=kind
CLUSTER=llmd-mock
KWOK_VER=v0.6.0
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) MODE="$2"; shift 2 ;;
    --cluster) CLUSTER="$2"; shift 2 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [[ "$MODE" == "kind" ]]; then
  command -v kind >/dev/null 2>&1 || { echo "missing required tool: kind" >&2; exit 1; }
  if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
    echo ">> deleting kind cluster '${CLUSTER}'"
    kind delete cluster --name "$CLUSTER"
  else
    echo "kind cluster '${CLUSTER}' not found — nothing to do"
  fi
elif [[ "$MODE" == "kwok" ]]; then
  echo ">> removing fake GPU nodes + kwok controller from $(kubectl config current-context)"
  kubectl delete -f "${SCRIPT_DIR}/fake-gpu-nodes-kwok.yaml" --ignore-not-found
  kubectl delete -f "https://github.com/kubernetes-sigs/kwok/releases/download/${KWOK_VER}/stage-fast.yaml" --ignore-not-found
  kubectl delete -f "https://github.com/kubernetes-sigs/kwok/releases/download/${KWOK_VER}/kwok.yaml" --ignore-not-found
else
  echo "unknown --mode '${MODE}' (expected: kind | kwok)" >&2
  exit 2
fi
