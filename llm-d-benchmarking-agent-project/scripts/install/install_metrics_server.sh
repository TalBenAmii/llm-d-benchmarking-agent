#!/usr/bin/env bash
# Vetted metrics-server installer for the llm-d benchmarking agent.
#
# The in-cluster metrics-server (the Kubernetes Metrics API, metrics.k8s.io) is what
# `kubectl top` / observe_run_metrics / the live resource sparklines read. It is a
# PER-CLUSTER add-on and is NOT installed by kind, the cicd/kind spec, or any llm-d guide.
# This script installs it on the CURRENT kube-context's cluster and nothing else — it is the
# only metrics-server install the security allowlist lets the agent run. The agent invokes it
# through `run_command`, so it goes through the normal approval gate. Every command here is a
# pinned `kubectl` call; the allowlist grants no raw kubectl apply/patch.
#
# Usage:
#   install_metrics_server.sh                          # install on the current cluster
#   install_metrics_server.sh --kubelet-insecure-tls   # REQUIRED on kind/self-signed kubelets
#   install_metrics_server.sh --version v0.7.2         # pin a specific release
#
# Flags: --kubelet-insecure-tls  --version <vX.Y.Z>  -h|--help
#
# Idempotent: re-running re-applies the same pinned manifest and re-asserts the
# kubelet-insecure-tls arg only when requested and not already present. Needs `kubectl` on PATH
# and a reachable cluster (kubectl current-context). Touches only the kube-system namespace.
set -euo pipefail

MS_VERSION="v0.7.2"          # pinned default; override with --version
INSECURE_TLS=0

usage() { sed -n '2,19p' "$0"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --kubelet-insecure-tls) INSECURE_TLS=1 ;;
    --version)              MS_VERSION="${2:?--version needs a value}"; shift ;;
    -h|--help)              usage; exit 0 ;;
    *) echo "[install_metrics_server] unknown flag: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done

log()  { printf '\033[1;32m[install_metrics_server]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[install_metrics_server]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[install_metrics_server] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

command -v kubectl >/dev/null 2>&1 \
  || die "kubectl not found on PATH (install it first, e.g. install_prereqs.sh --kubectl)."
kubectl cluster-info >/dev/null 2>&1 \
  || die "no reachable cluster for the current kube-context — create/select a cluster first."

MANIFEST="https://github.com/kubernetes-sigs/metrics-server/releases/download/${MS_VERSION}/components.yaml"

if kubectl get deployment metrics-server -n kube-system >/dev/null 2>&1; then
  log "metrics-server already present in kube-system — re-applying ${MS_VERSION} (idempotent)."
else
  log "Installing metrics-server ${MS_VERSION}…"
fi

log "Applying ${MANIFEST}"
kubectl apply -f "$MANIFEST"

# kind (and any cluster with self-signed kubelet serving certs) needs --kubelet-insecure-tls,
# or the metrics-server pod fails its TLS handshake to the kubelet and never becomes Ready.
# `kubectl apply` above resets args to the manifest's, so this re-asserts the arg every run.
if [[ "$INSECURE_TLS" == 1 ]]; then
  if kubectl get deployment metrics-server -n kube-system \
       -o jsonpath='{.spec.template.spec.containers[0].args}' 2>/dev/null \
       | grep -q -- '--kubelet-insecure-tls'; then
    log "--kubelet-insecure-tls already set — skipping patch."
  else
    log "Patching deployment with --kubelet-insecure-tls (kind/self-signed kubelets)…"
    kubectl patch deployment metrics-server -n kube-system --type=json \
      -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
  fi
fi

log "Waiting for metrics-server to become Available (up to 180s)…"
kubectl rollout status deployment/metrics-server -n kube-system --timeout=180s \
  || die "metrics-server did not become ready. On kind, re-run with --kubelet-insecure-tls."

# Verify the Metrics API actually serves data — the whole point of installing it.
log "Verifying the Metrics API responds (kubectl top nodes)…"
if kubectl top nodes >/dev/null 2>&1; then
  log "metrics-server is up — 'kubectl top' / live resource stats will now work."
else
  warn "metrics-server is Ready but 'kubectl top' did not return data yet; the Metrics API can"
  warn "take ~30-60s to populate after rollout. Try observe_run_metrics again shortly."
  warn "If it stays empty on kind, make sure --kubelet-insecure-tls was passed."
fi
