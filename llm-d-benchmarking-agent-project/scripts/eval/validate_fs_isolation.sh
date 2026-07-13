#!/usr/bin/env bash
# validate_fs_isolation.sh — prove the deployed agent can't write outside its mounted volumes,
# and that the namespace blocks the hostPath-Job escape. OPT-IN, operator-run against a LIVE
# cluster where the chart is already installed (NOT part of the hermetic pytest gate — see
# tests/platform/test_packaging.py for the chart-render/policy/job-manifest unit checks).
#
# It performs three black-box checks with the OPERATOR's kubeconfig (not the agent SA):
#   1. namespace carries pod-security.kubernetes.io/enforce=baseline   (PSA configured)
#   2. a hostPath Pod is refused at admission (server dry-run)         (PSA enforcing)
#   3. inside the pod: the ONLY writable mounts are the expected set, and the read-only rootfs
#      rejects writes to /, /app, /opt/venv, /repos, /etc, /usr, /var (kernel containment)
#
# Usage: ./scripts/eval/validate_fs_isolation.sh [-n NAMESPACE] [--kubeconfig PATH] [--context NAME]
#   -n, --namespace NS   namespace the agent is deployed in (default: llmd-bench)
set -euo pipefail

NAMESPACE="${NAMESPACE:-llmd-bench}"
KCTX=()   # --kubeconfig/--context passthrough
# The writable mounts a correctly-hardened pod is allowed to have: the two chart volumes plus the
# kubelet's own per-container binds. Anything else that is read-write is a containment hole.
ALLOWED_RW=("/workspace" "/tmp" "/dev" "/dev/shm" "/dev/termination-log"
            "/etc/hosts" "/etc/hostname" "/etc/resolv.conf")
# Rootfs locations that MUST be read-only (writing to any of them means readOnlyRootFilesystem is off).
READONLY_DIRS=("/" "/app" "/opt/venv" "/repos" "/etc" "/usr" "/var")

while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--namespace) NAMESPACE="${2:?}"; shift 2 ;;
    --kubeconfig)   KCTX+=(--kubeconfig "${2:?}"); shift 2 ;;
    --context)      KCTX+=(--context "${2:?}"); shift 2 ;;
    -h|--help)      sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option '$1'" >&2; exit 2 ;;
  esac
done
kc() { kubectl ${KCTX[@]+"${KCTX[@]}"} "$@"; }

pass() { printf '  \033[32m✓\033[0m %s\n' "$*"; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$*"; FAILED=1; }
hdr()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
FAILED=0

hdr "Namespace '$NAMESPACE' — Pod Security admission"
ENFORCE="$(kc get namespace "$NAMESPACE" -o jsonpath='{.metadata.labels.pod-security\.kubernetes\.io/enforce}' 2>/dev/null || true)"
[[ "$ENFORCE" == "baseline" || "$ENFORCE" == "restricted" ]] \
  && pass "enforce=$ENFORCE (hostPath / privileged pods forbidden at admission)" \
  || fail "enforce label is '${ENFORCE:-<unset>}' — expected baseline (or stricter). The chart's Namespace object sets it; is podSecurity.enabled=true?"

hdr "hostPath Pod is refused at admission (server dry-run)"
HOSTPATH_POD="$(cat <<'YAML'
apiVersion: v1
kind: Pod
metadata: { name: fs-isolation-probe, labels: { app: fs-isolation-probe } }
spec:
  containers:
    - name: probe
      image: busybox
      command: ["sh", "-c", "sleep 1"]
      volumeMounts: [ { name: node-root, mountPath: /host } ]
  volumes:
    - name: node-root
      hostPath: { path: / }
YAML
)"
if OUT="$(printf '%s' "$HOSTPATH_POD" | kc -n "$NAMESPACE" apply --dry-run=server -f - 2>&1)"; then
  fail "a hostPath Pod was ADMITTED — the namespace is not enforcing Pod Security. Output: $OUT"
else
  if grep -qiE 'hostPath|PodSecurity|violat|forbidden' <<<"$OUT"; then
    pass "rejected by admission ($(grep -oiE 'hostPath[^"]*' <<<"$OUT" | head -1 || echo 'PodSecurity violation'))"
  else
    fail "rejected, but not clearly by Pod Security — verify manually. Output: $OUT"
  fi
fi

hdr "Agent pod — writable surface + read-only rootfs"
POD="$(kc -n "$NAMESPACE" get pod -l app.kubernetes.io/name=llm-d-benchmarking-agent \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
if [[ -z "$POD" ]]; then
  fail "no agent pod found in '$NAMESPACE' (label app.kubernetes.io/name=llm-d-benchmarking-agent) — is the release installed and running?"
else
  # Every read-write mount point the container actually has.
  RW_MOUNTS="$(kc -n "$NAMESPACE" exec "$POD" -- sh -c \
    'awk "\$4 ~ /(^|,)rw(,|\$)/ {print \$2}" /proc/mounts | sort -u' 2>/dev/null || true)"
  UNEXPECTED=""
  while IFS= read -r mnt; do
    [[ -z "$mnt" ]] && continue
    ok=0; for a in "${ALLOWED_RW[@]}"; do [[ "$mnt" == "$a" ]] && ok=1 && break; done
    [[ $ok -eq 0 ]] && UNEXPECTED+="$mnt "
  done <<<"$RW_MOUNTS"
  [[ -z "$UNEXPECTED" ]] \
    && pass "writable mounts are exactly the expected set: $(tr '\n' ' ' <<<"$RW_MOUNTS")" \
    || fail "UNEXPECTED writable mount(s): $UNEXPECTED — a path outside /workspace + /tmp is writable."

  # The read-only rootfs must reject a write to each protected dir.
  for d in "${READONLY_DIRS[@]}"; do
    if kc -n "$NAMESPACE" exec "$POD" -- sh -c "touch $d/.fsprobe 2>/dev/null && rm -f $d/.fsprobe" 2>/dev/null; then
      fail "WROTE to $d — read-only rootfs is not in effect there."
    else
      pass "write to $d refused (read-only)"
    fi
  done
fi

echo
if [[ $FAILED -eq 0 ]]; then
  printf '\033[1;32mPASS\033[0m — the agent is confined to /workspace + /tmp, and the namespace blocks the hostPath escape.\n'
else
  printf '\033[1;31mFAIL\033[0m — one or more isolation checks did not hold (see ✗ above).\n'; exit 1
fi
