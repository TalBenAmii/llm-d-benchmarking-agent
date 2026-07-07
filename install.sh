#!/usr/bin/env bash
# install.sh — one-command install-and-run of the llm-d Benchmarking Assistant as a
# Kubernetes SERVICE on a local `kind` cluster (a laptop POC).
#
# It ORCHESTRATES only: preflight -> build image -> create kind cluster -> load image ->
# DELEGATE the actual deploy to scripts/install_service.sh (the real Helm installer) ->
# verify /healthz + /readyz -> LEAVE THE SERVICE RUNNING and print how to reach/tear it down.
# The Helm logic lives in install_service.sh; this script does not duplicate it. For a
# build+deploy+assert+AUTO-TEARDOWN e2e test instead, use testing/cluster-service-sim/run.sh.
#
# Caveats: the image is a ~1 GB "full-bake" and the FIRST build is slow and needs network
# egress (it clones the CLI + toolchain). On WSL2, run from the Linux filesystem (not /mnt/*)
# and make sure Docker is reachable in this shell. Prereqs (docker/kind/kubectl/helm) are a
# one-time `sudo ./install.sh --prereqs`.
set -euo pipefail

log()  { printf '\033[35m▸\033[0m %s\n' "$*"; }                                    # llm-d purple bullet
step() { printf '\n\033[1;35m━━ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[install] %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31m[install] ERROR: %s\033[0m\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

usage() {
  cat <<'EOF'
install.sh — one-command install-and-run of the llm-d Benchmarking Assistant as a Kubernetes
SERVICE on a local `kind` cluster (laptop POC). It orchestrates preflight -> build image ->
create kind cluster -> load image -> DELEGATE the deploy to scripts/install_service.sh (Helm)
-> verify /healthz + /readyz, then LEAVES THE SERVICE RUNNING and prints how to reach and tear
it down. (For a build+deploy+assert+auto-teardown e2e test instead, use the harness at
testing/cluster-service-sim/run.sh.)

Usage:
  ./install.sh [flags]
  sudo ./install.sh --prereqs        # one-time: install docker + kind + kubectl (+helm)

  --prereqs             install docker+kind+kubectl (+helm) and exit; needs root (sudo)
  --cluster NAME        kind cluster name            (default: bench-agent; env CLUSTER)
  -n, --namespace NS    target namespace             (default: llmd-bench;  env NAMESPACE)
  -r, --release NAME    Helm release name            (default: bench-agent; env RELEASE)
      --image REPO      local image name             (default: llm-d-benchmarking-agent; env IMAGE)
      --tag TAG         image tag                    (default: 0.1.0; env TAG / VERSION)
      --port PORT       local port for the health check / port-forward (default: 8000; env PORT)
      --oauth-token TOKEN  Claude subscription token from `claude setup-token`
                        (default: $CLAUDE_CODE_OAUTH_TOKEN, else the project .env) -> claude-agent-sdk
      --anthropic-key KEY  Anthropic API key fallback
                        (default: $ANTHROPIC_API_KEY, else the project .env) -> anthropic
      --no-build        reuse an existing local image; never build (fails if it is absent)
      --open            after a healthy deploy, port-forward :PORT and open the browser (Ctrl-C to stop)
      --build-timeout SECS  hard cap on the image build (default: 1800)
  -h, --help

Examples:
  sudo ./install.sh --prereqs   # fresh laptop: install docker+kind+kubectl+helm, then re-login
  ./install.sh                  # build, spin up kind, deploy, verify, leave it running
  ./install.sh --no-build       # skip the build; deploy an image already built locally
  ./install.sh --open           # deploy, then open the chat UI in your browser
EOF
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"      # this script lives at the repo root
PROJECT_DIR="$SCRIPT_DIR/llm-d-benchmarking-agent-project"
INSTALLER="$PROJECT_DIR/scripts/install_service.sh"            # the real Helm deployer we delegate to
CHART_DIR="$PROJECT_DIR/deploy/helm/llm-d-benchmarking-agent"

# Inputs — all env-overridable; the flags below win over the environment.
CLUSTER="${CLUSTER:-bench-agent}"
NAMESPACE="${NAMESPACE:-llmd-bench}"
RELEASE="${RELEASE:-bench-agent}"
IMAGE="${IMAGE:-llm-d-benchmarking-agent}"                      # bare LOCAL image name (loaded into kind)
TAG="${TAG:-${VERSION:-0.1.0}}"
PORT="${PORT:-8000}"
OAUTH_TOKEN="${OAUTH_TOKEN:-${CLAUDE_CODE_OAUTH_TOKEN:-}}"
ANTHROPIC_KEY="${ANTHROPIC_KEY:-${ANTHROPIC_API_KEY:-}}"
BUILD_TIMEOUT="${BUILD_TIMEOUT:-1800}"
NO_BUILD=0; PREREQS=0; OPEN=0

# Per-step bounds (seconds) — nothing runs unbounded.
LOAD_TIMEOUT=300
ROLLOUT_TIMEOUT=180
HEALTH_RETRIES=30
HEALTH_INTERVAL=2

# Runtime state (set as we go).
CTX=""; PROVIDER=""; KEYLESS=0; PF_PID=""; DEPLOY=""; SVC=""
TMPDIR="$(mktemp -d)"; BODY_FILE="$TMPDIR/body"

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --prereqs)        PREREQS=1; shift ;;
      --cluster)        CLUSTER="${2:?--cluster needs a value}"; shift 2 ;;
      -n|--namespace)   NAMESPACE="${2:?--namespace needs a value}"; shift 2 ;;
      -r|--release)     RELEASE="${2:?--release needs a value}"; shift 2 ;;
      --image)          IMAGE="${2:?--image needs a value}"; shift 2 ;;
      --tag)            TAG="${2:?--tag needs a value}"; shift 2 ;;
      --port)           PORT="${2:?--port needs a value}"; shift 2 ;;
      --oauth-token)    OAUTH_TOKEN="${2:?--oauth-token needs a value}"; shift 2 ;;
      --anthropic-key)  ANTHROPIC_KEY="${2:?--anthropic-key needs a value}"; shift 2 ;;
      --no-build)       NO_BUILD=1; shift ;;
      --open)           OPEN=1; shift ;;
      --build-timeout)  BUILD_TIMEOUT="${2:?--build-timeout needs a value}"; shift 2 ;;
      -h|--help)        usage; exit 0 ;;
      *) die "unknown option '$1' (try --help)" ;;
    esac
  done
}

# Kills a lingering port-forward and clears the temp dir on any exit; the cluster stays up.
cleanup() {
  local rc=$?
  [[ -n "$PF_PID" ]] && kill "$PF_PID" 2>/dev/null || true
  rm -rf "$TMPDIR" 2>/dev/null || true
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Read KEY=value for $1 from the project .env (pure bash): strip a trailing CR and quotes.
# `|| true` keeps a keyless .env from tripping set -euo pipefail. Always returns 0.
env_fallback() {
  local line val
  [[ -f "$PROJECT_DIR/.env" ]] || return 0
  line="$(grep -E "^[[:space:]]*$1=" "$PROJECT_DIR/.env" 2>/dev/null | tail -n1 || true)"
  [[ -n "$line" ]] || return 0
  val="${line#*=}"; val="${val%$'\r'}"
  val="${val#\"}"; val="${val%\"}"
  val="${val#\'}"; val="${val%\'}"
  printf '%s' "$val"
}

# Sets HTTP_CODE, writes the response body to $BODY_FILE. Never trips set -e (000 on failure).
http_get() {
  HTTP_CODE="$(timeout 15 curl -sS -o "$BODY_FILE" -w '%{http_code}' "http://127.0.0.1:$PORT$1" 2>/dev/null || echo 000)"
}

# --prereqs: the ONLY privileged step. Does not assume passwordless sudo; stops afterwards.
install_prereqs() {
  [[ $EUID -eq 0 ]] || die "Installing prerequisites needs root. Re-run:  sudo ./install.sh --prereqs"
  step "Installing prerequisites (docker + kind + kubectl)"
  bash "$PROJECT_DIR/scripts/install_prereqs.sh" --all
  if have helm; then
    log "helm already present — skipping."
  else
    step "Installing helm"
    curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
  fi
  step "Prerequisites installed."
  log "If docker was just installed, your group membership needs a fresh login:"
  log "    start a new shell (or run 'newgrp docker'), then run:  ./install.sh"
  log "Run ./install.sh as your NORMAL user (not root)."
}

preflight() {
  local missing=() t
  for t in docker kind kubectl helm curl timeout; do have "$t" || missing+=("$t"); done
  if [[ ${#missing[@]} -gt 0 ]]; then
    die "Missing prerequisites: ${missing[*]}
    Install them (one time, needs sudo):
        sudo ./install.sh --prereqs"
  fi
  docker info >/dev/null 2>&1 || die "docker is installed but not usable in this shell (the 'docker' group isn't active) — start a new shell or run 'newgrp docker', then re-run. If you just ran --prereqs, log out and back in."
  [[ -f "$INSTALLER" ]] || die "service installer not found at $INSTALLER (repo layout unexpected)."
  [[ -d "$CHART_DIR" ]] || die "Helm chart not found at $CHART_DIR (repo layout unexpected)."
}

# Late-bind auth from the project .env if unset (never overrides an explicit flag/env), then
# log the provider the deploy will select. The judgment lives in install_service.sh + the chart.
resolve_auth() {
  if [[ -z "$OAUTH_TOKEN" ]]; then
    OAUTH_TOKEN="$(env_fallback CLAUDE_CODE_OAUTH_TOKEN)"
    [[ -n "$OAUTH_TOKEN" ]] && log "Picked up CLAUDE_CODE_OAUTH_TOKEN from $PROJECT_DIR/.env"
  fi
  if [[ -z "$ANTHROPIC_KEY" ]]; then
    ANTHROPIC_KEY="$(env_fallback ANTHROPIC_API_KEY)"
    [[ -n "$ANTHROPIC_KEY" ]] && log "Picked up ANTHROPIC_API_KEY from $PROJECT_DIR/.env"
  fi
  if [[ -n "$OAUTH_TOKEN" ]]; then
    PROVIDER="claude-agent-sdk"; log "Auth: Claude subscription token -> provider $PROVIDER."
  elif [[ -n "$ANTHROPIC_KEY" ]]; then
    PROVIDER="anthropic"; log "Auth: Anthropic API key -> provider $PROVIDER."
  else
    PROVIDER="claude-agent-sdk"; KEYLESS=1
    warn "no Claude token / API key found — deploying anyway; /healthz + /readyz will be green but chat is disabled. Add CLAUDE_CODE_OAUTH_TOKEN to $PROJECT_DIR/.env (via 'claude setup-token') and re-run to enable chat."
  fi
}

build_image() {
  if [[ "$NO_BUILD" == 1 ]]; then
    docker image inspect "$IMAGE:$TAG" >/dev/null 2>&1 \
      || die "--no-build given but $IMAGE:$TAG is not present locally. Drop --no-build to build it."
    log "Reusing existing image $IMAGE:$TAG (--no-build)."
    return 0
  fi
  if docker image inspect "$IMAGE:$TAG" >/dev/null 2>&1; then
    log "Reusing existing image $IMAGE:$TAG (delete it with 'docker rmi $IMAGE:$TAG' or bump --tag to force a rebuild)."
    return 0
  fi
  step "Building the agent image $IMAGE:$TAG (~1GB full-bake; first build is slow + needs network egress)"
  if have make && [[ -f "$PROJECT_DIR/Makefile" ]]; then
    timeout --kill-after=30s "$BUILD_TIMEOUT" make -C "$PROJECT_DIR" image IMAGE="$IMAGE" VERSION="$TAG" \
      || die "image build failed or timed out after ${BUILD_TIMEOUT}s."
  else
    timeout --kill-after=30s "$BUILD_TIMEOUT" docker build -t "$IMAGE:$TAG" "$PROJECT_DIR" \
      || die "image build failed or timed out after ${BUILD_TIMEOUT}s."
  fi
  docker image inspect "$IMAGE:$TAG" >/dev/null 2>&1 || die "build reported success but $IMAGE:$TAG is still absent."
}

ensure_cluster() {
  if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
    log "Reusing existing kind cluster '$CLUSTER'."
  else
    step "Creating kind cluster '$CLUSTER'"
    timeout 300 kind create cluster --name "$CLUSTER" || die "kind create cluster timed out/failed."
  fi
  timeout 20 kubectl --context "$CTX" cluster-info >/dev/null 2>&1 || die "kind context '$CTX' is not reachable."
}

load_image() {
  step "Loading $IMAGE:$TAG into kind cluster '$CLUSTER'"
  timeout "$LOAD_TIMEOUT" kind load docker-image "$IMAGE:$TAG" --name "$CLUSTER" \
    || die "kind load docker-image failed/timed out after ${LOAD_TIMEOUT}s."
}

# DELEGATE the deploy to the real installer — kind-appropriate (locally-loaded image,
# pullPolicy Never, kind context targeted explicitly). Provider selection is the installer's job.
deploy() {
  local auth_args=()
  if   [[ -n "$OAUTH_TOKEN" ]];   then auth_args=(--oauth-token "$OAUTH_TOKEN")
  elif [[ -n "$ANTHROPIC_KEY" ]]; then auth_args=(--anthropic-key "$ANTHROPIC_KEY"); fi
  step "Deploying release '$RELEASE' into namespace '$NAMESPACE' (provider $PROVIDER)"
  bash "$INSTALLER" \
    -n "$NAMESPACE" -r "$RELEASE" \
    --image "$IMAGE" --tag "$TAG" \
    --image-pull-policy Never \
    --context "$CTX" \
    --timeout 5m \
    ${auth_args[@]+"${auth_args[@]}"} \
    || die "install_service.sh deploy failed — see the output above."
}

# Derive the deploy + service names from the release label, then wait for the rollout.
wait_ready() {
  DEPLOY="$(timeout 20 kubectl --context "$CTX" -n "$NAMESPACE" get deploy -l app.kubernetes.io/instance="$RELEASE" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  SVC="$(timeout 20 kubectl --context "$CTX" -n "$NAMESPACE" get svc -l app.kubernetes.io/instance="$RELEASE" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  [[ -n "$DEPLOY" ]] || die "no Deployment found for release '$RELEASE' in namespace '$NAMESPACE'."
  [[ -n "$SVC" ]]    || die "no Service found for release '$RELEASE' in namespace '$NAMESPACE'."
  log "deployment: $DEPLOY   service: $SVC"
  step "Waiting for rollout (bounded ${ROLLOUT_TIMEOUT}s)"
  timeout "$((ROLLOUT_TIMEOUT + 30))" kubectl --context "$CTX" -n "$NAMESPACE" rollout status "deploy/$DEPLOY" --timeout="${ROLLOUT_TIMEOUT}s" \
    || die "deployment '$DEPLOY' did not become Ready within ${ROLLOUT_TIMEOUT}s. Inspect: kubectl -n $NAMESPACE logs deploy/$DEPLOY"
}

# Temporary port-forward + bounded curl-retry; assert /healthz and /readyz, then drop the forward.
health_check() {
  step "Health check on http://127.0.0.1:$PORT"
  kubectl --context "$CTX" -n "$NAMESPACE" port-forward "svc/$SVC" "$PORT:8000" >"$TMPDIR/pf.log" 2>&1 &
  PF_PID=$!
  sleep 1
  kill -0 "$PF_PID" 2>/dev/null || { warn "port-forward exited immediately:"; cat "$TMPDIR/pf.log" >&2; die "port-forward failed to start."; }
  local reachable=0 i
  for ((i=1; i<=HEALTH_RETRIES; i++)); do
    http_get /healthz
    if [[ "$HTTP_CODE" == 200 ]]; then reachable=1; break; fi
    kill -0 "$PF_PID" 2>/dev/null || { warn "port-forward died mid-wait:"; cat "$TMPDIR/pf.log" >&2; break; }
    sleep "$HEALTH_INTERVAL"
  done
  [[ "$reachable" == 1 ]] || die "app never answered /healthz on :$PORT within $((HEALTH_RETRIES*HEALTH_INTERVAL))s. Inspect: kubectl -n $NAMESPACE logs deploy/$DEPLOY"
  http_get /healthz
  { [[ "$HTTP_CODE" == 200 ]] && grep -Eq '"ok"[[:space:]]*:[[:space:]]*true' "$BODY_FILE"; } \
    || die "/healthz did not return 200 with ok:true (got $HTTP_CODE). Inspect: kubectl -n $NAMESPACE logs deploy/$DEPLOY"
  http_get /readyz
  [[ "$HTTP_CODE" == 200 ]] || die "/readyz did not return 200 (got $HTTP_CODE). Inspect: kubectl -n $NAMESPACE logs deploy/$DEPLOY"
  log "health OK — /healthz ok:true, /readyz green."
  kill "$PF_PID" 2>/dev/null || true
  wait "$PF_PID" 2>/dev/null || true
  PF_PID=""
}

report() {
  step "Service is RUNNING on kind cluster '$CLUSTER'."
  log "Provider: $PROVIDER"
  [[ "$KEYLESS" == 1 ]] && warn "Chat is DISABLED (no token/key). Add CLAUDE_CODE_OAUTH_TOKEN to $PROJECT_DIR/.env and re-run."
  log "Reach the UI:"
  log "    kubectl -n $NAMESPACE port-forward svc/$SVC 8000:8000"
  log "    # then open http://localhost:8000   (or re-run:  ./install.sh --open)"
  log "Tear down:"
  log "    kind delete cluster --name $CLUSTER"
  log "Full e2e test (build+deploy+assert+auto-teardown):"
  log "    bash llm-d-benchmarking-agent-project/testing/cluster-service-sim/run.sh"
}

# --open: foreground port-forward + best-effort browser open, blocking until Ctrl-C.
open_ui() {
  step "Opening the UI — port-forward svc/$SVC $PORT:8000 (foreground)"
  kubectl --context "$CTX" -n "$NAMESPACE" port-forward "svc/$SVC" "$PORT:8000" >"$TMPDIR/pf.log" 2>&1 &
  PF_PID=$!
  sleep 1
  kill -0 "$PF_PID" 2>/dev/null || { cat "$TMPDIR/pf.log" >&2; die "port-forward failed to start."; }
  local url="http://localhost:$PORT"
  if   have xdg-open; then xdg-open "$url" >/dev/null 2>&1 || true
  elif have open;     then open "$url" >/dev/null 2>&1 || true
  else log "Open this in your browser: $url"; fi
  log "Forwarding $url -> svc/$SVC. Ctrl-C stops the port-forward; the service KEEPS RUNNING on the cluster."
  wait "$PF_PID" || true
  PF_PID=""
}

main() {
  parse_args "$@"
  [[ "$PREREQS" == 1 ]] && { install_prereqs; exit 0; }
  CTX="kind-$CLUSTER"
  preflight
  resolve_auth
  build_image
  ensure_cluster
  load_image
  deploy
  wait_ready
  health_check
  report
  if [[ "$OPEN" == 1 ]]; then
    open_ui
  fi
}

main "$@"
