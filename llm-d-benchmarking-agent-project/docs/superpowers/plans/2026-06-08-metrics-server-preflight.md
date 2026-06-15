# metrics-server Pre-flight Check + Agent Offer — Implementation Plan

**Status:** SHIPPED (2026-06-08) — done. This is the historical implementation record; the
feature is live in code. Read the code for current behavior; this doc explains *what* was built
and *why*. Companion design: `docs/superpowers/specs/2026-06-08-metrics-server-preflight-offer-design.md`.

**Goal:** Detect the in-cluster metrics-server deterministically *before* a benchmark run (a probe
fact), have the agent offer the approval-gated install in chat via a HARD_RULE, and retire the
colliding mid-run install button.

**Architecture:** A fact-only `metrics_server` check in `probe_environment` (mirrors
`_probe_prometheus_crds`) plus the connect-time pre-warm list, so the fact is present from turn 1.
One `HARD_RULES` line drives the pre-run offer (judgment stays in prompt/knowledge). The busy-only
UI install button became a passive note and its dead queueing infra was removed.

## What shipped (where the code lives now)

- **Probe fact** — `app/tools/probe.py`: `"metrics_server"` in `_ALL_CHECKS`; `_probe_metrics_server`
  helper (+ `_items_from_json`) returning `{available, installed, ready_replicas}`. `available` =
  `kubectl top nodes` exits 0; `installed` = metrics-server Deployment present in kube-system (queried
  by `-l k8s-app=metrics-server` label since `kubectl get` permits one positional); `ready_replicas`
  = `status.availableReplicas` (0 == installed-but-NotReady, the kind missing-`--kubelet-insecure-tls`
  case). Pure mechanism: never a verdict, never the install decision; degrades to all-absent with no
  kubectl. Both argvs are `read_only` against the allowlist (no allowlist change).
- **Pre-warm** — `app/main.py` `_prewarm_env`: `"metrics_server"` in the turn-1 snapshot list.
- **Agent offer** — `app/agent/prompt.py` `HARD_RULES`: on local kind, BEFORE the first `run`, if
  `metrics_server.available` is false make a single one-line offer to install via
  `run_command(["install_metrics_server.sh","--kubelet-insecure-tls"])`; per-cluster add-on (one
  install covers later runs); SKIP if already available / declined / managed cluster (GKE/OpenShift).
- **Knowledge** — `knowledge/quickstart_playbook.md` (step 5b keyed to the probe fact + run boundary)
  and `knowledge/observability.md` (offer keyed off `metrics_server.available == false`, pre-run).
- **UI** — `ui/app.js`: unavailable-panel button → passive `.resource-note-hint` ("the assistant
  offers to install it before a run"); dead queue infra removed (`sendOrQueueUserMessage`,
  `flushPendingUserSend`, `pendingUserSend`, `metricsInstallRequested`). `ui/styles.css`
  `.resource-fix-btn` → `.resource-note-hint`; `ui/preview.html` comment updated.
- **Tests** — `tests/test_metrics_server_probe.py` (probe states + HARD_RULE guard); `test_ui_frontend.py`
  / `test_static_cache.py` repointed to the passive-hint marker `"offers to install it"`.
- **Docs** — `FEATURES.md` installer row notes the proactive pre-run detection.

## File map (provenance)

| File | Responsibility |
|------|----------------|
| `app/tools/probe.py` | read-only environment facts — `metrics_server` check + helpers |
| `app/main.py` | connect-time pre-warm probe list |
| `tests/test_metrics_server_probe.py` | probe-fact unit tests + HARD_RULE guard |
| `app/agent/prompt.py` | HARD_RULES — offer install before the run when unavailable on kind |
| `knowledge/quickstart_playbook.md` | quickstart judgment (step 5b → probe fact + run boundary) |
| `knowledge/observability.md` | metrics-server judgment (offer keyed off the probe fact) |
| `ui/app.js` / `ui/styles.css` / `ui/preview.html` | passive note; dead queue infra removed |
| `tests/test_ui_frontend.py` / `tests/test_static_cache.py` | UI string contract → passive hint |
| `FEATURES.md` | feature inventory — proactive pre-run offer |

## Design notes (the *why*, preserved)

- The mid-run button lived in a busy-only panel and collided with the backend's single-turn-in-flight
  guard ("still working on the previous request"), so clicking it mid-run silently no-op'd. Moving the
  offer pre-run (deterministic probe fact + HARD_RULE) removed both the collision and the dead
  queue-on-busy infra.
- `--kubelet-insecure-tls` is REQUIRED on kind (self-signed kubelet certs); the installer is
  idempotent and per-cluster.
- The fact dict `{available, installed, ready_replicas}` is identical across the probe helper, its
  tests, and the spec table; the UI marker `"offers to install it"` is shared by `app.js` and both
  UI tests.
