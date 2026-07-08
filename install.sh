#!/usr/bin/env bash
# install.sh — one-command install-and-run of the llm-d Benchmarking Assistant as a
# Kubernetes SERVICE on a local `kind` cluster (a laptop POC).
#
# It ORCHESTRATES only: (curl-bootstrap self-clone) -> preflight (auto-installs any missing
# docker/kind/kubectl/helm via sudo) -> build image -> create kind cluster -> load image ->
# DELEGATE the actual deploy to scripts/install_service.sh (the real Helm installer) ->
# verify /healthz + /readyz -> LEAVE THE SERVICE RUNNING and (by default, on a terminal) open the UI.
# The Helm logic lives in install_service.sh; this script does not duplicate it. For a
# build+deploy+assert+AUTO-TEARDOWN e2e test instead, use testing/cluster-service-sim/run.sh.
#
# Caveats: the image is a ~1 GB "full-bake" and the FIRST build is slow and needs network
# egress (it clones the CLI + toolchain). On WSL2, run from the Linux filesystem (not /mnt/*).
# Missing prereqs (docker/kind/kubectl/helm) are auto-installed during preflight via interactive
# sudo. Run it straight from GitHub:
#   bash <(curl -fsSL https://raw.githubusercontent.com/TalBenAmii/llm-d-benchmarking-agent/main/install.sh)
set -euo pipefail

log()  { printf '\033[35m▸\033[0m %s\n' "$*"; }                                    # llm-d purple bullet
step() { printf '\n\033[1;35m━━ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[install] %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31m[install] ERROR: %s\033[0m\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

usage() {
  cat <<'EOF'
install.sh — one-command install-and-run of the llm-d Benchmarking Assistant as a Kubernetes
SERVICE on a local `kind` cluster (laptop POC). Run it straight from GitHub with the curl one-liner
below, or clone the repo and run ./install.sh. It orchestrates: fetch (curl-bootstrap self-clone) ->
preflight (auto-installs any missing docker/kind/kubectl/helm via interactive sudo) -> build image ->
create kind cluster -> load image -> DELEGATE the deploy to scripts/install_service.sh (Helm) ->
verify /healthz + /readyz. If no Claude auth is configured it checks whether you're signed in to the
Claude app (offering to sign in + mint a subscription token if not), then LEAVES THE SERVICE RUNNING
and (by default, on a terminal) opens the chat UI in your browser.
(For a build+deploy+assert+auto-teardown e2e test instead, use testing/cluster-service-sim/run.sh.)

Usage:
  bash <(curl -fsSL https://raw.githubusercontent.com/TalBenAmii/llm-d-benchmarking-agent/main/install.sh)
  ./install.sh [flags]

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
      --no-open         deploy and leave running, but don't port-forward / open a browser
      --open            open the UI even when stdout isn't a terminal (default: open on a terminal)
      --build-timeout SECS  hard cap on the image build (default: 1800)
  -h, --help

Prerequisites: any missing docker/kind/kubectl/helm are installed automatically during preflight via
interactive sudo (you'll be prompted for your password; Ctrl-C to abort and install them manually). If
Docker was just installed, its group membership only activates on a new login — log out/in (or run
'newgrp docker') once, then re-run.

Examples:
  bash <(curl -fsSL https://raw.githubusercontent.com/TalBenAmii/llm-d-benchmarking-agent/main/install.sh)
                                # from scratch: clone into ~/llm-d-benchmarking-agent, install, and run
  ./install.sh                  # in a checkout: build, spin up kind, deploy, verify, open the UI
  ./install.sh --no-build       # skip the build; deploy an image already built locally
  ./install.sh --no-open        # deploy and leave running, but don't open a browser
EOF
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" 2>/dev/null && pwd || true)"  # empty under `bash <(curl …)`
PROJECT_DIR="$SCRIPT_DIR/llm-d-benchmarking-agent-project"
INSTALLER="$PROJECT_DIR/scripts/install_service.sh"            # the real Helm deployer we delegate to
CHART_DIR="$PROJECT_DIR/deploy/helm/llm-d-benchmarking-agent"
INSTALL_DIR="${INSTALL_DIR:-$HOME/llm-d-benchmarking-agent}"   # curl-bootstrap clone target (see bootstrap_if_curl)

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
NO_BUILD=0; OPEN=0; NO_OPEN=0

# Per-step bounds (seconds) — nothing runs unbounded.
LOAD_TIMEOUT=300
ROLLOUT_TIMEOUT=180
HEALTH_RETRIES=30
HEALTH_INTERVAL=2

# Runtime state (set as we go).
CTX=""; PROVIDER=""; KEYLESS=0; PF_PID=""; DEPLOY=""; SVC=""
CLAUDE_CMD=()   # argv to invoke the `claude` CLI as the human user (see resolve_claude_cmd)
CLAUDE_HOME=""  # that user's home dir — where ~/.claude/.credentials.json lives (set by resolve_claude_cmd)
TMPDIR="$(mktemp -d)"; BODY_FILE="$TMPDIR/body"

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
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
      --no-open)        NO_OPEN=1; shift ;;
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

# Curl-bootstrap (mirrors scripts/install_local.sh): this file also runs via `bash <(curl … install.sh)`,
# where it is NOT inside a checkout (no sibling project dir). In that case clone the repo into INSTALL_DIR
# and re-exec the on-disk copy so every path below resolves. A real checkout (marker file present) is a no-op.
bootstrap_if_curl() {
  [[ -f "$PROJECT_DIR/pyproject.toml" ]] && return 0   # already a real checkout — nothing to do
  [[ "${_AGENT_BOOTSTRAPPED:-0}" == 1 ]] && die "project still not found after cloning (bootstrap loop) — check $INSTALL_DIR."
  have git  || die "git is required to fetch the repo — install git and re-run."
  have curl || die "curl is required to fetch the repo — install curl and re-run."
  if [[ -d "$INSTALL_DIR/.git" ]]; then
    # Deliberate: an existing checkout is reused as-is (no `git pull`) so a local .env / uncommitted work isn't clobbered.
    log "Using existing checkout at $INSTALL_DIR"
  else
    step "Fetching llm-d-benchmarking-agent → $INSTALL_DIR"
    git clone "https://github.com/TalBenAmii/llm-d-benchmarking-agent" "$INSTALL_DIR" \
      || die "git clone failed (no network?) — clone it yourself, then run $INSTALL_DIR/install.sh."
  fi
  [[ -f "$INSTALL_DIR/install.sh" ]] || die "$INSTALL_DIR/install.sh missing after clone (unexpected repo layout)."
  export _AGENT_BOOTSTRAPPED=1
  exec bash "$INSTALL_DIR/install.sh" "$@"   # re-run the on-disk copy so BASH_SOURCE paths resolve
}

# Auto-install any missing docker/kind/kubectl/helm via INTERACTIVE sudo (this user is NOT assumed to
# have passwordless sudo — the single password prompt happens at the `sudo` below). docker/kind/kubectl
# go through the vetted installer run under sudo (its root-check passes, its `sudo -n` calls no-op);
# helm (not covered there) via the upstream get-helm-3.
ensure_prereqs() {
  local tools=(docker kind kubectl helm) missing=() t docker_was_missing=0
  for t in "${tools[@]}"; do have "$t" || missing+=("$t"); done
  have docker || docker_was_missing=1

  if [[ ${#missing[@]} -gt 0 ]]; then
    warn "Missing: ${missing[*]} — installing them now; you'll be prompted for your sudo password. Ctrl-C to abort and install them manually."
    step "Installing prerequisites (docker/kind/kubectl) via sudo"
    sudo bash "$PROJECT_DIR/scripts/install_prereqs.sh" --all \
      || die "prerequisite install failed (see above). Install docker+kind+kubectl manually (scripts/install_prereqs.sh) and re-run."
    if ! have helm; then
      step "Installing helm via sudo"
      curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | sudo bash \
        || die "helm install failed — install helm manually (https://helm.sh/docs/intro/install/) and re-run."
    fi
    missing=(); for t in "${tools[@]}"; do have "$t" || missing+=("$t"); done
    [[ ${#missing[@]} -eq 0 ]] || die "still missing after install: ${missing[*]} — install them manually and re-run."
  fi

  # docker present but unusable is almost always the just-added 'docker' group not yet active in this
  # shell. If WE just installed docker that's an expected first-run stop (exit 0), not an error.
  if ! docker info >/dev/null 2>&1; then
    if [[ "$docker_was_missing" == 1 ]]; then
      step "Docker installed, but its group membership only activates on a new login."
      log  "Log out and back in (or run: newgrp docker), then re-run:  ./install.sh"
      exit 0
    fi
    die "docker is installed but not usable in this shell (the 'docker' group isn't active, or the daemon isn't running) — start a new shell or run 'newgrp docker', then re-run."
  fi
}

preflight() {
  # Hard requirements we never auto-install: curl (used here, by get-helm-3, and by the health check)
  # and timeout (bounds every step). git is needed only by the curl-bootstrap path (bootstrap_if_curl).
  local t
  for t in curl timeout; do have "$t" || die "required tool '$t' is missing — install it and re-run."; done
  ensure_prereqs   # docker/kind/kubectl/helm — auto-installed via interactive sudo if any are missing
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

# Resolve how to invoke the `claude` CLI *as the human user* into CLAUDE_CMD (+ that user's home into
# CLAUDE_HOME, where ~/.claude/.credentials.json lives), even when install.sh runs under sudo/root —
# Claude's login + credentials live in that user's home, not root's, and its bin (~/.local/bin/claude)
# is usually off root's PATH. Under sudo we drop back to $SUDO_USER (root → user needs no password);
# otherwise we probe PATH then ~/.local/bin. Returns 1 if none works.
resolve_claude_cmd() {
  local bin uhome
  if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != root ]]; then
    uhome="$(getent passwd "$SUDO_USER" 2>/dev/null | cut -d: -f6)"
    for bin in "$uhome/.local/bin/claude" claude; do
      if sudo -u "$SUDO_USER" -H -- "$bin" --version >/dev/null 2>&1; then
        CLAUDE_CMD=(sudo -u "$SUDO_USER" -H -- "$bin"); CLAUDE_HOME="$uhome"; return 0
      fi
    done
    return 1
  fi
  for bin in claude "$HOME/.local/bin/claude"; do
    command -v "$bin" >/dev/null 2>&1 && { CLAUDE_CMD=("$bin"); CLAUDE_HOME="$HOME"; return 0; }
  done
  return 1
}

# Signed in to the Claude app? `auth status --json` → {"loggedIn": true, ...}.
claude_logged_in() {
  "${CLAUDE_CMD[@]}" auth status --json 2>/dev/null | grep -q '"loggedIn"[[:space:]]*:[[:space:]]*true'
}

# Read the subscription LOGIN token from ~/.claude/.credentials.json (of the resolved user) into
# REUSE_TOKEN, and the whole hours until it expires into REUSE_TOKEN_H. Returns 0 only when a token is
# present and still valid ≥5 min out — the CLI keeps this token fresh, so when signed in it usually is.
# Pure grep/date, no jq/python dependency. Note: this token is SHORT-LIVED and the headless pod cannot
# refresh it (caller warns + does NOT persist it, so a re-run re-reads a freshly-refreshed one).
read_login_token() {
  local cred="$CLAUDE_HOME/.claude/.credentials.json" exp now_ms
  [[ -r "$cred" ]] || return 1
  REUSE_TOKEN="$(grep -oE '"accessToken"[[:space:]]*:[[:space:]]*"[^"]+"' "$cred" | head -n1 | sed -E 's/.*"([^"]+)"$/\1/' || true)"
  exp="$(grep -oE '"expiresAt"[[:space:]]*:[[:space:]]*[0-9]+' "$cred" | head -n1 | grep -oE '[0-9]+$' || true)"
  [[ -n "$REUSE_TOKEN" && -n "$exp" ]] || return 1
  now_ms=$(( $(date +%s) * 1000 ))
  (( exp > now_ms + 300000 )) || return 1        # <5 min left → treat as no valid token
  REUSE_TOKEN_H=$(( (exp - now_ms) / 3600000 ))
  return 0
}

# Append CLAUDE_CODE_OAUTH_TOKEN=<token> to the project .env (owner-only), unless one is already set.
persist_token() {
  local token="$1" envf="$PROJECT_DIR/.env"
  if [[ -f "$envf" ]] && grep -qE '^[[:space:]]*CLAUDE_CODE_OAUTH_TOKEN=[^[:space:]]' "$envf"; then
    log "CLAUDE_CODE_OAUTH_TOKEN already set in .env — leaving it as-is."; return 0
  fi
  touch "$envf"          # a fresh .env would otherwise be created at the umask default (world-readable)
  chmod 600 "$envf"      # owner-only before the long-lived token lands (matches _env.sh set_env_var)
  printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\n' "$token" >> "$envf"
  log "Saved CLAUDE_CODE_OAUTH_TOKEN to $envf."
}

# When the deploy would otherwise be keyless, wire up a Claude subscription token so chat works.
# Needs the `claude` CLI (found even under sudo). Flow: ensure signed in (offering `claude auth login`
# if not — needs a tty); then REUSE-FIRST — reuse the existing login token (instant, no browser) for
# THIS deploy only, NOT persisted so a re-run refreshes it. Only if no valid login token exists do we
# fall back to minting a long-lived (~1yr) token via `claude setup-token` (browser), which IS persisted
# to .env. Any missing piece (no CLI, not signed in + no tty, declined, aborted) → stay keyless.
ensure_claude_auth() {
  [[ "$KEYLESS" == 1 ]] || return 0
  local envf="$PROJECT_DIR/.env" reply out token
  if ! resolve_claude_cmd; then
    warn "the 'claude' CLI isn't reachable — can't set up subscription chat automatically. Install Claude Code (https://claude.com/claude-code) and sign in, or add CLAUDE_CODE_OAUTH_TOKEN to $envf, then re-run. Deploying keyless."
    return 0
  fi

  # Ensure a Claude sign-in (an interactive login needs a terminal).
  if ! claude_logged_in; then
    if [[ ! -t 0 ]]; then
      warn "not signed in to Claude and no terminal for an interactive sign-in — deploying keyless. Run 'claude auth login' (or add CLAUDE_CODE_OAUTH_TOKEN to $envf), then re-run."
      return 0
    fi
    printf '\033[36m?\033[0m Not signed in to Claude. Sign in now to enable chat? [Y/n] '
    IFS= read -r reply || reply=""
    case "$reply" in [nN]|[nN][oO]) log "Skipping Claude sign-in — deploying keyless (chat disabled)."; return 0 ;; esac
    step "Signing in to Claude (claude auth login)"
    "${CLAUDE_CMD[@]}" auth login \
      || { warn "'claude auth login' didn't complete — proceeding keyless. Sign in, then re-run."; return 0; }
    claude_logged_in || { warn "still not signed in after 'claude auth login' — proceeding keyless."; return 0; }
  fi

  # Reuse-first: use the existing login token (no browser). Short-lived + unrefreshable in the pod, so
  # use it in-memory for THIS deploy only and DON'T persist it — a re-run then reads a fresh one.
  if read_login_token; then
    OAUTH_TOKEN="$REUSE_TOKEN"; PROVIDER="claude-agent-sdk"; KEYLESS=0
    log "Reusing your Claude login token — chat enabled for this deploy."
    warn "this login token expires in ~${REUSE_TOKEN_H}h and the pod can't refresh it — just re-run ./install.sh to refresh, or run 'claude setup-token' and add the 1-year CLAUDE_CODE_OAUTH_TOKEN to $envf for a durable deploy."
    return 0
  fi

  # Fallback: no valid login token to reuse → mint a long-lived (~1yr) one (needs a browser + tty).
  if [[ ! -t 0 ]]; then
    warn "no reusable login token and no terminal for 'claude setup-token' — deploying keyless. Run 'claude setup-token', add CLAUDE_CODE_OAUTH_TOKEN to $envf, then re-run."
    return 0
  fi
  step "Minting a long-lived (~1yr) Claude token (claude setup-token — a browser window will open; complete the sign-in there)"
  out="$("${CLAUDE_CMD[@]}" setup-token)" || { warn "'claude setup-token' did not complete — proceeding keyless. Run it yourself, add CLAUDE_CODE_OAUTH_TOKEN to $envf, then re-run."; return 0; }
  token="$(printf '%s\n' "$out" | grep -oE 'sk-ant-oat[A-Za-z0-9._-]+' | tail -n1 || true)"
  [[ -n "$token" ]] || { warn "couldn't read a token from 'claude setup-token' output — proceeding keyless. Add CLAUDE_CODE_OAUTH_TOKEN to $envf manually, then re-run."; return 0; }
  persist_token "$token"
  OAUTH_TOKEN="$token"; PROVIDER="claude-agent-sdk"; KEYLESS=0
  log "Claude subscription wired (1-year token) — this deploy will have chat enabled."
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

# kind runs a K8s control plane inside a container whose init is systemd; if the host's inotify limits
# are too low that systemd never reaches Multi-User and `kind create cluster` fails with "could not find
# a log line that matches ...Reached target .*Multi-User System..." (logs show "Failed to allocate
# directory watch: Too many open files"). Raise the limits (once, persisted) before creating a cluster.
# No-op when they're already high enough — so the common case never prompts for a sudo password. Every
# privileged command is best-effort: a locked-down host that forbids the sysctl must NOT abort the
# install (kind may still work; if not, the die on cluster-create surfaces the real error).
ensure_inotify() {
  local watches instances
  watches="$(sysctl -n fs.inotify.max_user_watches 2>/dev/null || echo 0)"
  instances="$(sysctl -n fs.inotify.max_user_instances 2>/dev/null || echo 0)"
  [[ "$watches" -ge 1048576 && "$instances" -ge 8192 ]] && return 0
  step "Raising inotify limits so kind's node systemd can boot (you may be prompted for your sudo password)"
  sudo sysctl -w fs.inotify.max_user_watches=1048576 || true
  sudo sysctl -w fs.inotify.max_user_instances=8192 || true
  printf 'fs.inotify.max_user_watches=1048576\nfs.inotify.max_user_instances=8192\n' | sudo tee /etc/sysctl.d/99-inotify-kind.conf >/dev/null || true
}

ensure_cluster() {
  if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
    log "Reusing existing kind cluster '$CLUSTER'."
  else
    ensure_inotify
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
  log "    # then open http://localhost:8000"
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
  bootstrap_if_curl "$@"   # curl one-liner: clone + re-exec on-disk; no-op inside a checkout
  CTX="kind-$CLUSTER"
  preflight
  resolve_auth
  ensure_claude_auth
  build_image
  ensure_cluster
  load_image
  deploy
  wait_ready
  health_check
  report
  # Open the UI by default on a terminal; --no-open opts out, --open forces it even without a tty.
  local should_open=0
  if [[ "$NO_OPEN" != 1 ]] && [[ "$OPEN" == 1 || -t 1 ]]; then should_open=1; fi
  if [[ "$should_open" == 1 ]]; then
    open_ui
  fi
}

main "$@"
