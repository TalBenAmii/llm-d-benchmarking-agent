#!/usr/bin/env bash
# Vetted prerequisite installer for the llm-d benchmarking agent.
#
# install.sh (in llm-d-benchmark) installs the framework toolchain (kubectl/helm/jq/…)
# but NOT the Docker daemon or the `kind` binary. This script installs exactly those two
# (plus optionally kubectl) and nothing else — it is the ONLY thing the security allowlist
# lets the agent run with install privileges. The agent invokes it through `run_command`,
# so it goes through the normal approval gate. Every command here is pinned; the allowlist
# grants no raw apt-get/curl/sudo.
#
# Usage:
#   install_prereqs.sh --all                 # docker + kind + kubectl
#   install_prereqs.sh --docker --kind        # just those
#   install_prereqs.sh --kind --kind-version v0.31.0
#
# Flags: --docker  --kind  --kubectl  --all  --kind-version <vX.Y.Z>  -h|--help
#
# Idempotent: anything already on PATH is skipped. Needs root, or passwordless sudo
# (it uses `sudo -n` and fails fast with a clear message if a password would be required).
set -euo pipefail

KIND_VERSION="v0.31.0"          # pinned default; override with --kind-version
WANT_DOCKER=0; WANT_KIND=0; WANT_KUBECTL=0

usage() { sed -n '2,18p' "$0"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --docker)       WANT_DOCKER=1 ;;
    --kind)         WANT_KIND=1 ;;
    --kubectl)      WANT_KUBECTL=1 ;;
    --all)          WANT_DOCKER=1; WANT_KIND=1; WANT_KUBECTL=1 ;;
    --kind-version) KIND_VERSION="${2:?--kind-version needs a value}"; shift ;;
    -h|--help)      usage; exit 0 ;;
    *) echo "[install_prereqs] unknown flag: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done

if [[ "$WANT_DOCKER$WANT_KIND$WANT_KUBECTL" == "000" ]]; then
  echo "[install_prereqs] nothing to do — pass --docker / --kind / --kubectl / --all" >&2
  exit 2
fi

log()  { printf '\033[1;32m[install_prereqs]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[install_prereqs]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[install_prereqs] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# --- Privilege: root, or non-interactive sudo, or stop with a clear message --
SUDO=""
if [[ "$(id -u)" -ne 0 ]]; then
  if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
    SUDO="sudo"
  else
    die "need root or passwordless sudo to install system packages. Re-run this as root,
     or configure passwordless sudo for this user, then try again. (The agent runs
     commands non-interactively, so it cannot type a sudo password.)"
  fi
fi

ARCH="$(dpkg --print-architecture 2>/dev/null || echo amd64)"
export DEBIAN_FRONTEND=noninteractive

# --- Docker Engine -----------------------------------------------------------
if [[ "$WANT_DOCKER" == 1 ]]; then
  if command -v docker >/dev/null 2>&1; then
    log "docker already present — skipping install."
  else
    command -v apt-get >/dev/null 2>&1 || die "this installer supports apt-based distros (Debian/Ubuntu) only."
    log "Installing Docker Engine (official apt repo)…"
    . /etc/os-release
    $SUDO install -m 0755 -d /etc/apt/keyrings
    $SUDO curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    $SUDO chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=${ARCH} signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
      | $SUDO tee /etc/apt/sources.list.d/docker.list >/dev/null
    $SUDO apt-get update -y
    $SUDO apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  fi

  # Best-effort: start the daemon and let the invoking user reach the socket.
  log "Ensuring the Docker daemon is running…"
  $SUDO systemctl enable --now docker 2>/dev/null \
    || $SUDO service docker start 2>/dev/null \
    || warn "could not auto-start the Docker daemon (no systemd? WSL/Docker Desktop?). You may need to start Docker manually."
  TARGET_USER="${SUDO_USER:-${USER:-}}"
  if [[ -n "$TARGET_USER" && "$TARGET_USER" != "root" ]]; then
    $SUDO usermod -aG docker "$TARGET_USER" 2>/dev/null \
      && warn "added '$TARGET_USER' to the 'docker' group — log out/in (or 'newgrp docker') for it to take effect."
  fi
  if docker info >/dev/null 2>&1; then
    log "Docker is up and reachable."
  else
    warn "docker is installed but the daemon isn't reachable from this shell yet (group/login or manual start may be needed)."
  fi
fi

# --- kind binary -------------------------------------------------------------
if [[ "$WANT_KIND" == 1 ]]; then
  if command -v kind >/dev/null 2>&1; then
    log "kind already present ($(kind version 2>/dev/null | head -n1)) — skipping."
  else
    log "Installing kind ${KIND_VERSION}…"
    tmp="$(mktemp)"
    curl -fsSLo "$tmp" "https://kind.sigs.k8s.io/dl/${KIND_VERSION}/kind-linux-${ARCH}"
    $SUDO install -m 0755 "$tmp" /usr/local/bin/kind
    rm -f "$tmp"
    log "kind installed at /usr/local/bin/kind."
  fi
fi

# --- kubectl (install.sh also provides this; --kubectl is for a bare env) -----
if [[ "$WANT_KUBECTL" == 1 ]]; then
  if command -v kubectl >/dev/null 2>&1; then
    log "kubectl already present — skipping."
  else
    log "Installing kubectl…"
    KVER="$(curl -fsSL https://dl.k8s.io/release/stable.txt)"
    tmp="$(mktemp)"
    curl -fsSLo "$tmp" "https://dl.k8s.io/release/${KVER}/bin/linux/${ARCH}/kubectl"
    $SUDO install -m 0755 "$tmp" /usr/local/bin/kubectl
    rm -f "$tmp"
    log "kubectl installed at /usr/local/bin/kubectl."
  fi
fi

# --- Summary + exit status ---------------------------------------------------
have() { command -v "$1" >/dev/null 2>&1 && echo present || echo MISSING; }
echo "[install_prereqs] ───────── summary ─────────"
[[ "$WANT_DOCKER"  == 1 ]] && echo "[install_prereqs]   docker:  $(have docker)"
[[ "$WANT_KIND"    == 1 ]] && echo "[install_prereqs]   kind:    $(have kind)"
[[ "$WANT_KUBECTL" == 1 ]] && echo "[install_prereqs]   kubectl: $(have kubectl)"
echo "[install_prereqs] ──────────────────────────"

rc=0
[[ "$WANT_DOCKER"  == 1 ]] && ! command -v docker  >/dev/null 2>&1 && rc=1
[[ "$WANT_KIND"    == 1 ]] && ! command -v kind     >/dev/null 2>&1 && rc=1
[[ "$WANT_KUBECTL" == 1 ]] && ! command -v kubectl  >/dev/null 2>&1 && rc=1
[[ "$rc" -eq 0 ]] && log "Done." || die "one or more requested tools are still missing (see summary)."
