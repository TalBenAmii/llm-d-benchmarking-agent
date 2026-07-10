#!/usr/bin/env bash
# install_service.sh — deploy the llm-d Benchmarking Assistant as a Kubernetes SERVICE.
#
# In-cluster / service installer (contrast: install_local.sh bootstraps a laptop/dev box). It
# deploys the pre-built, PUBLISHED agent image into an existing cluster via the project's Helm
# chart, which ships a namespace-scoped ServiceAccount + least-privilege RBAC. LLM/HF secrets come
# from a Kubernetes Secret (chart-managed) — never baked into the image. Assumes kubectl + helm are
# installed and a cluster is reachable (current kube-context, or --kubeconfig/--context).
#
# For local/kind or air-gapped use, --build builds the image here (docker build) and defaults its
# pullPolicy to Never — load it onto the nodes yourself (e.g. `kind load docker-image`).
#
# LLM auth defaults to the Claude Agent SDK on a Claude subscription (Max/Pro): pass the token from
# `claude setup-token` via --oauth-token / $CLAUDE_CODE_OAUTH_TOKEN. Fallback: --anthropic-key /
# $ANTHROPIC_API_KEY selects the metered Anthropic API. Neither given → deploys with the SDK default
# and no auth secret; live chat stays disabled until a token/key is set.
#
# Usage:
#   ./scripts/install/install_service.sh [flags]
#
#   -n, --namespace NS         target namespace (default: llmd-bench; created if absent)
#   -r, --release NAME         Helm release name (default: bench-agent)
#       --image REPO           image repository (default: ghcr.io/llm-d/llm-d-benchmarking-agent)
#       --tag TAG              image tag / VERSION (default: 0.1.0)
#       --image-pull-policy P  Always|IfNotPresent|Never (default: IfNotPresent)
#       --build                docker-build the image locally and use it (air-gapped/dev; pullPolicy→Never)
#       --oauth-token TOKEN    Claude subscription token from `claude setup-token`
#                              (default: $CLAUDE_CODE_OAUTH_TOKEN); selects the claude-agent-sdk path
#       --anthropic-key KEY    Anthropic API key fallback (default: $ANTHROPIC_API_KEY); used only
#                              when no --oauth-token is given
#       --orchestrator-image IMG  image for in-cluster orchestrated benchmark Jobs (config.orchestratorImage)
#       --kubeconfig PATH      kubeconfig file (default: $KUBECONFIG / ~/.kube/config)
#       --context NAME         kube-context to use (default: current-context)
#       --timeout DUR          helm --wait timeout (default: 5m)
#       --dry-run              render + validate via `helm --dry-run`; apply nothing
#   -h, --help
#
# After a successful install it prints the port-forward command to reach the chat UI on :8000.
set -euo pipefail

log()  { printf '\033[35m▸\033[0m %s\n' "$*"; }                                    # llm-d purple bullet
step() { printf '\n\033[1;35m━━ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[install-service] %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31m[install-service] ERROR: %s\033[0m\n' "$*" >&2; exit 1; }

usage() { sed -n '2,38p' "$0" | sed 's/^# \{0,1\}//'; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # this script lives in scripts/install/
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
CHART_DIR="$PROJECT_DIR/deploy/helm/llm-d-benchmarking-agent"
CHART_NAME="llm-d-benchmarking-agent"                        # chart .name — drives the Service fullname

# Deploy inputs — all env-overridable (a test adapter may pre-seed these before sourcing/calling);
# the flags below win over the environment.
NAMESPACE="${NAMESPACE:-llmd-bench}"
RELEASE="${RELEASE:-bench-agent}"
IMAGE="${IMAGE:-ghcr.io/llm-d/llm-d-benchmarking-agent}"
VERSION="${VERSION:-0.1.0}"
TAG="${TAG:-$VERSION}"
IMAGE_PULL_POLICY="${IMAGE_PULL_POLICY:-IfNotPresent}"
PULL_POLICY_SET=0
BUILD="${BUILD:-0}"
OAUTH_TOKEN="${OAUTH_TOKEN:-${CLAUDE_CODE_OAUTH_TOKEN:-}}"
ANTHROPIC_KEY="${ANTHROPIC_KEY:-${ANTHROPIC_API_KEY:-}}"
ORCHESTRATOR_IMAGE="${ORCHESTRATOR_IMAGE:-}"
KUBECONFIG_PATH="${KUBECONFIG_PATH:-}"
KUBE_CONTEXT="${KUBE_CONTEXT:-}"
TIMEOUT="${TIMEOUT:-5m}"
DRY_RUN="${DRY_RUN:-0}"
KUBECTL_CTX=(); HELM_CTX=()   # --kubeconfig/--context flags, built after parsing

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -n|--namespace)       NAMESPACE="${2:?--namespace needs a value}"; shift 2 ;;
      -r|--release)         RELEASE="${2:?--release needs a value}"; shift 2 ;;
      --image)              IMAGE="${2:?--image needs a value}"; shift 2 ;;
      --tag)                TAG="${2:?--tag needs a value}"; shift 2 ;;
      --image-pull-policy)  IMAGE_PULL_POLICY="${2:?--image-pull-policy needs a value}"; PULL_POLICY_SET=1; shift 2 ;;
      --build)              BUILD=1; shift ;;
      --oauth-token)        OAUTH_TOKEN="${2:?--oauth-token needs a value}"; shift 2 ;;
      --anthropic-key)      ANTHROPIC_KEY="${2:?--anthropic-key needs a value}"; shift 2 ;;
      --orchestrator-image) ORCHESTRATOR_IMAGE="${2:?--orchestrator-image needs a value}"; shift 2 ;;
      --kubeconfig)         KUBECONFIG_PATH="${2:?--kubeconfig needs a value}"; shift 2 ;;
      --context)            KUBE_CONTEXT="${2:?--context needs a value}"; shift 2 ;;
      --timeout)            TIMEOUT="${2:?--timeout needs a value}"; shift 2 ;;
      --dry-run)            DRY_RUN=1; shift ;;
      -h|--help)            usage; exit 0 ;;
      *) die "unknown option '$1' (try --help)" ;;
    esac
  done
}

# helm takes --kube-context; kubectl takes --context — otherwise the same kubeconfig/context.
build_ctx_args() {
  if [[ -n "$KUBECONFIG_PATH" ]]; then
    KUBECTL_CTX+=(--kubeconfig "$KUBECONFIG_PATH"); HELM_CTX+=(--kubeconfig "$KUBECONFIG_PATH")
  fi
  if [[ -n "$KUBE_CONTEXT" ]]; then
    KUBECTL_CTX+=(--context "$KUBE_CONTEXT"); HELM_CTX+=(--kube-context "$KUBE_CONTEXT")
  fi
}

preflight() {
  command -v kubectl >/dev/null 2>&1 || die "kubectl not found on PATH — install it and re-run."
  command -v helm    >/dev/null 2>&1 || die "helm not found on PATH — install it and re-run."
  helm ${HELM_CTX[@]+"${HELM_CTX[@]}"} version >/dev/null 2>&1 || die "helm is present but not working ('helm version' failed)."
  kubectl ${KUBECTL_CTX[@]+"${KUBECTL_CTX[@]}"} cluster-info >/dev/null 2>&1 \
    || die "cannot reach a Kubernetes cluster ('kubectl cluster-info' failed) — check --kubeconfig/--context and that the cluster is up."
  [[ -d "$CHART_DIR" ]] || die "Helm chart not found at $CHART_DIR."
}

build_image() {
  command -v docker >/dev/null 2>&1 || die "--build needs docker on PATH."
  step "Building the agent image locally: $IMAGE:$TAG"
  docker build -t "$IMAGE:$TAG" "$PROJECT_DIR"
  warn "Built $IMAGE:$TAG locally — make sure it is loaded onto the cluster nodes (e.g. 'kind load docker-image $IMAGE:$TAG') before the pod schedules."
}

# Assembles + runs the Helm upgrade. Value assembly reads the module-level vars so a sourcing
# adapter can override image/tag/pullPolicy (etc.) and call this directly.
deploy_agent() {
  local helm_args=(
    upgrade --install "$RELEASE" "$CHART_DIR"
    --namespace "$NAMESPACE" --create-namespace
    --set "image.repository=$IMAGE"
    --set-string "image.tag=$TAG"
    --set "image.pullPolicy=$IMAGE_PULL_POLICY"
    --wait --timeout "$TIMEOUT"
  )
  # Provider selection: the OAuth token (Claude subscription via `claude setup-token`) is the primary
  # path; an Anthropic API key is the metered fallback; prefer the token when both are set. Neither →
  # the chart's claude-agent-sdk default with no auth secret. --set-string so a token/key's special
  # chars are never parsed as YAML/type coercion.
  # Under --dry-run helm renders the Secret's stringData in cleartext to stdout; mask the secret VALUE
  # with a placeholder so a real token/key can't leak to a shared/CI terminal. Provider selection and
  # arg structure stay identical, so the rendered manifest is still validated.
  local oauth_val="$OAUTH_TOKEN" key_val="$ANTHROPIC_KEY"
  if [[ "$DRY_RUN" == 1 ]]; then oauth_val="REDACTED-DRY-RUN"; key_val="REDACTED-DRY-RUN"; fi
  if [[ -n "$OAUTH_TOKEN" ]]; then
    helm_args+=(--set-string "config.llmProvider=claude-agent-sdk" --set-string "secret.claudeCodeOauthToken=$oauth_val")
  elif [[ -n "$ANTHROPIC_KEY" ]]; then
    helm_args+=(--set-string "config.llmProvider=anthropic" --set-string "secret.anthropicApiKey=$key_val")
  fi
  [[ -n "$ORCHESTRATOR_IMAGE" ]] && helm_args+=(--set "config.orchestratorImage=$ORCHESTRATOR_IMAGE")
  [[ "$DRY_RUN" == 1 ]]          && helm_args+=(--dry-run)
  helm ${HELM_CTX[@]+"${HELM_CTX[@]}"} "${helm_args[@]}"
}

print_success() {
  if [[ "$DRY_RUN" == 1 ]]; then log "Dry run complete — manifests validated, nothing applied."; return 0; fi
  # Service fullname mirrors the chart's agent.fullname helper.
  local fullname
  if [[ "$RELEASE" == *"$CHART_NAME"* ]]; then fullname="$RELEASE"; else fullname="${RELEASE}-${CHART_NAME}"; fi
  step "Deployed '$RELEASE' → namespace '$NAMESPACE'."
  log "Reach the chat UI:"
  log "  kubectl -n $NAMESPACE port-forward svc/$fullname 8000:8000"
  log "  # then browse http://localhost:8000"
  log "Live chat auth: the default provider is the Claude Agent SDK on a Claude subscription — get a"
  log "  token via 'claude setup-token' and set secret.claudeCodeOauthToken (or re-run with --oauth-token)."
  log "  API-key fallback: --anthropic-key / secret.anthropicApiKey (config.llmProvider=anthropic)."
  log "Full post-install notes:  helm get notes $RELEASE -n $NAMESPACE"
}

main() {
  trap 'rc=$?; [[ $rc -ne 0 ]] && printf "\n\033[1;31m[install-service] aborted (exit %s).\033[0m See the message above.\n" "$rc" >&2' EXIT
  parse_args "$@"
  # A locally built image usually isn't in any registry — default it to pullPolicy Never (unless
  # the caller set one explicitly, or already changed it away from the IfNotPresent default).
  [[ "$BUILD" == 1 && "$PULL_POLICY_SET" == 0 && "$IMAGE_PULL_POLICY" == "IfNotPresent" ]] && IMAGE_PULL_POLICY="Never"
  build_ctx_args
  preflight
  [[ "$BUILD" == 1 ]] && build_image
  if [[ -n "$OAUTH_TOKEN" && -n "$ANTHROPIC_KEY" ]]; then
    warn "Both an OAuth token and an Anthropic API key given — using the OAuth token (claude-agent-sdk); --anthropic-key / ANTHROPIC_API_KEY is ignored."
  elif [[ -z "$OAUTH_TOKEN" && -z "$ANTHROPIC_KEY" ]]; then
    warn "No OAuth token (--oauth-token / CLAUDE_CODE_OAUTH_TOKEN) or Anthropic API key (--anthropic-key / ANTHROPIC_API_KEY) — deploying with the claude-agent-sdk default and NO auth secret; live chat stays disabled until secret.claudeCodeOauthToken (or an API key) is set. /healthz and a keyless-green /readyz still serve."
  fi
  step "Deploying '$RELEASE' to '$NAMESPACE' (image $IMAGE:$TAG, pullPolicy $IMAGE_PULL_POLICY, timeout $TIMEOUT)"
  deploy_agent
  print_success
  trap - EXIT
}

# Run only when executed directly; a test adapter can `source` this file to reuse deploy_agent.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi
