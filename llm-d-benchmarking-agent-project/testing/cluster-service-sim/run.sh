#!/usr/bin/env bash
# run.sh — LOCAL cluster-service smoke adapter (NOT product; testing/ is build-excluded).
#
# Deploys the agent as a Kubernetes SERVICE onto a THROWAWAY `kind` cluster — exercising the
# real service installer (scripts/install/install_service.sh) + Helm chart — and asserts "the app fully
# works" end to end: liveness, readiness, the provider surface, the RBAC least-privilege
# boundary, and (when a key is present) one live-chat round-trip over the /ws WebSocket.
#
# It is a TEST harness for a maintainer to run on a box that HAS docker+kind+kubectl+helm; it is
# never baked into the image (.dockerignore excludes testing/, tests/test_product_boundary.py
# turns that into a checked invariant). It self-terminates: EVERY wait is hard-bounded (no
# unbounded loop can wedge), a post-build watchdog caps the whole cluster phase, and a trap tears
# the cluster down on exit unless --keep.
#
# Usage:
#   ./run.sh [flags]
#   ./run.sh --keep                 # leave the cluster up afterwards for inspection
#   CLAUDE_CODE_OAUTH_TOKEN=... ./run.sh # PRIMARY: deploy claude-agent-sdk (subscription auth) + live chat
#   ANTHROPIC_API_KEY=sk-... ./run.sh    # FALLBACK: deploy anthropic (API key) + the live-chat check
#
#   --cluster NAME        kind cluster name          (default: csvc-sim)
#   -n, --namespace NS    target namespace           (default: llmd-bench)
#   -r, --release NAME    Helm release name          (default: bench-agent)
#   --port PORT           local port-forward port    (default: 8000)
#   --image REPO          image repository           (default: llm-d-benchmarking-agent)
#   --tag TAG             image tag                   (default: 0.1.0)
#   --no-build            require the image to already exist locally (never build it)
#   --keep                do NOT tear the cluster down on exit (and reuse it if it exists)
#   --oauth-token TOKEN   Claude subscription token from `claude setup-token` — PRIMARY auth
#                         (default: $CLAUDE_CODE_OAUTH_TOKEN / a project .env); -> claude-agent-sdk
#   --anthropic-key KEY   Anthropic API key — FALLBACK auth (default: $ANTHROPIC_API_KEY / a project .env)
#   --build-timeout SECS  hard cap on the image build     (default: 1800)
#   --phase-timeout SECS  watchdog on the post-build cluster phase (default: 1800)
#   -h, --help
#
# Exit status is 0 only when every REQUIRED assertion passed.
set -euo pipefail

# ─── location ────────────────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"            # testing/cluster-service-sim -> project root
INSTALLER="$PROJECT_DIR/scripts/install/install_service.sh"      # the service installer we exercise
CHART_DIR="$PROJECT_DIR/deploy/helm/llm-d-benchmarking-agent"  # same chart install_service.sh deploys
MAIN_PID=$$

# ─── defaults ────────────────────────────────────────────────────────────────────────────────
CLUSTER="csvc-sim"
NS="llmd-bench"
RELEASE="bench-agent"
PORT="8000"
IMAGE="llm-d-benchmarking-agent"
TAG="0.1.0"
NO_BUILD=0
KEEP=0
OAUTH_TOKEN="${CLAUDE_CODE_OAUTH_TOKEN:-}"
ANTHROPIC_KEY="${ANTHROPIC_API_KEY:-}"
BUILD_TIMEOUT=1800
PHASE_TIMEOUT=1800

# Per-step bounds (seconds) — none is ever unbounded.
KIND_WAIT=120         # kind create --wait
LOAD_TIMEOUT=300      # kind load docker-image
HELM_TIMEOUT="5m"     # helm --wait (install_service.sh --timeout / direct helm)
DEPLOY_TIMEOUT=420    # outer cap on the deploy step (helm --wait is 5m)
ROLLOUT_TIMEOUT=180   # kubectl rollout status
HEALTH_RETRIES=45     # /healthz poll: 45 * 2s = 90s bounded
HEALTH_INTERVAL=2
CHAT_DEADLINE=120     # WS live-chat client internal deadline

# ─── colours / log ───────────────────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then C_R=$'\033[31m'; C_G=$'\033[32m'; C_Y=$'\033[33m'; C_B=$'\033[1;35m'; C_0=$'\033[0m'
else C_R=; C_G=; C_Y=; C_B=; C_0=; fi
step() { printf '\n%s━━ %s%s\n' "$C_B" "$*" "$C_0"; }
info() { printf '  %s\n' "$*"; }
warn() { printf '%s[csvc-sim] %s%s\n' "$C_Y" "$*" "$C_0" >&2; }

# ─── summary state ───────────────────────────────────────────────────────────────────────────
declare -a SUMMARY=()
REQUIRED_FAILS=0
pass() { printf '  %s✔ PASS%s  %s\n' "$C_G" "$C_0" "$1"; SUMMARY+=("PASS|$1"); }
fail() { printf '  %s✗ FAIL%s  %s\n' "$C_R" "$C_0" "$1"; SUMMARY+=("FAIL|$1"); REQUIRED_FAILS=$((REQUIRED_FAILS+1)); }
skip() { printf '  %s• SKIP%s  %s\n' "$C_Y" "$C_0" "$1"; SUMMARY+=("SKIP|$1"); }

# Print the header comment block (from line 2 to the first non-comment line), stripping "# ".
usage() { awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "${BASH_SOURCE[0]}"; }
have()  { command -v "$1" >/dev/null 2>&1; }
die()   { printf '%s[csvc-sim] ERROR: %s%s\n' "$C_R" "$*" "$C_0" >&2; exit 1; }

# Read KEY=value for $1 from a project .env if present; echo the parsed value (empty if absent).
# Pure bash parameter-expansion parsing: strip a trailing CR and surrounding quotes. The `|| true`
# keeps a keyless .env from tripping `set -euo pipefail`. Always returns 0.
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

# ─── arg parsing ─────────────────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --cluster)         CLUSTER="${2:?--cluster needs a value}"; shift 2 ;;
    -n|--namespace)    NS="${2:?--namespace needs a value}"; shift 2 ;;
    -r|--release)      RELEASE="${2:?--release needs a value}"; shift 2 ;;
    --port)            PORT="${2:?--port needs a value}"; shift 2 ;;
    --image)           IMAGE="${2:?--image needs a value}"; shift 2 ;;
    --tag)             TAG="${2:?--tag needs a value}"; shift 2 ;;
    --no-build)        NO_BUILD=1; shift ;;
    --keep)            KEEP=1; shift ;;
    --oauth-token)     OAUTH_TOKEN="${2:?--oauth-token needs a value}"; shift 2 ;;
    --anthropic-key)   ANTHROPIC_KEY="${2:?--anthropic-key needs a value}"; shift 2 ;;
    --build-timeout)   BUILD_TIMEOUT="${2:?--build-timeout needs a value}"; shift 2 ;;
    --phase-timeout)   PHASE_TIMEOUT="${2:?--phase-timeout needs a value}"; shift 2 ;;
    -h|--help)         usage; exit 0 ;;
    *) die "unknown option '$1' (try --help)" ;;
  esac
done

CTX="kind-$CLUSTER"            # kind names its kube-context kind-<cluster>; target it EXPLICITLY
KREF="$IMAGE:$TAG"
TMPDIR="$(mktemp -d)"
BODY_FILE="$TMPDIR/body"
PF_PID=""
WATCHDOG_PID=""

# Late-bind auth from a project .env if still unset (never overrides an explicit flag/env). The
# OAuth token is the PRIMARY path (Claude subscription auth); the Anthropic API key is the fallback.
if [[ -z "$OAUTH_TOKEN" ]]; then
  OAUTH_TOKEN="$(env_fallback CLAUDE_CODE_OAUTH_TOKEN)"
  [[ -n "$OAUTH_TOKEN" ]] && info "Picked up CLAUDE_CODE_OAUTH_TOKEN from $PROJECT_DIR/.env"
fi
if [[ -z "$ANTHROPIC_KEY" ]]; then
  ANTHROPIC_KEY="$(env_fallback ANTHROPIC_API_KEY)"
  [[ -n "$ANTHROPIC_KEY" ]] && info "Picked up ANTHROPIC_API_KEY from $PROJECT_DIR/.env"
fi

# Provider selection mirrors install_service.sh: an OAuth token -> claude-agent-sdk (subscription
# auth, live-chat testable in-Pod); else an Anthropic API key -> anthropic (metered API, live-chat
# testable); else keyless -> claude-agent-sdk with chat disabled (still passes /readyz green).
if   [[ -n "$OAUTH_TOKEN" ]];   then LLM_PROVIDER="claude-agent-sdk"
elif [[ -n "$ANTHROPIC_KEY" ]]; then LLM_PROVIDER="anthropic"
else                                 LLM_PROVIDER="claude-agent-sdk"; fi
LIVE_CHAT_AUTH=0; [[ -n "$OAUTH_TOKEN" || -n "$ANTHROPIC_KEY" ]] && LIVE_CHAT_AUTH=1

# ─── cleanup / trap ──────────────────────────────────────────────────────────────────────────
# Idempotent, self-bounded (every teardown command is timeout-wrapped so cleanup itself can't
# wedge). Kills the port-forward + watchdog, then deletes the cluster unless --keep.
cleanup() {
  local rc=$?
  trap - EXIT INT TERM
  [[ -n "$WATCHDOG_PID" ]] && kill "$WATCHDOG_PID" 2>/dev/null || true
  [[ -n "$PF_PID" ]] && kill "$PF_PID" 2>/dev/null || true
  if [[ "$KEEP" == 1 ]]; then
    step "--keep: leaving cluster '$CLUSTER' up"
    info "Reach it:   kubectl --context $CTX -n $NS port-forward svc/\$(kubectl --context $CTX -n $NS get svc -o name | head -n1 | cut -d/ -f2) $PORT:8000"
    info "Tear down:  kind delete cluster --name $CLUSTER"
  else
    step "Tearing down (trap): helm uninstall + kind delete cluster '$CLUSTER'"
    timeout 60 helm --kube-context "$CTX" uninstall "$RELEASE" -n "$NS" >/dev/null 2>&1 || true
    timeout 120 kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true
  fi
  rm -rf "$TMPDIR" 2>/dev/null || true
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 124' TERM   # watchdog SIGTERM -> exit -> EXIT trap runs cleanup

# Overall watchdog for the post-build cluster phase (a "plus" guard on top of per-step bounds):
# if the whole phase overruns, SIGTERM the main shell so the trap cleans up rather than hanging.
arm_watchdog() {
  ( sleep "$PHASE_TIMEOUT"
    printf '%s[csvc-sim] watchdog: cluster phase exceeded %ss — aborting%s\n' "$C_R" "$PHASE_TIMEOUT" "$C_0" >&2
    kill -TERM "$MAIN_PID" 2>/dev/null ) &
  WATCHDOG_PID=$!
}

# ─── http helper ─────────────────────────────────────────────────────────────────────────────
# Sets HTTP_CODE, writes the response body to $BODY_FILE. Never trips set -e (000 on failure).
http_get() {
  HTTP_CODE="$(timeout 15 curl -sS -o "$BODY_FILE" -w '%{http_code}' "http://127.0.0.1:$PORT$1" 2>/dev/null || echo 000)"
}

# Best-effort, bounded cluster dump printed before a fatal deploy/rollout/reachability failure —
# so a maintainer sees WHY even though the trap then tears the cluster down (re-run with --keep to
# poke at it live). Each command is timeout-wrapped; failures are swallowed.
dump_diagnostics() {
  step "DIAGNOSTICS (best-effort)"
  timeout 20 kubectl --context "$CTX" -n "$NS" get pods -o wide 2>&1 | sed 's/^/  /' || true
  timeout 20 kubectl --context "$CTX" -n "$NS" get events --sort-by=.lastTimestamp 2>&1 | tail -n 20 | sed 's/^/  /' || true
  local pod
  pod="$(timeout 15 kubectl --context "$CTX" -n "$NS" get pods -o name 2>/dev/null | head -n1 || true)"
  if [[ -n "$pod" ]]; then
    timeout 20 kubectl --context "$CTX" -n "$NS" describe "$pod" 2>&1 | tail -n 30 | sed 's/^/  /' || true
    timeout 20 kubectl --context "$CTX" -n "$NS" logs "$pod" --tail=40 2>&1 | sed 's/^/  /' || true
  fi
}

# ═══════════════════════════════════════════════════════════════════════════════════════════════
# 1. Preflight
# ═══════════════════════════════════════════════════════════════════════════════════════════════
step "1. Preflight — required tooling"
missing=()
for t in docker kind kubectl helm curl timeout; do have "$t" || missing+=("$t"); done
[[ ${#missing[@]} -eq 0 ]] || die "missing required tool(s): ${missing[*]} — install them and re-run."
[[ -x "$INSTALLER" || -f "$INSTALLER" ]] || die "service installer not found at $INSTALLER"
[[ -d "$CHART_DIR" ]] || die "Helm chart not found at $CHART_DIR"
timeout 20 docker info >/dev/null 2>&1 || die "docker is installed but the daemon is unreachable ('docker info' failed)."
HAVE_PY3=0; have python3 && HAVE_PY3=1
info "tooling OK (docker, kind, kubectl, helm, curl, timeout$( [[ $HAVE_PY3 == 1 ]] && echo ', python3' ))"
info "provider: $LLM_PROVIDER$( [[ "$LIVE_CHAT_AUTH" == 1 ]] && echo ' (auth present -> live chat enabled)' || echo ' (no auth -> live chat skipped)')"

# ═══════════════════════════════════════════════════════════════════════════════════════════════
# 2. Ensure the image exists locally
# ═══════════════════════════════════════════════════════════════════════════════════════════════
step "2. Image — ensure $KREF exists locally"
if docker image inspect "$KREF" >/dev/null 2>&1; then
  info "found $KREF locally"
elif [[ "$NO_BUILD" == 1 ]]; then
  die "--no-build given but $KREF is not present locally. Build it first (make image IMAGE=$IMAGE VERSION=$TAG)."
else
  info "building $KREF (bounded at ${BUILD_TIMEOUT}s) — this is the ~1GB full-bake, first build is slow…"
  if have make; then
    timeout --kill-after=30s "$BUILD_TIMEOUT" make -C "$PROJECT_DIR" image IMAGE="$IMAGE" VERSION="$TAG" \
      || die "image build failed or timed out after ${BUILD_TIMEOUT}s."
  else
    timeout --kill-after=30s "$BUILD_TIMEOUT" docker build -t "$KREF" "$PROJECT_DIR" \
      || die "image build failed or timed out after ${BUILD_TIMEOUT}s."
  fi
  docker image inspect "$KREF" >/dev/null 2>&1 || die "build reported success but $KREF is still absent."
  info "built $KREF"
fi

# Everything past here is bounded by the phase watchdog too.
arm_watchdog

# ═══════════════════════════════════════════════════════════════════════════════════════════════
# 3. kind cluster
# ═══════════════════════════════════════════════════════════════════════════════════════════════
step "3. kind cluster '$CLUSTER'"
cluster_exists() { timeout 20 kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; }
if cluster_exists; then
  if [[ "$KEEP" == 1 ]]; then
    info "cluster '$CLUSTER' exists and --keep set — reusing it"
  else
    info "cluster '$CLUSTER' exists — deleting for a clean slate"
    timeout 120 kind delete cluster --name "$CLUSTER" || die "failed to delete pre-existing cluster '$CLUSTER'."
    timeout "$((KIND_WAIT + 120))" kind create cluster --name "$CLUSTER" --wait "${KIND_WAIT}s" \
      || die "kind create cluster timed out/failed."
  fi
else
  timeout "$((KIND_WAIT + 120))" kind create cluster --name "$CLUSTER" --wait "${KIND_WAIT}s" \
    || die "kind create cluster timed out/failed."
fi
timeout 20 kubectl --context "$CTX" cluster-info >/dev/null 2>&1 || die "kind context '$CTX' is not reachable."
info "cluster ready (context $CTX)"

# ═══════════════════════════════════════════════════════════════════════════════════════════════
# 4. Load the image into kind (so the node never reaches a registry)
# ═══════════════════════════════════════════════════════════════════════════════════════════════
step "4. Load $KREF into kind"
timeout "$LOAD_TIMEOUT" kind load docker-image "$KREF" --name "$CLUSTER" \
  || die "kind load docker-image timed out/failed after ${LOAD_TIMEOUT}s."
info "image loaded onto node(s)"

# ═══════════════════════════════════════════════════════════════════════════════════════════════
# 5. Deploy via the service installer (kind-appropriate: locally-loaded image, pullPolicy Never)
# ═══════════════════════════════════════════════════════════════════════════════════════════════
step "5. Deploy — release '$RELEASE' into namespace '$NS' (provider $LLM_PROVIDER)"
# ALWAYS exercise the REAL installer end to end — it now selects the provider from the auth flag:
#   --oauth-token  -> claude-agent-sdk (+ secret.claudeCodeOauthToken)
#   --anthropic-key -> anthropic       (+ secret.anthropicApiKey)
#   neither         -> claude-agent-sdk with chat disabled (keeps a keyless /readyz green)
# so there is no longer any keyless helm bypass. --context targets kind explicitly (never the
# caller's current-context); --image-pull-policy Never keeps kind off any registry.
auth_args=()
if   [[ -n "$OAUTH_TOKEN" ]];   then auth_args=(--oauth-token "$OAUTH_TOKEN")
elif [[ -n "$ANTHROPIC_KEY" ]]; then auth_args=(--anthropic-key "$ANTHROPIC_KEY"); fi
timeout --kill-after=30s "$DEPLOY_TIMEOUT" bash "$INSTALLER" \
  -n "$NS" -r "$RELEASE" \
  --image "$IMAGE" --tag "$TAG" --image-pull-policy Never \
  --context "$CTX" --timeout "$HELM_TIMEOUT" \
  ${auth_args[@]+"${auth_args[@]}"} \
  || { dump_diagnostics; die "install_service.sh deploy failed/timed out (see diagnostics above)."; }

# Derive the deployment + service names FROM THE CLUSTER (robust vs the chart's fullname helper).
DEPLOY="$(timeout 20 kubectl --context "$CTX" -n "$NS" get deploy -o name 2>/dev/null | head -n1)"
SVC="$(timeout 20 kubectl --context "$CTX" -n "$NS" get svc -o name 2>/dev/null | head -n1)"
[[ -n "$DEPLOY" ]] || { dump_diagnostics; die "no Deployment found in namespace '$NS' after deploy."; }
[[ -n "$SVC" ]] || { dump_diagnostics; die "no Service found in namespace '$NS' after deploy."; }
info "deployment: $DEPLOY"
info "service:    $SVC"

# ═══════════════════════════════════════════════════════════════════════════════════════════════
# 6. Rollout (helm --wait already blocked on readiness; this is belt-and-suspenders + bounded)
# ═══════════════════════════════════════════════════════════════════════════════════════════════
step "6. Rollout status (bounded ${ROLLOUT_TIMEOUT}s)"
timeout "$((ROLLOUT_TIMEOUT + 30))" kubectl --context "$CTX" -n "$NS" rollout status "$DEPLOY" --timeout="${ROLLOUT_TIMEOUT}s" \
  || { dump_diagnostics; die "deployment did not become Ready within ${ROLLOUT_TIMEOUT}s."; }
info "rollout complete"

# ═══════════════════════════════════════════════════════════════════════════════════════════════
# 7. Port-forward + wait for /healthz (bounded curl-retry — NEVER an unbounded loop)
# ═══════════════════════════════════════════════════════════════════════════════════════════════
step "7. Port-forward $SVC $PORT:8000 and wait for /healthz"
kubectl --context "$CTX" -n "$NS" port-forward "$SVC" "$PORT:8000" >"$TMPDIR/pf.log" 2>&1 &
PF_PID=$!
sleep 1
kill -0 "$PF_PID" 2>/dev/null || { warn "port-forward exited immediately:"; cat "$TMPDIR/pf.log" >&2; die "port-forward failed to start."; }
reachable=0
for ((i=1; i<=HEALTH_RETRIES; i++)); do
  http_get /healthz
  if [[ "$HTTP_CODE" == 200 ]]; then reachable=1; break; fi
  if ! kill -0 "$PF_PID" 2>/dev/null; then warn "port-forward died mid-wait:"; cat "$TMPDIR/pf.log" >&2; break; fi
  sleep "$HEALTH_INTERVAL"
done
[[ "$reachable" == 1 ]] || { dump_diagnostics; die "app never answered /healthz on :$PORT within $((HEALTH_RETRIES*HEALTH_INTERVAL))s."; }
info "app reachable on http://127.0.0.1:$PORT"

# ═══════════════════════════════════════════════════════════════════════════════════════════════
# 8. ASSERTIONS
# ═══════════════════════════════════════════════════════════════════════════════════════════════
step "8. Assertions"

# 8a. /healthz == 200 and body has "ok": true
http_get /healthz
if [[ "$HTTP_CODE" == 200 ]] && grep -Eq '"ok"[[:space:]]*:[[:space:]]*true' "$BODY_FILE"; then
  pass "/healthz 200 with {\"ok\": true}"
else
  fail "/healthz expected 200 + ok:true, got $HTTP_CODE: $(head -c 200 "$BODY_FILE")"
fi

# 8b. /readyz == 200 (green). On failure print the JSON body (it names the failed probe).
http_get /readyz
if [[ "$HTTP_CODE" == 200 ]]; then
  pass "/readyz 200 (readiness self-check green)"
else
  fail "/readyz expected 200, got $HTTP_CODE"
  info "readyz body: $(head -c 600 "$BODY_FILE")"
fi

# 8c. /api/provider == 200; log which provider built.
http_get /api/provider
if [[ "$HTTP_CODE" == 200 ]]; then
  if [[ "$HAVE_PY3" == 1 ]]; then
    pv="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print("provider=%s model=%s configured=%s" % (d.get("provider"), d.get("model"), d.get("configured")))' "$BODY_FILE" 2>/dev/null || true)"
  else
    pv="$(head -c 200 "$BODY_FILE")"
  fi
  pass "/api/provider 200 — $pv"
else
  fail "/api/provider expected 200, got $HTTP_CODE"
fi

# 8d. RBAC boundary (the security proof): the in-cluster SA must NOT be able to delete a
# cluster-scoped namespace. A SUCCESS here would be a security FAILURE. Required pass: the exec
# exits non-zero AND stderr says "forbidden" (case-insensitive).
# set +e around it: the delete is EXPECTED to fail, which would otherwise trip set -e.
set +e
rbac_out="$(timeout 60 kubectl --context "$CTX" -n "$NS" exec "$DEPLOY" -- kubectl delete ns kube-system 2>&1)"
rbac_rc=$?
set -e
if [[ "$rbac_rc" -ne 0 ]] && printf '%s' "$rbac_out" | grep -qi 'forbidden'; then
  pass "RBAC boundary — in-pod 'kubectl delete ns kube-system' Forbidden (least-privilege holds)"
else
  fail "RBAC boundary — expected a Forbidden refusal (rc=$rbac_rc): $(printf '%s' "$rbac_out" | head -c 300)"
fi

# 8e. Live chat (only when an OAuth token or Anthropic key is present). One minimal /ws round-trip;
# assert a non-error assistant reply comes back. Pure-stdlib raw-WS client (no websockets/
# websocket-client dependency). Bounded by both an internal deadline and an outer `timeout`.
if [[ "$LIVE_CHAT_AUTH" == 1 ]]; then
  if [[ "$HAVE_PY3" == 1 ]]; then
    chat_msg="Respond with a brief one-sentence greeting confirming you are online. Do not use any tools or run any commands."
    if chat_out="$(timeout "$((CHAT_DEADLINE + 30))" python3 "$SCRIPT_DIR/ws_chat_probe.py" "$PORT" "$chat_msg" "$CHAT_DEADLINE" 2>&1)"; then
      pass "live chat — assistant replied over /ws ($chat_out)"
    else
      fail "live chat — no valid reply over /ws: $chat_out"
    fi
  else
    # Fallback per the task: approximate live-chat by asserting the authed provider built (configured)
    # AND is the expected provider on /api/provider, and clearly log that the WS round-trip was not
    # performed. Used as an `if` condition so the greps never trip set -e. Provider-agnostic (SDK via
    # token or anthropic via key).
    http_get /api/provider
    if grep -Eq '"configured"[[:space:]]*:[[:space:]]*true' "$BODY_FILE" \
       && grep -Eq "\"provider\"[[:space:]]*:[[:space:]]*\"$LLM_PROVIDER\"" "$BODY_FILE"; then
      pass "live chat (APPROXIMATED — no python3 for a WS client) — /api/provider shows $LLM_PROVIDER built + configured"
    else
      fail "live chat approximation — /api/provider did not show $LLM_PROVIDER configured"
    fi
  fi
else
  skip "live chat — no OAuth token or Anthropic key (deployed claude-agent-sdk with chat disabled for a keyless-green /readyz)"
fi

# ═══════════════════════════════════════════════════════════════════════════════════════════════
# 9. Summary
# ═══════════════════════════════════════════════════════════════════════════════════════════════
step "SUMMARY"
for line in "${SUMMARY[@]}"; do
  lvl="${line%%|*}"; msg="${line#*|}"
  case "$lvl" in
    PASS) printf '  %s✔ PASS%s  %s\n' "$C_G" "$C_0" "$msg" ;;
    FAIL) printf '  %s✗ FAIL%s  %s\n' "$C_R" "$C_0" "$msg" ;;
    SKIP) printf '  %s• SKIP%s  %s\n' "$C_Y" "$C_0" "$msg" ;;
  esac
done
echo
if [[ "$REQUIRED_FAILS" -eq 0 ]]; then
  printf '%s✔ ALL REQUIRED CHECKS PASSED%s — the cluster-service install fully works.\n' "$C_G" "$C_0"
  exit 0
else
  printf '%s✗ %s REQUIRED CHECK(S) FAILED%s\n' "$C_R" "$REQUIRED_FAILS" "$C_0"
  exit 1
fi
